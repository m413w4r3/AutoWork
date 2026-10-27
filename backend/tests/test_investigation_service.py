import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.api.investigation import InvestigationOperatorStatusResponse
from cti_app.application.analyst_handoff import AnalystHandoffPolicy
from cti_app.application.blob_storage import BlobReadLimitExceededError
from cti_app.application.bridge_investigator import (
    BridgeInvestigator,
    ScriptedTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.investigation_composition import (
    build_investigation_composition,
    investigation_tool_dependencies_from_settings,
)
from cti_app.application.investigation_contracts import (
    ArticleClaimV1,
    ArticleContextV1,
    FindingProposalV1,
    PublicationAnchorV1,
)
from cti_app.application.investigation_operator_view import build_investigation_operator_view
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemorySampleAssociationRepository,
    InMemoryToolInvocationRepository,
    InvestigationBudgetSnapshot,
    VersionConflictError,
)
from cti_app.application.investigation_service import (
    InvestigationService,
    InvocationReconciliationOutcome,
    ProductionArtifactContextSnapshotRepository,
    ProductionArtifactObservationWriter,
    _confirmed_external_non_execution,
    _envelope_reduced_since,
    bridge_budget_operation_key,
)
from cti_app.application.investigation_state import (
    InMemoryInvestigationContextSnapshotRepository,
    InMemoryInvestigationStateRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolResult,
)
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.operator_tool_invocation import OperatorToolInvocationService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import (
    USER_REQUEST_ACTION_PREFIX,
    ArticleSnapshot,
    ResearchContextBuilder,
)
from cti_app.config import Settings
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    OpinionBlockType,
    OpinionClaimQualification,
)
from cti_app.domain.external_effects import EvidenceReference
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetReservationStatus,
    DisclosureBounds,
    EnvelopeAuthorityKind,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    FrameworkPreSubmissionProof,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    InvestigationFinding,
    NoveltyClass,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
    tool_operation_key,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    HypothesisStatus,
    StopReason,
    investigation_bridge_attempt,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    AnalystInvestigationStatus,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)

_FINDING_NAMESPACE = UUID("3e2a06e9-660e-44a7-9a9c-6db60a16ff55")
_OBSERVATION_NAMESPACE = UUID("9a5f4af5-e256-45fb-b905-4f913ed229ba")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")


def test_envelope_reduction_uses_only_domain_fields() -> None:
    investigation_id = uuid4()
    baseline = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("network", "vt"),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 20,
                BudgetDimension.DOWNLOADS: 4,
            }
        ),
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    identical = replace(
        baseline,
        budget=InvestigationBudget(limits=dict(baseline.budget.limits)),
    )
    reduced = replace(
        baseline,
        version=baseline.version + 1,
        allowed_providers=("vt",),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 10,
                BudgetDimension.DOWNLOADS: 2,
            }
        ),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.AMBER,
            do_not_submit=True,
            external_llm_allowed=False,
        ),
    )
    extended = replace(
        baseline,
        version=baseline.version + 1,
        allowed_providers=("network", "shodan", "vt"),
        budget=InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: 8,
                BudgetDimension.VT_UNITS: 30,
                BudgetDimension.DOWNLOADS: 6,
            }
        ),
    )

    assert _envelope_reduced_since(baseline, identical) is False
    assert _envelope_reduced_since(baseline, reduced) is True
    assert _envelope_reduced_since(baseline, extended) is False
    assert _envelope_reduced_since(baseline, None) is True


def test_budget_snapshot_preserves_historical_commit_under_revoked_limit() -> None:
    budget = InvestigationBudget(limits={BudgetDimension.VT_UNITS: 3})
    budget.reserve("first", {BudgetDimension.VT_UNITS: 2})
    budget.commit("first", {BudgetDimension.VT_UNITS: 1})
    budget.reserve("second", {BudgetDimension.VT_UNITS: 1})
    budget.commit("second", {BudgetDimension.VT_UNITS: 0})

    snapshot = InvestigationBudgetSnapshot(
        investigation_id=uuid4(),
        version=1,
        budget=budget,
    )

    assert snapshot.budget.limits[BudgetDimension.VT_UNITS] == 3
    assert snapshot.budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert [item.operation_key for item in snapshot.budget.reservations] == ["first", "second"]

    revoked = InvestigationBudget(limits={BudgetDimension.VT_UNITS: 0})
    for reservation in snapshot.budget.reservations:
        revoked.restore_reservation(reservation)
    revoked_snapshot = InvestigationBudgetSnapshot(
        investigation_id=snapshot.investigation_id,
        version=2,
        budget=revoked,
    )

    assert revoked_snapshot.budget.limits[BudgetDimension.VT_UNITS] == 0
    assert revoked_snapshot.budget.consumed[BudgetDimension.VT_UNITS] == 1


def test_tool_operation_identity_ignores_explanatory_purpose() -> None:
    investigation_id = uuid4()
    common = {
        "investigation_id": investigation_id,
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "normalized_parameters": {"file_hash": "a" * 64},
    }

    assert tool_operation_key(**common, purpose="check the sample") == tool_operation_key(
        **common, purpose="verify the same hash"
    )


class _InvestigationRepository:
    def __init__(self):
        self.items = {}

    async def get(self, investigation_id):
        return self.items.get(investigation_id)

    async def get_for_run(self, run_id):
        return next(
            (item for item in self.items.values() if item.production_run_id == run_id), None
        )

    async def add(self, investigation):
        self.items[investigation.id] = investigation

    async def save(self, investigation):
        self.items[investigation.id] = investigation


class _Handoff:
    def __init__(self, repository):
        self.repository = repository
        self.modified_artifacts = []

    async def ensure_for_verified_synthesis(self, *, run, synthesis, **_kwargs):
        existing = await self.repository.get_for_run(run.id)
        if existing is None:
            existing = AnalystInvestigation.from_verified_synthesis(
                synthesis=synthesis,
                budget=LoopBudget(
                    max_cycles=12,
                    max_pivot_runs=20,
                    max_hits_acquired=10,
                    max_new_samples=10,
                    max_vt_read_units=50,
                ),
            )
            await self.repository.add(existing)
        return SimpleNamespace(investigation_id=existing.id)


class _ArticlePort:
    def __init__(self, snapshot):
        self.snapshot = snapshot

    async def resolve(self, synthesis_artifact_id):
        assert synthesis_artifact_id == self.snapshot.synthesis_artifact_id
        return self.snapshot

    async def get_claims(self, investigation_id, claim_ids=()):
        if not claim_ids:
            return self.snapshot.article.claims
        wanted = set(claim_ids)
        return tuple(claim for claim in self.snapshot.article.claims if claim.id in wanted)


class _StateRepository(InMemoryInvestigationStateRepository):
    def __init__(self):
        super().__init__()
        self.save_count = 0
        self.fail_once_at = None

    async def save_if_version(self, state, *, expected_version):
        self.save_count += 1
        if self.save_count == self.fail_once_at:
            self.fail_once_at = None
            raise RuntimeError("simulated process crash after durable tool result")
        return await super().save_if_version(state, expected_version=expected_version)


class _MemoryProofArtifactStore:
    def __init__(self):
        self.blobs = {}
        self.descriptors = {}
        self.read_limit_errors = set()

    def store(self, blob_id, content):
        self.blobs[blob_id] = content
        self.descriptors[blob_id] = {
            "sha256": hashlib.sha256(content).hexdigest(),
            "byte_size": len(content),
            "mime_type": "application/json",
        }

    async def blob_metadata(self, blob_id):
        return self.descriptors.get(blob_id)

    async def read_bytes(self, blob_id, *, max_bytes):
        if blob_id in self.read_limit_errors:
            raise BlobReadLimitExceededError(size_bytes=max_bytes + 1, max_bytes=max_bytes)
        content = self.blobs[blob_id]
        if len(content) > max_bytes:
            raise ValueError("blob exceeds max_bytes")
        return content

    async def summary(self, observation):
        descriptor = await self.blob_metadata(observation.blob_id)
        if descriptor is None or descriptor["sha256"] != observation.sha256:
            return None
        try:
            raw = await self.read_bytes(observation.blob_id, max_bytes=512 * 1024 * 1024)
        except Exception:
            # Match CanonicalEvidenceSummaryPort's bounded binary fallback when
            # a descriptor exists but the payload cannot be read.
            return {"mime_type": descriptor["mime_type"], "byte_size": descriptor["byte_size"]}
        if hashlib.sha256(raw).hexdigest() != observation.sha256:
            return None
        try:
            payload = json.loads(raw)
        except Exception:
            return {"mime_type": descriptor["mime_type"], "byte_size": len(raw)}
        if (
            isinstance(payload, dict)
            and payload.get("schema") == "investigation-tool-observation.v1"
        ):
            result = payload.get("result")
            return result if isinstance(result, dict) else None
        return {"mime_type": descriptor["mime_type"], "byte_size": len(raw)}


def _replace_framework_proof_blob(fixture, observation, raw):
    fixture.proof_artifacts.blobs[observation.blob_id] = raw
    digest = hashlib.sha256(raw).hexdigest()
    fixture.proof_artifacts.descriptors[observation.blob_id].update(
        sha256=digest,
        byte_size=len(raw),
    )
    fixture.observations._items[observation.id] = replace(observation, sha256=digest)


class _ObservationWriter:
    def __init__(self, repository, proof_artifacts=None):
        self.repository = repository
        self.proof_artifacts = proof_artifacts

    async def archive(self, invocation, result):
        encoded = canonical_json_bytes(dict(result.output))
        blob_id = uuid5(_OBSERVATION_NAMESPACE, "blob:" + invocation.operation_key)
        if result.framework_pre_submission_proof is not None:
            safe_result = dict(result.output)
            safe_result["framework_pre_submission_proof"] = {
                "object_type": result.framework_pre_submission_proof.value
            }
            payload = {
                "schema": "investigation-tool-observation.v1",
                "tool_name": invocation.tool_name,
                "schema_version": invocation.schema_version,
                "operation_key": invocation.operation_key,
                "result": safe_result,
            }
            encoded = canonical_json_bytes(payload)
            if self.proof_artifacts is not None:
                self.proof_artifacts.store(blob_id, encoded)
        observation = EvidenceObservation(
            id=uuid5(_OBSERVATION_NAMESPACE, invocation.operation_key),
            investigation_id=invocation.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=blob_id,
            sha256=hashlib.sha256(encoded).hexdigest(),
            source=invocation.tool_name,
            source_version=invocation.schema_version,
            observed_at=datetime.now(UTC),
            object_type=(
                result.framework_pre_submission_proof.value
                if result.framework_pre_submission_proof is not None
                else "provider_authorization_refusal"
                if result.output.get("reason") == "provider_authorization_refused"
                else {
                    "provider_download_route_not_found": "provider_download_route_refused",
                    "provider_download_authorization_refused": (
                        "provider_download_authorization_refused"
                    ),
                }.get(
                    result.output.get("reason"),
                    str(result.output.get("object_type", "tool_call")),
                )
            ),
            object_id=str(result.output.get("object_id", invocation.operation_key)),
            limitations=("Fake tool output is untrusted test data.",),
            invocation_ref=invocation.id,
        )
        return (await self.repository.append_if_absent(observation),)


class _FailingObservationWriter:
    async def archive(self, invocation, result):
        raise RuntimeError("simulated evidence persistence failure")


class _DecisionRepository:
    def __init__(self):
        self.items = []

    async def append(self, decision):
        if not any(item.id == decision.id for item in self.items):
            self.items.append(decision)

    async def list_for_investigation(self, investigation_id):
        return tuple(item for item in self.items if item.investigation_id == investigation_id)


class _ContextBlobStore:
    def __init__(self):
        self.blob_id = uuid4()
        self.payload: Any = None
        self.bucket = None

    async def put_canonical_json(self, payload, *, bucket):
        self.payload = payload
        self.bucket = bucket
        return self.blob_id, hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    async def read_json(self, blob_id):
        assert blob_id == self.blob_id
        return self.payload


class _FakeTool:
    def __init__(self, name, execution_order):
        self.name = name
        self.execution_order = execution_order
        self.calls = []
        self.contexts = []
        self.after_execute = None
        self.result: ToolResult | None = None
        self.observation_object_type = None
        self.observation_object_id = None

    async def execute(self, arguments, context):
        self.calls.append(dict(arguments))
        self.contexts.append(context)
        self.execution_order.append(self.name)
        if self.after_execute is not None:
            result = self.after_execute()
            if hasattr(result, "__await__"):
                await result
        output = {"summary": f"{self.name} observed the requested relation."}
        if self.observation_object_type is not None:
            output["object_type"] = self.observation_object_type
        if self.observation_object_id is not None:
            output["object_id"] = self.observation_object_id
        return self.result or ToolResult(
            status=ToolInvocationStatus.SUCCEEDED,
            output=output,
        )


class _CrashAfterDispatch:
    def __init__(self, dispatcher):
        self.dispatcher = dispatcher
        self.crashed = False

    async def dispatch(self, context, **kwargs):
        result = await self.dispatcher.dispatch(context, **kwargs)
        if not self.crashed:
            self.crashed = True
            raise RuntimeError("simulated worker crash after provider call")
        return result


class _RevokeAfterCommittedOperation:
    def __init__(self, base, operation_key):
        self.base = base
        self.operation_key = operation_key
        self.did_revoke = False

    async def get(self, investigation_id):
        return await self.base.get(investigation_id)

    async def save_if_version(self, envelope, *, expected_version):
        saved = await self.base.save_if_version(envelope, expected_version=expected_version)
        committed = any(
            item.operation_key == self.operation_key and item.status.value == "committed"
            for item in saved.budget.reservations
        )
        if committed and not self.did_revoke:
            self.did_revoke = True
            revoked = replace(
                saved,
                version=saved.version + 1,
                allowed_providers=(),
                acquire_hits_allowed=False,
                budget=replace(
                    saved.budget,
                    limits={dimension: 0 for dimension in saved.budget.limits},
                ),
                disclosure=replace(
                    saved.disclosure,
                    do_not_submit=True,
                    external_llm_allowed=False,
                ),
            )
            await self.base.save_if_version(revoked, expected_version=saved.version)
        return saved


class _Fixture:
    def __init__(
        self,
        *,
        responses,
        bridge_turn_budget=8,
        external_allowed=True,
        tools=(),
        no_progress_limit=3,
        narrative_hashes=(),
        publication_anchors=(),
    ):
        self.run_id = uuid4()
        self.subject_id = uuid4()
        self.synthesis = ProductionArtifact(
            production_run_id=self.run_id,
            subject_id=self.subject_id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="1" * 64,
            status=ProductionArtifactStatus.VERIFIED,
        )
        self.publication_id = uuid4()
        self.snapshot = ArticleSnapshot(
            subject_id=self.subject_id,
            production_run_id=self.run_id,
            synthesis_artifact_id=self.synthesis.id,
            synthesis_sha256="2" * 64,
            article=ArticleContextV1(
                short_summary="A verified campaign narrative.",
                claims=(
                    ArticleClaimV1(
                        id="C1",
                        statement="The article describes a loader chain.",
                        source_refs=("source-1",),
                    ),
                ),
                documented_samples=("a" * 64,),
                known_chain=(),
                open_questions=("Which stage retrieves the payload?",),
                publication_anchors=tuple(publication_anchors),
            ),
            base_publication_artifact_id=self.publication_id,
            base_publication_sha256="3" * 64,
            tlp="GREEN",
            do_not_submit=False,
            external_llm_allowed=external_allowed,
            narrative_hashes=tuple(narrative_hashes),
        )
        self.investigations = _InvestigationRepository()
        seed_investigation = AnalystInvestigation.from_verified_synthesis(
            synthesis=self.synthesis,
            budget=LoopBudget(
                max_cycles=12,
                max_pivot_runs=20,
                max_hits_acquired=10,
                max_new_samples=10,
                max_vt_read_units=50,
            ),
        )
        self.investigations.items[seed_investigation.id] = seed_investigation
        self.investigation_id = seed_investigation.id
        self.handoff = _Handoff(self.investigations)
        self.states = _StateRepository()
        self.context_snapshots = InMemoryInvestigationContextSnapshotRepository()
        self.envelopes = InMemoryInvestigationEnvelopeRepository()
        self.observations = InMemoryEvidenceObservationRepository()
        self.proof_artifacts = _MemoryProofArtifactStore()
        self.findings = InMemoryFindingRepository()
        self.invocations = InMemoryToolInvocationRepository()
        self.analyst_decisions = _DecisionRepository()
        self.commit_count = 0
        self.committed_running_keys = set()
        self.opinions = InMemoryAnalystOpinionRepository()
        self.registry = InMemoryToolRegistry()
        self.tools = {}
        self.execution_order = []
        providers = set()
        capabilities = set()
        for tool_spec in tools:
            name, provider, *configuration = tool_spec
            effect = configuration[0] if configuration else ToolEffect.EXTERNAL_READ
            capability = configuration[1] if len(configuration) > 1 else f"{provider}.read"
            cost_model = (
                configuration[2] if len(configuration) > 2 else {BudgetDimension.VT_UNITS: 1}
            )
            tool = _FakeTool(name, self.execution_order)
            self.tools[name] = tool
            properties = {"query": {"type": "string", "maxLength": 500}}
            if name == "sample.analyze":
                properties["association_id"] = {"type": "string", "minLength": 36, "maxLength": 36}
            definition = ToolDefinition(
                name=name,
                schema_version="1",
                input_schema={
                    "type": "object",
                    "properties": properties,
                    "required": ["query"],
                    "additionalProperties": False,
                },
                effect=effect,
                cost_model=cost_model,
                confidentiality=ToolConfidentiality.INTERNAL,
                limitations=("Passive fake fixture",),
                capability_key=capability,
            )
            self.registry.register(definition, tool)
            providers.add(provider)
            capabilities.add(capability)
        self.available_capabilities = frozenset(capabilities)
        self.bridge_transport = ScriptedTurnTransport(responses)
        self.bridge = BridgeInvestigator(self.bridge_transport)
        self.context_builder = ResearchContextBuilder(
            article_snapshots=_ArticlePort(self.snapshot),
            observations=self.observations,
            findings=self.findings,
            invocations=self.invocations,
            opinions=self.opinions,
        )
        self.service = InvestigationService(
            post_synthesis=self.handoff,
            investigations=self.investigations,
            states=self.states,
            context_snapshots=self.context_snapshots,
            envelopes=self.envelopes,
            context_builder=self.context_builder,
            bridge=self.bridge,
            registry=self.registry,
            dispatcher=RegistryToolDispatcher(self.registry),
            invocations=self.invocations,
            observations=self.observations,
            findings=self.findings,
            opinions=self.opinions,
            observation_writer=_ObservationWriter(self.observations, self.proof_artifacts),
            artifact_store=self.proof_artifacts,
            evidence_summaries=self.proof_artifacts,
            available_capabilities=self.available_capabilities,
            no_progress_limit=no_progress_limit,
            commit_uow=self._commit,
        )

        self.envelope = InvestigationEnvelope(
            investigation_id=self.investigation_id,
            allowed_providers=tuple(sorted(providers)),
            budget=InvestigationBudget(
                limits={
                    BudgetDimension.BRIDGE_TURNS: bridge_turn_budget,
                    BudgetDimension.VT_UNITS: 20,
                }
            ),
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            approval_decision_id=uuid4(),
            approval_actor_id="analyst-1",
        )
        self.run = SimpleNamespace(id=self.run_id, subject_id=self.subject_id)

    async def _commit(self):
        self.commit_count += 1
        current = await self.invocations.list_for_investigation(self.investigation_id)
        self.committed_running_keys.update(
            item.operation_key for item in current if item.status is ToolInvocationStatus.RUNNING
        )

    async def start(self, *, hypotheses=(), execution_mode=None):
        return await self.service.start(
            run=self.run,
            synthesis=self.synthesis,
            extraction_artifacts=(),
            extraction_items=(),
            policy=AnalystHandoffPolicy(
                tlp="GREEN", do_not_submit=False, external_llm_allowed=True
            ),
            envelope=self.envelope,
            hypotheses=tuple(hypotheses),
            execution_mode=execution_mode,
        )


def _enable_osint_vt_hash_reads(fixture: _Fixture, *, authorized: bool = True):
    policy_id = uuid4()
    policy_event_id = uuid4()
    policy_version = 1
    envelope = replace(
        fixture.envelope,
        approval_decision_id=policy_event_id,
        authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
        inherited_policy_id=policy_id,
        inherited_policy_version=policy_version,
        inherited_policy_event_id=policy_event_id,
        allowed_providers=("vt",),
    )
    inheritance = SimpleNamespace(
        investigation_id=fixture.investigation_id,
        subject_id=fixture.subject_id,
        policy_id=policy_id,
        policy_version=policy_version,
        policy_event_id=policy_event_id,
    )
    policy = SimpleNamespace(
        policy_id=policy_id,
        version=policy_version,
        event_id=policy_event_id,
        contains_subject=lambda subject_id: subject_id == fixture.subject_id,
        capabilities=SimpleNamespace(allowed_providers=("vt",)),
    )
    effective = SimpleNamespace(authorized=authorized, inheritance=inheritance, policy=policy)

    async def resolve_effective_policy(_investigation_id):
        return effective

    fixture.service._effective_policy_for = resolve_effective_policy
    return envelope, effective


@pytest.mark.asyncio
async def test_trusted_vt_source_refs_accept_arbitrary_hash_under_active_osint_policy() -> None:
    fixture = _Fixture(responses=[], tools=())
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope, _effective = _enable_osint_vt_hash_reads(fixture)
    arbitrary_hash = "b" * 64

    references = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={"file_hash": arbitrary_hash},
        envelope=envelope,
    )

    assert references == (
        EvidenceReference("sample_hash", arbitrary_hash, "sample-hash.v1", arbitrary_hash),
    )
    assert fixture.observations.items == {}
    caller_claims = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={
            "file_hash": arbitrary_hash,
            "evidence_refs": ["caller-controlled"],
            "_vt_external_effect_source_refs": [
                EvidenceReference("sample_hash", "e" * 64, "sample-hash.v1", "e" * 64).to_json()
            ],
        },
        envelope=envelope,
    )
    assert caller_claims == references


@pytest.mark.asyncio
async def test_trusted_vt_source_refs_remain_strict_outside_osint_policy() -> None:
    fixture = _Fixture(responses=[], tools=())
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    arbitrary_hash = "b" * 64

    references = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={
            "file_hash": arbitrary_hash,
            "evidence_refs": ["caller-controlled"],
            "_vt_external_effect_source_refs": [
                EvidenceReference(
                    "sample_hash", arbitrary_hash, "sample-hash.v1", arbitrary_hash
                ).to_json()
            ],
        },
        envelope=fixture.envelope,
    )

    assert references == ()

    documented_hash = "a" * 64
    documented_references = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={"file_hash": documented_hash},
        envelope=fixture.envelope,
    )
    assert documented_hash in fixture.snapshot.article.documented_samples
    assert documented_references == ()


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_hash", ["g" * 64, "a" * 32, "b" * 40])
async def test_trusted_vt_source_refs_reject_malformed_hash_under_osint_policy(
    invalid_hash: str,
) -> None:
    fixture = _Fixture(responses=[], tools=())
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope, _effective = _enable_osint_vt_hash_reads(fixture)

    references = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={"file_hash": invalid_hash},
        envelope=envelope,
    )

    assert references == ()


@pytest.mark.asyncio
async def test_trusted_vt_source_refs_reject_revoked_osint_policy() -> None:
    fixture = _Fixture(responses=[], tools=())
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope, effective = _enable_osint_vt_hash_reads(fixture, authorized=False)

    references = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={"file_hash": "b" * 64},
        envelope=envelope,
    )

    assert effective.authorized is False
    assert references == ()


@pytest.mark.asyncio
async def test_trusted_vt_source_refs_normalize_case_and_are_replay_stable() -> None:
    fixture = _Fixture(responses=[], tools=())
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope, _effective = _enable_osint_vt_hash_reads(fixture)
    raw_hash = f" {'b' * 64}".upper() + " "

    first = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={"file_hash": raw_hash},
        envelope=envelope,
    )
    replay = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name="vt.file_report",
        arguments={"file_hash": raw_hash},
        envelope=envelope,
    )

    expected = EvidenceReference("sample_hash", "b" * 64, "sample-hash.v1", "b" * 64)
    assert first == replay == (expected,)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_name", "arguments"),
    [
        ("vt.behaviour", {"file_hash": "c" * 64}),
        ("vt.behaviour_pcap", {"file_hash": "c" * 64}),
        ("vt.relationships", {"object_type": "file", "object_id": "c" * 64}),
    ],
)
async def test_trusted_vt_source_refs_accept_any_file_pivot_hash_under_osint(
    tool_name: str, arguments: dict[str, str]
) -> None:
    fixture = _Fixture(responses=[], tools=())
    investigation = await fixture.investigations.get(fixture.investigation_id)
    assert investigation is not None
    envelope, _effective = _enable_osint_vt_hash_reads(fixture)

    references = await fixture.service._trusted_vt_source_references(
        investigation=investigation,
        tool_name=tool_name,
        arguments=arguments,
        envelope=envelope,
    )

    hash_value = arguments.get("file_hash", arguments.get("object_id"))
    assert references == (
        EvidenceReference("sample_hash", hash_value, "sample-hash.v1", hash_value),
    )


def _compose_fixture_service(fixture: _Fixture) -> Any:
    sample_associations = InMemorySampleAssociationRepository()
    uow = SimpleNamespace(
        analyst_investigations=fixture.investigations,
        investigation_states=fixture.states,
        investigation_envelopes=fixture.envelopes,
        tool_invocations=fixture.invocations,
        evidence_observations=fixture.observations,
        investigation_findings=fixture.findings,
        sample_associations=sample_associations,
        analyst_opinions=fixture.opinions,
    )
    settings = Settings.model_construct(investigation_no_progress_limit=3)
    artifact_store = cast(ProductionArtifactStore, SimpleNamespace())
    dependencies = investigation_tool_dependencies_from_settings(
        settings,
        investigation_id=fixture.investigation_id,
        article_snapshots=fixture.context_builder.article_snapshots,
        observations=fixture.observations,
        findings=fixture.findings,
        sample_associations=sample_associations,
        artifact_store=artifact_store,
    )
    composition = build_investigation_composition(
        uow,
        uow_factory=cast(UnitOfWorkFactory, lambda: None),
        artifact_store=artifact_store,
        model_conversations=cast(ModelConversationService, SimpleNamespace()),
        settings=settings,
        investigation_id=fixture.investigation_id,
        tool_dependencies=dependencies,
        turn_transport=fixture.bridge_transport,
    )
    composition.service.post_synthesis = fixture.handoff
    composition.service._context_snapshots = fixture.context_snapshots
    return composition.service


def _tool_call(name, query, purpose):
    return {
        "tool_name": name,
        "schema_version": "1",
        "arguments": {"query": query},
        "purpose": purpose,
    }


def _proposal(
    kind,
    assessment,
    statement,
    observation_id,
    claim_refs=(),
    subject_ref=None,
    novelty=None,
):
    return {
        "kind": kind,
        "assessment": assessment,
        "statement": statement,
        "subject_ref": subject_ref,
        "observation_refs": [str(observation_id)] if observation_id else [],
        "claim_refs": list(claim_refs),
        "novelty": novelty,
        "scope": "investigation article",
        "limitations": [],
    }


def _operation_and_observation_id(investigation_id, call):
    operation_key = tool_operation_key(
        investigation_id=investigation_id,
        tool_name=call["tool_name"],
        schema_version=call["schema_version"],
        normalized_parameters=call["arguments"],
        purpose=call["purpose"],
    )
    return operation_key, uuid5(_OBSERVATION_NAMESPACE, operation_key)


def _finding_id(investigation_id, proposal):
    digest = hashlib.sha256(canonical_json_bytes(proposal)).hexdigest()
    return uuid5(_FINDING_NAMESPACE, f"{investigation_id}:{digest}")


def _decision(*, goal, next_action, tools=(), findings=(), opinion=None, hypothesis_refs=()):
    return {
        "reasoning_summary": f"Selected next action for {goal}.",
        "goal": goal,
        "hypothesis_refs": list(hypothesis_refs),
        "proposed_tool_calls": list(tools),
        "finding_proposals": list(findings),
        "opinion_delta": opinion,
        "next_action": next_action,
    }


def _opinion_delta(finding_id, observation_id, *, text="The observation supports this conclusion."):
    return {
        "blocks": [
            {
                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                "text": text,
                "finding_refs": [str(finding_id)],
                "evidence_refs": [str(observation_id)],
                "qualification": OpinionClaimQualification.FACT.value,
                "certainty": 0.9,
            }
        ],
        "unresolved_points": [],
    }


@pytest.mark.asyncio
async def test_start_is_idempotent_and_uses_verified_synthesis_handoff() -> None:
    fixture = _Fixture(responses=[], tools=())
    first = await fixture.start()
    second = await fixture.start()

    assert first.investigation_id == second.investigation_id
    assert first.version == second.version == 1
    investigation = await fixture.investigations.get(first.investigation_id)
    assert investigation is not None
    assert investigation.status is AnalystInvestigationStatus.RUNNING
    assert fixture.handoff.modified_artifacts == []


@pytest.mark.asyncio
async def test_production_context_snapshot_adapter_round_trips_immutable_json() -> None:
    blob_store = _ContextBlobStore()
    snapshots = ProductionArtifactContextSnapshotRepository(blob_store)
    canonical_context = canonical_json_bytes({"view": "delta", "changes": []}).decode()

    snapshot_id = await snapshots.store(canonical_context)

    assert snapshot_id == blob_store.blob_id
    assert blob_store.bucket == "investigation-context-snapshots"
    assert await snapshots.load(snapshot_id) == canonical_context


@pytest.mark.asyncio
async def test_budget_exhaustion_is_a_local_stop_without_model_error() -> None:
    fixture = _Fixture(responses=[], bridge_turn_budget=0, tools=())
    state = await fixture.start()
    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.BUDGET_EXHAUSTED
    assert fixture.bridge_transport.attempt_count == 0
    assert (
        await fixture.investigations.get(state.investigation_id)
    ).status is AnalystInvestigationStatus.EXHAUSTED


@pytest.mark.asyncio
async def test_operator_view_explains_local_conversation_and_context_limit_code() -> None:
    from cti_app.application.bridge_investigator import make_investigation_conversation_id
    from cti_app.application.investigation_operator_view import (
        build_investigation_operator_view,
    )

    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    pending = state.with_pending_context(uuid4(), "a" * 64).conclude(
        StopReason.SUBMISSION_NEEDS_REVIEW
    )
    context_limited = state.with_context_limit_failure(
        actual_bytes=300_000, max_bytes=262_144
    ).conclude(StopReason.BUDGET_EXHAUSTED)
    investigation = await fixture.investigations.get(fixture.investigation_id)

    pending_view = build_investigation_operator_view(
        investigation=investigation,
        state=pending,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=None,
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=(),
        unavailable_tools=(),
    )
    needs_review_turn_id = uuid4()
    review_turn_view = build_investigation_operator_view(
        investigation=investigation,
        state=pending,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=None,
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=(),
        unavailable_tools=(),
        pending_bridge_turn_id=needs_review_turn_id,
        pending_bridge_turn_status="needs_review",
        pending_bridge_turn_key="turn-key",
    )
    adopted_turn_view = build_investigation_operator_view(
        investigation=investigation,
        state=pending,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=None,
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=(),
        unavailable_tools=(),
        pending_bridge_turn_id=needs_review_turn_id,
        pending_bridge_turn_status="succeeded",
    )
    abandoned_turn_view = build_investigation_operator_view(
        investigation=investigation,
        state=pending,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=None,
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=(),
        unavailable_tools=(),
        pending_bridge_turn_id=needs_review_turn_id,
        pending_bridge_turn_status="failed",
        pending_bridge_turn_error_code="operator_abandoned",
    )
    limit_view = build_investigation_operator_view(
        investigation=investigation,
        state=context_limited,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=None,
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=(),
        unavailable_tools=(),
    )

    assert pending_view.payload["bridge_conversation_id"] == make_investigation_conversation_id(
        fixture.investigation_id
    )
    assert pending_view.payload["bridge_conversation_id_scope"] == "local_autowork_record_only"
    assert pending_view.payload["bridge_external_target"] == {
        "mode": "stateless",
        "lifecycle": "temporary_chat_per_turn",
    }
    assert review_turn_view.payload["next_idempotent_action"]["code"] == (
        "reconcile_model_conversation_turn"
    )
    assert review_turn_view.payload["next_idempotent_action"]["adopt_path"].endswith(
        "/reconciliation/visible/adopt"
    )
    assert adopted_turn_view.payload["next_idempotent_action"]["code"] == ("resume_investigation")
    assert abandoned_turn_view.payload["next_idempotent_action"]["code"] == ("resume_investigation")
    assert pending_view.payload["last_operation"]["conversation_id_scope"] == (
        "local_autowork_record_only"
    )
    response_payload = InvestigationOperatorStatusResponse.model_validate(
        pending_view.payload
    ).model_dump()
    assert set(response_payload) == set(pending_view.payload)
    assert limit_view.payload["last_error"] == {
        "code": "investigation_context_too_large",
        "details": "300000>262144",
    }


@pytest.mark.parametrize(
    ("dimension", "limit", "expected_stop_reason"),
    (
        (BudgetDimension.CONTEXT_BYTES, 1, StopReason.AGENT_FINISHED),
        (BudgetDimension.CONTEXT_TOKENS, 0, StopReason.BUDGET_EXHAUSTED),
    ),
)
@pytest.mark.asyncio
async def test_context_budget_limits_bytes_to_accounting_and_enforces_tokens(
    dimension, limit, expected_stop_reason
) -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
    )
    fixture.envelope = replace(
        fixture.envelope,
        budget=InvestigationBudget(
            limits={
                **dict(fixture.envelope.budget.limits),
                dimension: limit,
            }
        ),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is expected_stop_reason
    assert fixture.bridge_transport.attempt_count == (
        0 if expected_stop_reason is StopReason.BUDGET_EXHAUSTED else 1
    )
    persisted = await fixture.envelopes.get(state.investigation_id)
    assert persisted is not None
    if dimension is BudgetDimension.CONTEXT_BYTES:
        assert persisted.budget.consumed[dimension] > 0
    else:
        assert persisted.budget.consumed[dimension] == 0


@pytest.mark.asyncio
async def test_context_budget_replay_does_not_double_consume_committed_turn() -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
    )
    state = await fixture.start()
    fixture.states.fail_once_at = fixture.states.save_count + 2

    with pytest.raises(RuntimeError, match="simulated process crash"):
        await fixture.service.run(state.investigation_id)

    after_crash = await fixture.envelopes.get(state.investigation_id)
    assert after_crash is not None
    consumed_after_crash = dict(after_crash.budget.consumed)
    pending = await fixture.states.get(state.investigation_id)
    assert pending is not None and pending.pending_context_snapshot_id is not None
    pending_context = await fixture.context_snapshots.load(pending.pending_context_snapshot_id)
    assert pending_context is not None
    context_size_bytes = len(pending_context.encode("utf-8"))
    assert consumed_after_crash[BudgetDimension.CONTEXT_BYTES] == context_size_bytes
    assert consumed_after_crash[BudgetDimension.CONTEXT_TOKENS] == (context_size_bytes + 3) // 4
    assert fixture.bridge_transport.submission_count == 1

    await fixture.service.resume(state.investigation_id)
    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    after_replay = await fixture.envelopes.get(state.investigation_id)
    assert after_replay is not None
    assert (
        after_replay.budget.consumed[BudgetDimension.CONTEXT_BYTES]
        == (consumed_after_crash[BudgetDimension.CONTEXT_BYTES])
    )
    assert (
        after_replay.budget.consumed[BudgetDimension.CONTEXT_TOKENS]
        == (consumed_after_crash[BudgetDimension.CONTEXT_TOKENS])
    )
    assert after_replay.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert (
        after_replay.budget.consumed[BudgetDimension.CONTEXT_TOKENS]
        == (after_replay.budget.consumed[BudgetDimension.CONTEXT_BYTES] + 3) // 4
    )
    assert fixture.bridge_transport.submission_count == 1


@pytest.mark.asyncio
async def test_unlimited_context_dimensions_are_consumed_without_blocking() -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    persisted = await fixture.envelopes.get(state.investigation_id)
    assert persisted is not None
    assert persisted.budget.limits.get(BudgetDimension.CONTEXT_BYTES) is None
    assert persisted.budget.limits.get(BudgetDimension.CONTEXT_TOKENS) is None
    assert persisted.budget.consumed[BudgetDimension.CONTEXT_BYTES] > 0
    assert (
        persisted.budget.consumed[BudgetDimension.CONTEXT_TOKENS]
        == (persisted.budget.consumed[BudgetDimension.CONTEXT_BYTES] + 3) // 4
    )


@pytest.mark.asyncio
async def test_request_permission_pauses_for_review_then_resumes_on_a_new_turn() -> None:
    fixture = _Fixture(
        responses=[
            _decision(goal="request approval for a new query", next_action="request_permission"),
            _decision(goal="finish after review", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    paused = await fixture.service.run(state.investigation_id)

    assert paused.stop_reason is StopReason.PERMISSION_REQUIRED
    assert (
        await fixture.investigations.get(state.investigation_id)
    ).status is AnalystInvestigationStatus.AWAITING_REVIEW
    resumed = await fixture.service.resume(state.investigation_id)
    assert resumed.turn_index == 1
    completed = await fixture.service.run(state.investigation_id)

    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2


@pytest.mark.asyncio
async def test_no_new_information_stops_after_configured_streak() -> None:
    fixture = _Fixture(
        responses=[
            _decision(goal="inspect evidence", next_action="continue"),
            _decision(goal="inspect evidence again", next_action="continue"),
            _decision(goal="try another angle", next_action="continue"),
        ],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.NO_NEW_INFORMATION
    assert result.no_progress_streak == 3
    assert fixture.bridge_transport.submission_count == 3


@pytest.mark.asyncio
async def test_no_progress_allows_hypothesis_switch_at_the_limit() -> None:
    fixture = _Fixture(
        no_progress_limit=1,
        responses=[
            _decision(goal="switch to another hypothesis", next_action="switch_hypothesis"),
            _decision(goal="finish after the switch", next_action="finish"),
        ],
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2


@pytest.mark.asyncio
async def test_no_progress_treats_a_new_operation_as_a_path_forward_once() -> None:
    repeated = _tool_call("tool.not_registered", "new-query", "try an unseen lookup")
    fixture = _Fixture(
        no_progress_limit=2,
        responses=[
            _decision(goal="inspect current context", next_action="continue"),
            _decision(goal="try an unseen lookup", next_action="continue", tools=(repeated,)),
            _decision(goal="repeat the same lookup", next_action="continue", tools=(repeated,)),
        ],
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.NO_NEW_INFORMATION
    assert result.no_progress_streak == 3
    assert fixture.bridge_transport.submission_count == 3


@pytest.mark.asyncio
async def test_switch_hypothesis_abandons_the_referenced_open_hypothesis() -> None:
    fixture = _Fixture(
        responses=[
            _decision(
                goal="leave the unsupported attribution hypothesis",
                next_action="switch_hypothesis",
                hypothesis_refs=("ip-attribution",),
            ),
            _decision(goal="finish with the alternative line", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start(
        hypotheses=(Hypothesis(id="ip-attribution", statement="The IP belongs to campaign C2."),)
    )

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert result.hypotheses[0].status is HypothesisStatus.ABANDONED


@pytest.mark.asyncio
async def test_worker_turn_slice_yields_active_state_without_declaring_budget_stop() -> None:
    fixture = _Fixture(
        responses=[
            _decision(goal="continue research", next_action="continue"),
            _decision(goal="finish after resumed slice", next_action="finish"),
        ],
        tools=(),
    )
    fixture.service._max_turns_per_run = 1
    state = await fixture.start()

    yielded = await fixture.service.run(state.investigation_id)

    assert yielded.stop_reason is None
    assert yielded.turn_index == 1
    assert (
        await fixture.investigations.get(state.investigation_id)
    ).status is AnalystInvestigationStatus.RUNNING
    completed = await fixture.service.run(state.investigation_id)
    assert completed.stop_reason is StopReason.AGENT_FINISHED


@pytest.mark.asyncio
async def test_unavailable_tool_does_not_abort_later_agent_decision() -> None:
    unavailable = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="check a payload", next_action="continue", tools=(unavailable,)),
            _decision(goal="finish with current evidence", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2
    matching_invocations = [
        item
        for item in await fixture.invocations.list_for_investigation(state.investigation_id)
        if item.tool_name == "vt.search" and item.normalized_parameters == unavailable["arguments"]
    ]
    assert len(matching_invocations) == 1
    invocation = matching_invocations[0]
    assert invocation.status is ToolInvocationStatus.UNAVAILABLE


@pytest.mark.asyncio
async def test_two_tools_in_one_turn_use_latest_envelope_and_commit_both_budgets() -> None:
    first = _tool_call("vt.search", "content:abc", "find related payloads")
    second = _tool_call("vt.lookup", "content:def", "check a second payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="compare payloads", next_action="finish", tools=(first, second)),
        ],
        tools=(("vt.search", "vt"), ("vt.lookup", "vt")),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    envelope = await fixture.envelopes.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.execution_order == ["vt.search", "vt.lookup"]
    assert fixture.bridge_transport.submission_count == 1
    assert envelope is not None
    assert envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 2
    for call in (first, second):
        operation_key = tool_operation_key(
            investigation_id=state.investigation_id,
            tool_name=call["tool_name"],
            schema_version=call["schema_version"],
            normalized_parameters=call["arguments"],
            purpose=call["purpose"],
        )
        reservation = next(
            item for item in envelope.budget.reservations if item.operation_key == operation_key
        )
        assert reservation.status.value == "committed"
        assert reservation.committed_amounts[BudgetDimension.VT_UNITS] == 1


@pytest.mark.asyncio
async def test_repeated_tool_parameters_across_turns_reuse_observation_and_budget() -> None:
    first = _tool_call("vt.search", "synthetic:sample", "look for related samples")
    second = _tool_call("vt.search", "synthetic:sample", "check the same sample again")
    fixture = _Fixture(
        responses=[
            _decision(goal="look for related samples", next_action="continue", tools=(first,)),
            _decision(goal="check the same sample again", next_action="continue", tools=(second,)),
            _decision(goal="finish after reviewing the existing result", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    envelope = await fixture.envelopes.get(state.investigation_id)
    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    observation_id = invocations[0].response_refs[0]
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 1
    assert len(invocations) == 1
    assert envelope is not None
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 1
    assert all(item.status.value != "reserved" for item in envelope.budget.reservations)
    assert (
        len(
            [
                item
                for item in envelope.budget.reservations
                if BudgetDimension.VT_UNITS in item.amounts
            ]
        )
        == 1
    )
    assert str(observation_id) in fixture.bridge_transport.prompts[2]
    assert "already executed" in fixture.bridge_transport.prompts[2].lower()


@pytest.mark.asyncio
async def test_provider_auth_refusal_archives_a_typed_observation_with_prior_refs() -> None:
    investigation_id = uuid4()
    observations = InMemoryEvidenceObservationRepository()
    writer = ProductionArtifactObservationWriter(_ContextBlobStore(), observations)
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key="synthetic-provider-auth-operation",
        tool_name="shodan.host",
        schema_version="1",
        normalized_parameters={"ip": "203.0.113.58"},
        purpose="synthetic provider check",
    )
    result = ToolResult(
        status=ToolInvocationStatus.UNAVAILABLE,
        observation_refs=(uuid4(),),
        output={"reason": "provider_authorization_refused"},
    )

    archived = await writer.archive(invocation, result)

    assert len(archived) == 1
    assert archived[0].object_type == "provider_authorization_refusal"
    assert archived[0].source == "shodan.host"


@pytest.mark.asyncio
async def test_download_route_refusal_archives_scoped_untrusted_observation() -> None:
    investigation_id = uuid4()
    observations = InMemoryEvidenceObservationRepository()
    writer = ProductionArtifactObservationWriter(_ContextBlobStore(), observations)
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key="synthetic-download-route-refusal",
        tool_name="vt.download_sample",
        schema_version="1",
        normalized_parameters={"sha256": "e" * 64},
        purpose="synthetic sample acquisition",
    )
    result = ToolResult(
        status=ToolInvocationStatus.UNAVAILABLE,
        output={
            "reason": "provider_download_route_not_found",
            "error_code": "virustotal_download_route_not_found",
            "confirmed_non_execution": True,
        },
    )

    archived = await writer.archive(invocation, result)

    assert len(archived) == 1
    assert archived[0].object_type == "provider_download_route_refused"
    assert archived[0].source == "vt.download_sample"
    assert await observations.get(archived[0].id) == archived[0]


@pytest.mark.asyncio
async def test_framework_policy_refusal_archives_stable_error_code() -> None:
    investigation_id = uuid4()
    observations = InMemoryEvidenceObservationRepository()
    store = _ContextBlobStore()
    writer = ProductionArtifactObservationWriter(store, observations)
    invocation = ToolInvocation(
        investigation_id=investigation_id,
        operation_key="synthetic-source-reference-refusal",
        tool_name="vt.download_sample",
        schema_version="1",
        normalized_parameters={"sha256": "e" * 64},
        purpose="synthetic sample acquisition",
    )
    error_code = "external_disclosure_revoked_before_dispatch"
    result = ToolResult(
        status=ToolInvocationStatus.POLICY_DENIED,
        output={"reason": error_code, "error_code": error_code},
        error_code=error_code,
        framework_pre_submission_proof=FrameworkPreSubmissionProof.DISCLOSURE_REVOKED,
    )

    archived = await writer.archive(invocation, result)
    payload = await store.read_json(archived[0].blob_id)

    assert payload["result"]["error_code"] == error_code
    assert payload["result"]["framework_pre_submission_proof"]["object_type"] == (
        FrameworkPreSubmissionProof.DISCLOSURE_REVOKED.value
    )


@pytest.mark.asyncio
async def test_untyped_download_refusal_requires_reconciliation_before_retry() -> None:
    first_call = _tool_call(
        "vt.download_sample", "synthetic-hash-one", "review first synthetic hit"
    )
    second_call = _tool_call(
        "vt.download_sample", "synthetic-hash-two", "review second synthetic hit"
    )
    fixture = _Fixture(
        responses=[
            _decision(
                goal="review two sample acquisition attempts",
                next_action="continue",
                tools=(first_call, second_call),
            ),
            _decision(goal="finish after repeated route refusals", next_action="finish"),
        ],
        tools=(
            (
                "vt.download_sample",
                "vt",
                ToolEffect.ACQUIRE,
                "vt.file_download",
                {
                    BudgetDimension.VT_UNITS: 1,
                    BudgetDimension.DOWNLOADS: 1,
                },
            ),
        ),
    )
    fixture.envelope = replace(fixture.envelope, acquire_hits_allowed=True)
    fixture.tools["vt.download_sample"].result = ToolResult(
        status=ToolInvocationStatus.UNAVAILABLE,
        budget_consumed={BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 0},
        output={
            "error_code": "virustotal_download_route_not_found",
            "reason": "provider_download_route_not_found",
            "confirmed_non_execution": True,
        },
        message="The provider download route is unavailable.",
    )
    state = await fixture.start()

    stopped = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    observations = await fixture.observations.list_for_investigation(state.investigation_id)
    envelope = await fixture.envelopes.get(state.investigation_id)
    assert stopped.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.NEEDS_REVIEW
    assert len(invocations[0].response_refs) == 1
    assert observations[0].id == invocations[0].response_refs[0]
    assert envelope is not None
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    assert envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0
    reservation = next(
        item
        for item in envelope.budget.reservations
        if item.operation_key == invocations[0].operation_key
    )
    assert reservation.status is BudgetReservationStatus.RESERVED


@pytest.mark.asyncio
async def test_ambiguous_external_result_preserves_only_valid_refs_without_archiving_output() -> (
    None
):
    call = _tool_call(
        "vt.download_sample", "synthetic-partial-download", "reconcile the interrupted download"
    )
    fixture = _Fixture(
        responses=[_decision(goal="reconcile download", next_action="finish", tools=(call,))],
        tools=(("vt.download_sample", "vt", ToolEffect.ACQUIRE),),
    )
    fixture.envelope = replace(fixture.envelope, acquire_hits_allowed=True)
    recorded = EvidenceObservation(
        investigation_id=fixture.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"synthetic recorded partial observation").hexdigest(),
        source="synthetic-download-adapter",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="download_result",
        object_id="synthetic-partial-download",
        limitations=("Synthetic pre-recorded observation.",),
    )
    foreign = replace(recorded, investigation_id=uuid4(), id=uuid4())
    await fixture.observations.append_if_absent(recorded)
    await fixture.observations.append_if_absent(foreign)
    fixture.tools["vt.download_sample"].result = ToolResult(
        status=ToolInvocationStatus.NEEDS_REVIEW,
        observation_refs=(recorded.id, foreign.id),
        output={"raw_adapter_payload": "UNTRUSTED-SYNTHETIC-OUTPUT"},
    )
    state = await fixture.start()

    stopped = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    observations = await fixture.observations.list_for_investigation(state.investigation_id)
    assert stopped.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.NEEDS_REVIEW
    assert invocations[0].response_refs == (recorded.id,)
    assert observations == (recorded,)
    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)
    assert fixture.tools["vt.download_sample"].calls == [call["arguments"]]


@pytest.mark.asyncio
async def test_unreconciled_review_with_refs_blocks_next_external_tool_call() -> None:
    first = _tool_call("vt.search", "synthetic:ambiguous", "reuse the reviewed lookup")
    second = _tool_call("vt.search", "synthetic:later", "start another external lookup")
    fixture = _Fixture(
        responses=[
            _decision(
                goal="continue after reviewed lookup", next_action="continue", tools=(first, second)
            ),
            _decision(goal="finish after reconciliation gate", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256=hashlib.sha256(b"synthetic ambiguous lookup evidence").hexdigest(),
        source="synthetic-vt-search",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="search_result",
        object_id="synthetic:ambiguous",
    )
    await fixture.observations.append_if_absent(observation)
    operation_key, _ = _operation_and_observation_id(state.investigation_id, first)
    invocation = ToolInvocation(
        investigation_id=state.investigation_id,
        operation_key=operation_key,
        tool_name=first["tool_name"],
        schema_version=first["schema_version"],
        normalized_parameters=first["arguments"],
        purpose=first["purpose"],
    )
    invocation = await fixture.invocations.add_if_absent(invocation)
    invocation = await fixture.invocations.update_if_version(
        invocation.transition(ToolInvocationStatus.RUNNING), expected_version=invocation.version
    )
    reviewed = replace(
        invocation.transition(ToolInvocationStatus.NEEDS_REVIEW),
        response_refs=(observation.id,),
    )
    await fixture.invocations.update_if_version(reviewed, expected_version=invocation.version)

    result = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    assert result.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert fixture.tools["vt.search"].calls == []
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.NEEDS_REVIEW
    assert invocations[0].response_refs == (observation.id,)


@pytest.mark.asyncio
async def test_untyped_shodan_authorization_refusal_blocks_later_calls() -> None:
    calls = tuple(
        _tool_call("shodan.host", f"synthetic:{index}", f"check synthetic host {index}")
        for index in range(3)
    )
    fixture = _Fixture(
        responses=[
            _decision(goal="check three hosts", next_action="continue", tools=calls),
            _decision(goal="finish after provider refusal", next_action="finish"),
        ],
        tools=(("shodan.host", "shodan"),),
    )
    tool = fixture.tools["shodan.host"]
    original_execute = tool.execute

    async def refuse_first_two(arguments, context):
        tool.calls.append(dict(arguments))
        tool.contexts.append(context)
        fixture.execution_order.append(tool.name)
        if len(tool.calls) <= 2:
            return ToolResult(
                status=ToolInvocationStatus.UNAVAILABLE,
                output={"reason": "provider_authorization_refused"},
            )
        return await original_execute(arguments, context)

    tool.execute = refuse_first_two
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    assert result.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert len(tool.calls) == 1
    assert len(invocations) == 1
    assert invocations[0].status is ToolInvocationStatus.NEEDS_REVIEW


@pytest.mark.asyncio
async def test_different_tool_parameters_remain_distinct_operations() -> None:
    first = _tool_call("vt.search", "synthetic:first", "search first sample")
    second = _tool_call("vt.search", "synthetic:second", "search second sample")
    fixture = _Fixture(
        responses=[
            _decision(goal="search first sample", next_action="continue", tools=(first,)),
            _decision(goal="search second sample", next_action="continue", tools=(second,)),
            _decision(goal="finish", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    envelope = await fixture.envelopes.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 2
    assert envelope is not None
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("effect", "expected_invocations"),
    (
        (ToolEffect.EXTERNAL_READ, 1),
        (ToolEffect.EXTERNAL_SUBMIT, 2),
    ),
)
async def test_identical_operations_are_deduplicated_only_for_read_effects(
    effect: ToolEffect,
    expected_invocations: int,
) -> None:
    first = _tool_call("vt.synthetic", "synthetic:target", "inspect target")
    second = _tool_call("vt.synthetic", "synthetic:target", "inspect target again")
    fixture = _Fixture(
        responses=[
            _decision(goal="inspect the target", next_action="continue", tools=(first,)),
            _decision(goal="repeat the target operation", next_action="continue", tools=(second,)),
            _decision(goal="finish", next_action="finish"),
        ],
        tools=(("vt.synthetic", "vt", effect),),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.synthetic"].calls) == expected_invocations
    assert len(invocations) == expected_invocations
    assert len({item.operation_key for item in invocations}) == expected_invocations


@pytest.mark.asyncio
async def test_operator_view_proposes_advisor_checkpoint_as_nonblocking_action() -> None:
    fixture = _Fixture(responses=[], tools=())
    initial_state = await fixture.start()
    state = replace(
        initial_state,
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )
    investigation = await fixture.investigations.get(state.investigation_id)
    guidance = {
        "checkpoint_due": True,
        "reason": "tool_threshold_reached",
        "suggested_consultation_type": "pivot_strategy",
    }

    view = build_investigation_operator_view(
        investigation=investigation,
        state=state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=await fixture.envelopes.get(state.investigation_id),
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=("advisor.consult",),
        unavailable_tools=(),
        advisor_guidance=guidance,
    )

    action = view.payload["next_idempotent_action"]
    assert action["code"] == "advisor_checkpoint_due"
    assert action["blocking"] is False
    assert action["tool_name"] == "advisor.consult"
    assert action["arguments"] == {"consultation_type": "pivot_strategy"}
    assert action["requires_idempotency_key"] is True


@pytest.mark.asyncio
async def test_semantically_identical_findings_are_idempotent_and_counted() -> None:
    first_statement = "The loader  starts a child process."
    replay_statement = "The loader starts a child process."
    fixture = _Fixture(
        responses=[],
        tools=(),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="d" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="a" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    fixture.bridge_transport._responses.extend(
        [
            _decision(
                goal="record a finding",
                next_action="continue",
                findings=(
                    _proposal(
                        FindingKind.CAPABILITY.value,
                        FindingAssessment.QUALIFIED.value,
                        first_statement,
                        observation.id,
                        claim_refs=("C1",),
                    ),
                ),
            ),
            _decision(
                goal="repeat the same finding",
                next_action="continue",
                findings=(
                    _proposal(
                        FindingKind.CAPABILITY.value,
                        FindingAssessment.QUALIFIED.value,
                        replay_statement,
                        observation.id,
                        claim_refs=("C1",),
                    ),
                ),
            ),
            _decision(goal="finish", next_action="finish"),
        ]
    )

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    persisted_state = await fixture.states.get(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].statement == first_statement
    assert persisted_state.deduplicated_findings == 1
    investigation = await fixture.investigations.get(state.investigation_id)
    operator_view = build_investigation_operator_view(
        investigation=investigation,
        state=persisted_state,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=await fixture.envelopes.get(state.investigation_id),
        invocations=(),
        observations=(observation,),
        opinion=None,
        jobs=(),
        available_tools=(),
        unavailable_tools=(),
    )
    response = InvestigationOperatorStatusResponse.model_validate(operator_view.payload)
    assert response.deduplicated_findings == 1


@pytest.mark.asyncio
async def test_findings_with_same_references_but_different_statements_are_preserved() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="e" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="b" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    common = {
        "kind": FindingKind.CAPABILITY,
        "assessment": FindingAssessment.QUALIFIED,
        "observation_refs": (observation.id,),
        "claim_refs": ("C1",),
    }

    await fixture.service._persist_findings(
        investigation_id=state.investigation_id,
        proposals=(
            FindingProposalV1(statement="The loader starts a child process.", **common),
            FindingProposalV1(statement="The loader persists a service entry.", **common),
        ),
        decision_trace="two distinct synthetic findings",
    )

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert {item.statement for item in findings} == {
        "The loader starts a child process.",
        "The loader persists a service entry.",
    }


@pytest.mark.asyncio
async def test_finish_with_supported_findings_requires_a_versioned_opinion_draft() -> None:
    fixture = _Fixture(
        responses=[
            "@@DECISION@@\nGOAL: Finish the review.\nSUMMARY: Findings are ready.\n"
            "NEXT: finish\n@@END@@",
            "@@DECISION@@\nGOAL: Draft the conclusion.\nSUMMARY: The supported finding is cited.\n"
            "NEXT: finish\n@@END@@\n@@OPINION@@\nTYPE: key_findings\n"
            "TEXT: The supported observation shows the loader starts a child process.\n"
            "FINDINGS: {finding_id}\nEVIDENCE: {observation_id}\n"
            "QUALIFICATION: fact\nCERTAINTY: 0.8\n@@END@@",
        ],
        tools=(),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="c" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    finding = InvestigationFinding(
        investigation_id=state.investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The loader starts a child process.",
        observation_refs=(observation.id,),
        claim_refs=("C1",),
    )
    await fixture.findings.append_version(finding, expected_previous_id=None)
    fixture.bridge_transport._responses[1] = fixture.bridge_transport._responses[1].format(
        finding_id=finding.id, observation_id=observation.id
    )

    result = await fixture.service.run(state.investigation_id)

    opinion = await fixture.opinions.get(uuid5(_OPINION_NAMESPACE, str(state.investigation_id)))
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.bridge_transport.prompts) == 2
    assert "opinion_draft_required" in fixture.bridge_transport.prompts[1].lower()
    assert opinion is not None
    assert opinion.version == 1


@pytest.mark.asyncio
async def test_finish_with_qualified_findings_requires_a_cited_opinion_draft() -> None:
    fixture = _Fixture(
        responses=[],
        tools=(),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="e" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="f" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    finding = InvestigationFinding(
        investigation_id=state.investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.QUALIFIED,
        statement="The synthetic sample has a loader-like behavior, with limited scope.",
        observation_refs=(observation.id,),
        claim_refs=("C1",),
    )
    await fixture.findings.append_version(finding, expected_previous_id=None)
    finish = _decision(goal="Finish the qualified analysis.", next_action="finish")
    opinion = _opinion_delta(finding.id, observation.id)
    opinion["blocks"][0]["qualification"] = "inference"
    opinion["blocks"][0]["certainty"] = 0.4
    corrected = _decision(
        goal="Draft the qualified conclusion.", next_action="finish", opinion=opinion
    )
    fixture.bridge_transport._responses.extend([finish, corrected])

    result = await fixture.service.run(state.investigation_id)

    draft = await fixture.opinions.get(uuid5(_OPINION_NAMESPACE, str(state.investigation_id)))
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2
    assert "non-inconclusive findings" in fixture.bridge_transport.prompts[1].lower()
    assert draft is not None and draft.renderable_blocks
    assert draft.renderable_blocks[0].qualification is OpinionClaimQualification.INFERENCE


async def _inject_untested_rule_candidate(
    fixture: _Fixture,
    monkeypatch: pytest.MonkeyPatch,
    *,
    candidate_id: UUID,
    version: int = 2,
) -> None:
    original_build = fixture.context_builder.build
    candidate = {
        "kind": "rule_candidate",
        "candidate_id": str(candidate_id),
        "version": version,
        "version_id": str(uuid4()),
        "status": "active",
        "validation_status": "untested",
        "latest_test_report": None,
        "source_sha256": "a" * 64,
        "source_excerpt": "rule synthetic",
    }

    async def build(**kwargs: Any) -> Any:
        context = await original_build(**kwargs)
        investigation = context.investigation.model_copy(update={"rule_deltas": (candidate,)})
        return context.model_copy(update={"investigation": investigation})

    monkeypatch.setattr(fixture.context_builder, "build", build)


def _register_rule_test_tool(fixture: _Fixture) -> _FakeTool:
    tool = cast(_FakeTool, fixture.tools["rule.test"])
    assert fixture.registry.unregister("rule.test", "1")
    fixture.registry.register(
        ToolDefinition(
            name="rule.test",
            schema_version="1",
            input_schema={
                "type": "object",
                "properties": {
                    "candidate_id": {"type": "string", "format": "uuid"},
                    "version": {"type": "integer", "minimum": 1},
                },
                "required": ["candidate_id", "version"],
                "additionalProperties": False,
            },
            effect=ToolEffect.LOCAL_READ,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Synthetic rule test tool.",),
            capability_key="rule.read",
        ),
        tool,
    )
    return tool


@pytest.mark.asyncio
async def test_finish_requires_rule_test_for_available_untested_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture(
        responses=[],
        tools=(
            (
                "rule.test",
                "rule",
                ToolEffect.LOCAL_READ,
                "rule.read",
                {},
            ),
        ),
    )
    tool = _register_rule_test_tool(fixture)
    state = await fixture.start()
    candidate_id = uuid4()
    await _inject_untested_rule_candidate(fixture, monkeypatch, candidate_id=candidate_id)
    fixture.bridge_transport._responses.extend(
        [
            _decision(goal="Finish now.", next_action="finish"),
            _decision(
                goal="Test the untested rule candidate before finishing.",
                next_action="finish",
                tools=(
                    {
                        "tool_name": "rule.test",
                        "schema_version": "1",
                        "arguments": {
                            "candidate_id": str(candidate_id),
                            "version": 2,
                        },
                        "purpose": "test the listed untested candidate version",
                    },
                ),
            ),
        ]
    )

    result = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert len(tool.calls) == 1
    assert tool.calls[0] == {"candidate_id": str(candidate_id), "version": 2}
    assert len(fixture.bridge_transport.prompts) == 2
    assert "rule_test_required" in fixture.bridge_transport.prompts[1]
    assert any(item.status is ToolInvocationStatus.SUCCEEDED for item in invocations)


@pytest.mark.asyncio
async def test_finish_records_limit_when_untested_rule_tool_is_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture(
        responses=[_decision(goal="Finish with the available evidence.", next_action="finish")],
        tools=(),
    )
    state = await fixture.start()
    await _inject_untested_rule_candidate(fixture, monkeypatch, candidate_id=uuid4())

    result = await fixture.service.run(state.investigation_id)

    persisted = await fixture.states.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert persisted is not None
    assert any(action.startswith("rule_test_unavailable:") for action in persisted.recent_actions)


@pytest.mark.asyncio
async def test_finish_records_limit_when_rule_test_refuses_candidate(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fixture = _Fixture(
        responses=[],
        tools=(
            (
                "rule.test",
                "rule",
                ToolEffect.LOCAL_READ,
                "rule.read",
                {},
            ),
        ),
    )
    tool = _register_rule_test_tool(fixture)
    tool.result = ToolResult(
        status=ToolInvocationStatus.UNAVAILABLE,
        message="Synthetic rule test capability unavailable.",
    )
    state = await fixture.start()
    candidate_id = uuid4()
    await _inject_untested_rule_candidate(fixture, monkeypatch, candidate_id=candidate_id)
    fixture.bridge_transport._responses.append(
        _decision(
            goal="Attempt the listed rule test before finishing.",
            next_action="finish",
            tools=(
                {
                    "tool_name": "rule.test",
                    "schema_version": "1",
                    "arguments": {"candidate_id": str(candidate_id), "version": 2},
                    "purpose": "attempt the exact candidate version test",
                },
            ),
        )
    )

    result = await fixture.service.run(state.investigation_id)

    persisted = await fixture.states.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert persisted is not None
    assert any(action.startswith("rule_test_unavailable:") for action in persisted.recent_actions)


@pytest.mark.asyncio
async def test_finish_requires_analysis_of_an_acquired_sample_before_finishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    association_id = uuid4()
    sample_sha256 = "a" * 64
    fixture = _Fixture(
        responses=[],
        tools=(
            (
                "sample.analyze",
                "local",
                ToolEffect.LOCAL_COMPUTE,
                "investigation.sample_analysis",
                {},
            ),
        ),
    )
    state = await fixture.start()
    original_build = fixture.context_builder.build

    async def build(**kwargs: Any) -> Any:
        context = await original_build(**kwargs)
        invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
        analyzed = any(
            invocation.tool_name == "sample.analyze"
            and invocation.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
            for invocation in invocations
        )
        sample = {
            "association_id": str(association_id),
            "sample_id": str(uuid4()),
            "sha256": sample_sha256,
            "sha256_short": sample_sha256[:12],
            "byte_size": 128,
            "detected_type": None,
            "provenance": "vt_download",
            "qualification": "candidate",
            "analysis_status": "analyzed" if analyzed else "not_analyzed",
            "analysis_engines": ["static@fixture-v1"] if analyzed else [],
            "observation_ids": [str(invocation.id) for invocation in invocations if analyzed],
        }
        investigation = context.investigation.model_copy(update={"sample_deltas": (sample,)})
        return context.model_copy(update={"investigation": investigation})

    monkeypatch.setattr(fixture.context_builder, "build", build)
    unsupported_blocked_reason = _decision(goal="Finish the analysis.", next_action="finish")
    unsupported_blocked_reason["reasoning_summary"] = "BLOCKED_REASON: engine_unavailable"
    fixture.bridge_transport._responses.extend(
        [
            unsupported_blocked_reason,
            _decision(
                goal="Analyze the downloaded sample, then finish.",
                next_action="finish",
                tools=(
                    {
                        "tool_name": "sample.analyze",
                        "schema_version": "1",
                        "arguments": {
                            "association_id": str(association_id),
                            "query": "analyze the acquired association",
                        },
                        "purpose": "run local static analysis on the acquired sample",
                    },
                ),
            ),
        ]
    )

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2
    assert len(fixture.tools["sample.analyze"].calls) == 1
    assert str(association_id) in fixture.bridge_transport.prompts[0]
    assert "finish_gate" in fixture.bridge_transport.prompts[1]


@pytest.mark.asyncio
async def test_finish_gate_accepts_engine_unavailable_only_after_tool_reports_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    association_id = uuid4()
    sample_sha256 = "c" * 64
    fixture = _Fixture(
        responses=[],
        tools=(
            (
                "sample.analyze",
                "local",
                ToolEffect.LOCAL_COMPUTE,
                "investigation.sample_analysis",
                {},
            ),
        ),
    )
    state = await fixture.start()
    unavailable = ToolInvocation(
        investigation_id=state.investigation_id,
        operation_key="sample-analysis-unavailable",
        tool_name="sample.analyze",
        schema_version="1",
        normalized_parameters={"association_id": str(association_id)},
        purpose="Attempt local analysis of the quarantined sample.",
        status=ToolInvocationStatus.UNAVAILABLE,
    )
    await fixture.invocations.add_if_absent(unavailable)
    original_build = fixture.context_builder.build

    async def build(**kwargs: Any) -> Any:
        context = await original_build(**kwargs)
        sample = {
            "association_id": str(association_id),
            "sample_id": str(uuid4()),
            "sha256": sample_sha256,
            "sha256_short": sample_sha256[:12],
            "byte_size": 64,
            "detected_type": None,
            "provenance": "vt_download",
            "qualification": "candidate",
            "analysis_status": "not_analyzed",
            "analysis_engines": [],
            "observation_ids": [],
        }
        return context.model_copy(
            update={
                "investigation": context.investigation.model_copy(
                    update={"sample_deltas": (sample,)}
                )
            }
        )

    monkeypatch.setattr(fixture.context_builder, "build", build)
    blocked_finish = _decision(
        goal="Finish with an explicit engine limitation.", next_action="finish"
    )
    blocked_finish["reasoning_summary"] = "BLOCKED_REASON: engine_unavailable"
    fixture.bridge_transport._responses.append(blocked_finish)

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 1
    assert not any(action.startswith("finish_gate_refused:") for action in result.recent_actions)


@pytest.mark.asyncio
async def test_finish_gate_allows_finish_after_bounded_consecutive_refusals(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    association_id = uuid4()
    fixture = _Fixture(
        responses=[],
        tools=(
            (
                "sample.analyze",
                "local",
                ToolEffect.LOCAL_COMPUTE,
                "investigation.sample_analysis",
                {},
            ),
        ),
    )
    state = await fixture.start()
    original_build = fixture.context_builder.build

    async def build(**kwargs: Any) -> Any:
        context = await original_build(**kwargs)
        sample = {
            "association_id": str(association_id),
            "sample_id": str(uuid4()),
            "sha256": "b" * 64,
            "sha256_short": "b" * 12,
            "byte_size": 32,
            "detected_type": None,
            "provenance": "vt_download",
            "qualification": "candidate",
            "analysis_status": "not_analyzed",
            "analysis_engines": [],
            "observation_ids": [],
        }
        return context.model_copy(
            update={
                "investigation": context.investigation.model_copy(
                    update={"sample_deltas": (sample,)}
                )
            }
        )

    monkeypatch.setattr(fixture.context_builder, "build", build)
    fixture.bridge_transport._responses.extend(
        _decision(goal=f"Finish attempt {index}.", next_action="finish") for index in range(3)
    )

    result = await fixture.service.run(state.investigation_id)

    persisted = await fixture.states.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 3
    assert persisted is not None
    assert (
        sum(action.startswith("finish_gate_refused:") for action in persisted.recent_actions) == 2
    )


@pytest.mark.asyncio
async def test_finish_without_findings_does_not_require_an_opinion() -> None:
    fixture = _Fixture(
        responses=[_decision(goal="Finish the empty investigation.", next_action="finish")],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 1
    assert (
        await fixture.opinions.get(uuid5(_OPINION_NAMESPACE, str(state.investigation_id))) is None
    )


@pytest.mark.asyncio
async def test_rejected_opinion_attempt_requires_correction_even_with_existing_draft() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="d" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="a" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    finding = InvestigationFinding(
        investigation_id=state.investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The synthetic loader starts a child process.",
        observation_refs=(observation.id,),
        claim_refs=("C1",),
    )
    await fixture.findings.append_version(finding, expected_previous_id=None)
    opinion_id = uuid5(_OPINION_NAMESPACE, str(state.investigation_id))
    prior_opinion = AnalystOpinionV1(
        id=opinion_id,
        version=1,
        investigation_id=state.investigation_id,
        synthesis_artifact_id=fixture.synthesis.id,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        base_publication_artifact_id=fixture.publication_id,
        base_publication_sha256=fixture.snapshot.base_publication_sha256,
        blocks=(
            OpinionBlock(
                block_type=OpinionBlockType.KEY_FINDINGS,
                text="The existing synthetic observation supports this conclusion.",
                finding_refs=(finding.id,),
                evidence_refs=(observation.id,),
                qualification=OpinionClaimQualification.FACT,
                certainty=0.9,
            ),
        ),
        author_id="synthetic-analyst",
        status=AnalystOpinionStatus.DRAFT,
    )
    await fixture.opinions.append_version(prior_opinion, expected_version=0)
    rejected_opinion = {
        "blocks": [
            {
                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                "text": "An unsupported synthetic conclusion.",
                "finding_refs": [str(uuid4())],
                "evidence_refs": [str(observation.id)],
                "qualification": OpinionClaimQualification.FACT.value,
                "certainty": 0.8,
            }
        ],
        "unresolved_points": [],
    }
    fixture.bridge_transport._responses.extend(
        [
            _decision(
                goal="Replace the draft with an unsupported claim.",
                next_action="finish",
                opinion=rejected_opinion,
            ),
            _decision(goal="Finish with the existing reviewed evidence.", next_action="finish"),
        ]
    )

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2
    assert "opinion_validation_rejected" in fixture.bridge_transport.prompts[1]


@pytest.mark.asyncio
async def test_notes_are_visible_deduplicated_by_anchor_and_replaced_in_place() -> None:
    fixture = _Fixture(
        responses=[],
        tools=(),
        publication_anchors=(
            PublicationAnchorV1(anchor="lead:0001", excerpt="Synthetic anchored paragraph."),
        ),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="c" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="b" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    finding = InvestigationFinding(
        investigation_id=state.investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="A synthetic loader uses the observed capability.",
        observation_refs=(observation.id,),
        claim_refs=("C1",),
    )
    await fixture.findings.append_version(finding, expected_previous_id=None)

    def response(*, next_action: str, note_text: str, relation: str) -> str:
        return "\n".join(
            (
                "@@DECISION@@",
                "GOAL: Maintain the anchored synthetic observation.",
                "SUMMARY: Keep the note tied to its exact publication paragraph.",
                f"NEXT: {next_action}",
                "@@END@@",
                "@@OPINION@@",
                "TYPE: key_findings",
                "TEXT: The synthetic observation supports one loader capability.",
                f"FINDINGS: {finding.id}",
                f"EVIDENCE: {observation.id}",
                "QUALIFICATION: fact",
                "CERTAINTY: 0.9",
                "@@END@@",
                "@@NOTE@@",
                "ANCHOR: lead:0001",
                "STATE: observed",
                f"RELATION: {relation}",
                f"TEXT: {note_text}",
                f"FINDINGS: {finding.id}",
                f"EVIDENCE: {observation.id}",
                "@@END@@",
            )
        )

    fixture.bridge_transport._responses.extend(
        [
            response(
                next_action="continue",
                note_text="The sample shows a loader.",
                relation="confirms",
            ),
            response(
                next_action="continue",
                note_text="The sample shows a loader.",
                relation="qualifies",
            ),
            response(
                next_action="finish",
                note_text="The sample shows one loader stage.",
                relation="qualifies",
            ),
        ]
    )

    result = await _compose_fixture_service(fixture).run(state.investigation_id)

    opinion = await fixture.opinions.get(uuid5(_OPINION_NAMESPACE, str(state.investigation_id)))
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 3
    assert "The sample shows a loader." in fixture.bridge_transport.prompts[1]
    assert "Do not repeat an existing note" in fixture.bridge_transport.prompts[1]
    assert opinion is not None
    assert len(opinion.observations) == 1
    assert opinion.observations[0].text == "The sample shows one loader stage."


@pytest.mark.asyncio
async def test_compact_bridge_turn_persists_deduplicated_findings_opinion_and_note() -> None:
    fixture = _Fixture(
        responses=[],
        tools=(),
        publication_anchors=(
            PublicationAnchorV1(anchor="lead:0001", excerpt="The report documents the loader."),
        ),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="a" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="d" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    finding = InvestigationFinding(
        investigation_id=state.investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="The synthetic loader starts a child process.",
        observation_refs=(observation.id,),
        claim_refs=("C1",),
    )
    await fixture.findings.append_version(finding, expected_previous_id=None)
    fixture.bridge_transport._responses.append(
        "\n".join(
            (
                "@@DECISION@@",
                "GOAL: Summarize the documented loader behavior.",
                "SUMMARY: Two equivalent proposals refer to one supported finding.",
                "NEXT: finish",
                "@@END@@",
                "@@FINDING@@",
                "KIND: capability",
                "ASSESSMENT: supported",
                "STATEMENT: The synthetic loader starts a child process.",
                f"SUBJECT_REF: {observation.object_id}",
                f"OBSERVATIONS: {observation.id}",
                "CLAIMS: C1",
                "@@END@@",
                "@@FINDING@@",
                "KIND: capability",
                "ASSESSMENT: supported",
                "STATEMENT: The synthetic loader starts a   child process.",
                f"SUBJECT_REF: {observation.object_id}",
                f"OBSERVATIONS: {observation.id}",
                "CLAIMS: C1",
                "@@END@@",
                "@@OPINION@@",
                "TYPE: key_findings",
                "TEXT: The synthetic observation supports the loader capability.",
                f"FINDINGS: {finding.id}",
                f"EVIDENCE: {observation.id}",
                "QUALIFICATION: fact",
                "CERTAINTY: 0.8",
                "@@END@@",
                "@@NOTE@@",
                "ANCHOR: lead:0001",
                "STATE: observed",
                "RELATION: confirms",
                "TEXT: The loader capability is supported by the cited observation.",
                f"FINDINGS: {finding.id}",
                f"EVIDENCE: {observation.id}",
                "@@END@@",
                "@@NOTE@@",
                "ANCHOR: lead:9999",
                "STATE: observed",
                "RELATION: confirms",
                "TEXT: An unknown synthetic anchor must be rejected.",
                f"FINDINGS: {finding.id}",
                f"EVIDENCE: {observation.id}",
                "@@END@@",
            )
        )
    )

    composition_service = _compose_fixture_service(fixture)
    result = await composition_service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    opinion = await fixture.opinions.get(uuid5(_OPINION_NAMESPACE, str(state.investigation_id)))
    persisted_state = await fixture.states.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert "lead:0001" in fixture.bridge_transport.prompts[0]
    assert len(findings) == 1
    assert findings[0].id == finding.id
    assert persisted_state.deduplicated_findings == 2
    assert opinion is not None
    assert opinion.version == 1
    assert sum(block.block_type is OpinionBlockType.KEY_FINDINGS for block in opinion.blocks) == 1
    assert len(opinion.observations) == 1
    assert opinion.observations[0].anchor == "lead:0001"
    assert any(
        action.startswith("note_validation_rejected:") for action in persisted_state.recent_actions
    )


@pytest.mark.asyncio
async def test_finish_opinion_correction_loop_stops_after_bounded_retries() -> None:
    finish_response = (
        "@@DECISION@@\nGOAL: Finish the review.\nSUMMARY: Findings are ready.\n"
        "NEXT: finish\n@@END@@"
    )
    fixture = _Fixture(responses=[finish_response] * 4, tools=())
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.VT_FILE_REPORT,
        blob_id=uuid4(),
        sha256="b" * 64,
        source="vt.file_report",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="file",
        object_id="e" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    await fixture.findings.append_version(
        InvestigationFinding(
            investigation_id=state.investigation_id,
            kind=FindingKind.CAPABILITY,
            assessment=FindingAssessment.SUPPORTED,
            statement="A synthetic loader starts a child process.",
            observation_refs=(observation.id,),
        ),
        expected_previous_id=None,
    )

    result = await fixture.service.run(state.investigation_id)

    persisted_state = await fixture.states.get(state.investigation_id)
    assert result.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert len(fixture.bridge_transport.prompts) == 3
    assert (
        sum(
            action.startswith("opinion_draft_required:")
            for action in persisted_state.recent_actions
        )
        == 2
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "terminal_status",
    [ToolInvocationStatus.EMPTY, ToolInvocationStatus.UNAVAILABLE],
)
async def test_final_empty_or_unavailable_result_is_replayed_without_another_call(
    terminal_status: ToolInvocationStatus,
) -> None:
    first = _tool_call("vt.search", "synthetic:terminal", "check terminal sample result")
    second = _tool_call("vt.search", "synthetic:terminal", "ask about same terminal result")
    fixture = _Fixture(
        responses=[
            _decision(goal="check the sample", next_action="continue", tools=(first,)),
            _decision(goal="reuse the result", next_action="continue", tools=(second,)),
            _decision(goal="finish", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    tool = fixture.tools["vt.search"]

    async def terminal_execute(arguments, context):
        tool.calls.append(dict(arguments))
        tool.contexts.append(context)
        fixture.execution_order.append(tool.name)
        output = (
            {"reason": "not_configured"}
            if terminal_status is ToolInvocationStatus.UNAVAILABLE
            else {"summary": "Synthetic provider reported no matching records."}
        )
        return ToolResult(status=terminal_status, output=output)

    tool.execute = terminal_execute
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    expected_stop = (
        StopReason.SUBMISSION_NEEDS_REVIEW
        if terminal_status is ToolInvocationStatus.UNAVAILABLE
        else StopReason.AGENT_FINISHED
    )
    assert result.stop_reason is expected_stop
    assert len(tool.calls) == 1
    assert len(invocations) == 1
    expected_status = (
        ToolInvocationStatus.NEEDS_REVIEW
        if terminal_status is ToolInvocationStatus.UNAVAILABLE
        else terminal_status
    )
    assert invocations[0].status is expected_status
    if terminal_status is ToolInvocationStatus.EMPTY:
        assert "already executed" in fixture.bridge_transport.prompts[2].lower()


@pytest.mark.asyncio
async def test_untyped_transient_failure_waits_for_reconciliation() -> None:
    call = _tool_call("vt.search", "synthetic:retry", "retry a rejected local attempt")
    fixture = _Fixture(
        responses=[
            _decision(goal="try the sample lookup", next_action="continue", tools=(call,)),
            _decision(goal="retry the rejected lookup", next_action="continue", tools=(call,)),
            _decision(goal="finish", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    tool = fixture.tools["vt.search"]
    original_execute = tool.execute
    attempts = 0

    async def fail_once(arguments, context):
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return ToolResult(
                status=ToolInvocationStatus.FAILED,
                output={"reason": "subject_slot_limit"},
            )
        return await original_execute(arguments, context)

    tool.execute = fail_once
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    envelope = await fixture.envelopes.get(state.investigation_id)
    assert result.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert attempts == 1
    assert tool.calls == []
    assert [item.status for item in invocations] == [ToolInvocationStatus.NEEDS_REVIEW]
    assert len({item.operation_key for item in invocations}) == 1
    assert envelope is not None
    assert envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    reservation = next(
        item
        for item in envelope.budget.reservations
        if item.operation_key == invocations[0].operation_key
    )
    assert reservation.status is BudgetReservationStatus.RESERVED


@pytest.mark.asyncio
async def test_tool_outside_envelope_is_denied_with_alternative_and_budget_release() -> None:
    denied = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="search VT", next_action="continue", tools=(denied,)),
            _decision(goal="finish with network alternative", next_action="finish"),
        ],
        tools=(("vt.search", "vt"), ("network.lookup", "network")),
    )
    state = await fixture.start()
    envelope = await fixture.envelopes.get(state.investigation_id)
    assert envelope is not None
    restricted = replace(
        envelope,
        allowed_providers=("network",),
        version=envelope.version + 1,
    )
    await fixture.envelopes.save_if_version(restricted, expected_version=envelope.version)

    result = await fixture.service.run(state.investigation_id)

    operation_key = tool_operation_key(
        investigation_id=state.investigation_id,
        tool_name=denied["tool_name"],
        schema_version=denied["schema_version"],
        normalized_parameters=denied["arguments"],
        purpose=denied["purpose"],
    )
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    latest_envelope = await fixture.envelopes.get(state.investigation_id)
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.POLICY_DENIED
    assert latest_envelope is not None
    assert (
        next(
            item
            for item in latest_envelope.budget.reservations
            if item.operation_key == operation_key
        ).status.value
        == "released"
    )
    assert "network.lookup" in fixture.bridge_transport.prompts[1]


@pytest.mark.asyncio
async def test_observation_archive_failure_requires_review_without_tool_replay() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="check payload", next_action="finish", tools=(call,)),
            _decision(goal="do not repeat ambiguous tool", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    fixture.service._observation_writer = _FailingObservationWriter()
    state = await fixture.start()

    first = await fixture.service.run(state.investigation_id)
    pending_state = await fixture.states.get(state.investigation_id)
    assert pending_state is not None
    assert pending_state.pending_context_snapshot_id is not None
    pending_json = await fixture.context_snapshots.load(pending_state.pending_context_snapshot_id)
    assert pending_json is not None
    assert hashlib.sha256(pending_json.encode()).hexdigest() == pending_state.pending_context_sha256
    assert len(pending_state.canonical_bytes()) < len(pending_json.encode())
    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert invocation is not None
    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)
    resolved = await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.ABANDON,
        actor="analyst-1",
        reason="The execution result cannot be recovered; close the ambiguity.",
        expected_version=invocation.version,
        idempotency_key="reconcile-ambiguous-call",
    )
    assert resolved.status is ToolInvocationStatus.FAILED
    replay = await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.ABANDON,
        actor="analyst-1",
        reason="The execution result cannot be recovered; close the ambiguity.",
        expected_version=invocation.version,
        idempotency_key="reconcile-ambiguous-call",
    )
    assert replay.version == resolved.version
    assert len(fixture.analyst_decisions.items) == 1
    await fixture.service.resume(state.investigation_id)
    second = await fixture.service.run(state.investigation_id)

    assert first.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert second.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 1


@pytest.mark.asyncio
async def test_running_invocation_is_committed_before_dispatch_and_never_replayed() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    decision = _decision(goal="check payload", next_action="continue", tools=(call,))
    fixture = _Fixture(
        responses=[decision, decision, _decision(goal="finish", next_action="finish")],
        tools=(("vt.search", "vt"),),
    )
    fixture.service._dispatcher = _CrashAfterDispatch(fixture.service._dispatcher)
    state = await fixture.start()
    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)

    with pytest.raises(RuntimeError, match="simulated worker crash after provider call"):
        await fixture.service.run(state.investigation_id)

    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.RUNNING
    assert operation_key in fixture.committed_running_keys
    assert fixture.tools["vt.search"].calls == [call["arguments"]]
    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)

    ambiguous = await fixture.service.run(state.investigation_id)
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert ambiguous.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)

    await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.ABANDON,
        actor="analyst-1",
        reason="The provider may have executed the operation.",
        expected_version=invocation.version,
        idempotency_key="crash-after-dispatch",
    )
    envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in envelope.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.status.value == "committed"
    assert reservation.committed_amounts[BudgetDimension.VT_UNITS] == 1
    await fixture.service.resume(state.investigation_id)
    finished = await fixture.service.run(state.investigation_id)

    assert finished.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 1
    assert fixture.tools["vt.search"].contexts[0].idempotency_key == operation_key
    assert (
        len([item for item in envelope.budget.reservations if item.operation_key == operation_key])
        == 1
    )


@pytest.mark.asyncio
async def test_interrupted_running_invocation_requires_reconciliation_before_progress() -> None:
    ambiguous_call = _tool_call("vt.search", "synthetic:stopped", "check the interrupted lookup")
    later_call = _tool_call("network.lookup", "synthetic:later", "continue after reconciliation")
    fixture = _Fixture(
        responses=[
            _decision(goal="start the lookup", next_action="continue", tools=(ambiguous_call,)),
            _decision(
                goal="continue after reconciliation", next_action="continue", tools=(later_call,)
            ),
            _decision(goal="finish", next_action="finish"),
        ],
        tools=(("vt.search", "vt"), ("network.lookup", "network")),
    )
    state = await fixture.start()
    operation_key, _ = _operation_and_observation_id(state.investigation_id, ambiguous_call)
    original_dispatcher = fixture.service._dispatcher

    class _CrashBeforeDispatch:
        async def dispatch(self, *_args, **_kwargs):
            raise RuntimeError("simulated crash after durable RUNNING journal")

    fixture.service._dispatcher = _CrashBeforeDispatch()
    with pytest.raises(RuntimeError, match="durable RUNNING journal"):
        await fixture.service.run(state.investigation_id)

    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.RUNNING
    assert fixture.tools["vt.search"].calls == []
    stopped = await fixture.service.interrupt(state.investigation_id)
    assert stopped.stop_reason is StopReason.USER_INTERRUPT
    fixture.service._dispatcher = original_dispatcher

    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert fixture.tools["vt.search"].calls == []

    await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.NOT_EXECUTED,
        actor="analyst-1",
        reason="The worker crashed before entering the adapter.",
        expected_version=invocation.version,
        idempotency_key="stopped-running-not-executed",
    )
    await fixture.service.resume(state.investigation_id)
    completed = await fixture.service.run(state.investigation_id)

    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.tools["vt.search"].calls == []
    assert fixture.tools["network.lookup"].calls == [later_call["arguments"]]
    reconciled = await fixture.invocations.get_by_operation_key(operation_key)
    assert reconciled is not None
    assert reconciled.status is ToolInvocationStatus.FAILED


@pytest.mark.asyncio
async def test_framework_budget_refusal_resumes_after_budget_extension_without_duplicate_call() -> (
    None
):
    call = _tool_call("vt.search", "synthetic:budget", "query within the approved budget")
    fixture = _Fixture(
        responses=[
            _decision(goal="query the sample", next_action="continue", tools=(call,)),
            _decision(goal="retry after budget extension", next_action="continue", tools=(call,)),
            _decision(goal="finish", next_action="finish"),
            _decision(goal="replay the successful query", next_action="continue", tools=(call,)),
            _decision(goal="finish after replay", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    fixture.envelope = replace(
        fixture.envelope,
        budget=InvestigationBudget(
            limits={
                **fixture.envelope.budget.limits,
                BudgetDimension.VT_UNITS: 0,
            }
        ),
    )
    state = await fixture.start()

    refused = await fixture.service.run(state.investigation_id)

    assert refused.stop_reason is StopReason.PERMISSION_REQUIRED
    assert fixture.tools["vt.search"].calls == []
    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    assert len(invocations) == 1
    refusal = invocations[0]
    assert refusal.status is ToolInvocationStatus.NEEDS_REVIEW
    assert len(refusal.response_refs) == 1
    proof_observation = await fixture.observations.get(refusal.response_refs[0])
    assert proof_observation is not None
    assert proof_observation.object_type == FrameworkPreSubmissionProof.BUDGET_REJECTED.value
    envelope = await fixture.envelopes.get(state.investigation_id)
    assert envelope is not None
    refusal_reservation = next(
        item for item in envelope.budget.reservations if item.operation_key == refusal.operation_key
    )
    assert refusal_reservation.status is BudgetReservationStatus.RELEASED

    extended_budget = InvestigationBudget(
        limits={**envelope.budget.limits, BudgetDimension.VT_UNITS: 1}
    )
    for reservation in envelope.budget.reservations:
        extended_budget.restore_reservation(reservation)
    await fixture.envelopes.save_if_version(
        replace(envelope, budget=extended_budget, version=envelope.version + 1),
        expected_version=envelope.version,
    )
    assert fixture.tools["vt.search"].calls == [], "no adapter call may precede the extension"

    await fixture.service.resume(state.investigation_id)
    refusal = await fixture.invocations.get_by_operation_key(refusal.operation_key)
    assert refusal is not None
    assert refusal.status is ToolInvocationStatus.FAILED
    completed = await fixture.service.run(state.investigation_id)
    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.tools["vt.search"].calls == [call["arguments"]]

    await fixture.service.resume(state.investigation_id)
    replayed = await fixture.service.run(state.investigation_id)
    assert replayed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.tools["vt.search"].calls == [call["arguments"]]
    invocations = await fixture.invocations.list_for_investigation(state.investigation_id)
    assert len(invocations) == 2
    assert [item.status for item in invocations] == [
        ToolInvocationStatus.FAILED,
        ToolInvocationStatus.SUCCEEDED,
    ]
    assert invocations[0].operation_key != invocations[1].operation_key
    assert invocations[1].operation_key.startswith(f"{invocations[0].operation_key}:retry:1")


@pytest.mark.parametrize(
    ("corruption", "expected_error"),
    (
        ("tampered_bytes", "framework_proof_blob_hash_mismatch"),
        ("missing_blob", "framework_proof_blob_unreadable"),
        ("descriptor_hash_mismatch", "framework_proof_descriptor_mismatch"),
        ("descriptor_size_overflow", "framework_proof_blob_size_overflow"),
        ("mime_mismatch", "framework_proof_descriptor_mismatch"),
        ("read_limit_overflow", "framework_proof_blob_size_overflow"),
        ("invalid_json", "framework_proof_payload_invalid_json"),
        ("noncanonical_json", "framework_proof_payload_not_canonical"),
        ("payload_tool_mismatch", "framework_proof_payload_mismatch"),
        ("payload_schema_mismatch", "framework_proof_payload_mismatch"),
        ("payload_version_mismatch", "framework_proof_payload_mismatch"),
        ("payload_operation_mismatch", "framework_proof_payload_mismatch"),
        ("proof_object_mismatch", "framework_proof_payload_mismatch"),
    ),
)
@pytest.mark.asyncio
async def test_framework_budget_refusal_stays_blocked_when_proof_blob_is_invalid(
    corruption, expected_error
) -> None:
    call = _tool_call("vt.search", "synthetic:invalid-proof", "query within the approved budget")
    fixture = _Fixture(
        responses=[
            _decision(goal="query the sample", next_action="continue", tools=(call,)),
            _decision(goal="retry after budget extension", next_action="continue", tools=(call,)),
            _decision(goal="finish", next_action="finish"),
        ],
        tools=(("vt.search", "vt"),),
    )
    fixture.envelope = replace(
        fixture.envelope,
        budget=InvestigationBudget(
            limits={
                **fixture.envelope.budget.limits,
                BudgetDimension.VT_UNITS: 0,
            }
        ),
    )
    state = await fixture.start()
    stopped = await fixture.service.run(state.investigation_id)
    assert stopped.stop_reason is StopReason.PERMISSION_REQUIRED
    assert fixture.tools["vt.search"].calls == []

    refusal = (await fixture.invocations.list_for_investigation(state.investigation_id))[0]
    proof_observation = await fixture.observations.get(refusal.response_refs[0])
    assert proof_observation is not None
    assert proof_observation.object_type == FrameworkPreSubmissionProof.BUDGET_REJECTED.value
    if corruption == "tampered_bytes":
        stored = fixture.proof_artifacts.blobs[proof_observation.blob_id]
        fixture.proof_artifacts.blobs[proof_observation.blob_id] = (
            bytes((stored[0] ^ 1,)) + stored[1:]
        )
    elif corruption == "missing_blob":
        fixture.proof_artifacts.blobs.pop(proof_observation.blob_id)
    elif corruption == "descriptor_hash_mismatch":
        fixture.proof_artifacts.descriptors[proof_observation.blob_id]["sha256"] = "0" * 64
    elif corruption == "descriptor_size_overflow":
        fixture.proof_artifacts.descriptors[proof_observation.blob_id]["byte_size"] = 64 * 1024 + 1
    elif corruption == "mime_mismatch":
        fixture.proof_artifacts.descriptors[proof_observation.blob_id]["mime_type"] = (
            "application/octet-stream"
        )
    elif corruption == "read_limit_overflow":
        fixture.proof_artifacts.read_limit_errors.add(proof_observation.blob_id)
    elif corruption == "invalid_json":
        _replace_framework_proof_blob(fixture, proof_observation, b"not-json")
    elif corruption == "noncanonical_json":
        stored = fixture.proof_artifacts.blobs[proof_observation.blob_id]
        _replace_framework_proof_blob(fixture, proof_observation, b" " + stored)
    else:
        stored = fixture.proof_artifacts.blobs[proof_observation.blob_id]
        payload = json.loads(stored)
        if corruption == "payload_tool_mismatch":
            payload["tool_name"] = "vt.other_tool"
        elif corruption == "payload_schema_mismatch":
            payload["schema"] = "wrong-schema"
        elif corruption == "payload_version_mismatch":
            payload["schema_version"] = "wrong-version"
        elif corruption == "payload_operation_mismatch":
            payload["operation_key"] = "wrong-operation"
        else:
            payload["result"]["framework_pre_submission_proof"]["object_type"] = (
                "framework_pre_submission_invalid_arguments"
            )
        _replace_framework_proof_blob(fixture, proof_observation, canonical_json_bytes(payload))

    operator_invocations = OperatorToolInvocationService(
        investigation_id=state.investigation_id,
        investigations=fixture.investigations,
        states=fixture.states,
        envelopes=fixture.envelopes,
        invocations=fixture.invocations,
        context_builder=fixture.context_builder,
        execution=fixture.service,
        registry=fixture.registry,
        available_capabilities=fixture.available_capabilities,
        unavailable_tool_reasons={},
        observations=fixture.observations,
        evidence_summaries=fixture.proof_artifacts,
    )
    operator_proof, operator_rejection = await operator_invocations._framework_pre_submission_proof(
        refusal
    )
    assert operator_proof is None
    assert operator_rejection == expected_error

    envelope = await fixture.envelopes.get(state.investigation_id)
    assert envelope is not None
    extended_budget = InvestigationBudget(
        limits={**envelope.budget.limits, BudgetDimension.VT_UNITS: 1}
    )
    for reservation in envelope.budget.reservations:
        extended_budget.restore_reservation(reservation)
    await fixture.envelopes.save_if_version(
        replace(envelope, budget=extended_budget, version=envelope.version + 1),
        expected_version=envelope.version,
    )

    with pytest.raises(ValueError, match=expected_error):
        await fixture.service.resume(state.investigation_id)

    still_blocked = await fixture.invocations.get_by_operation_key(refusal.operation_key)
    assert still_blocked is not None
    assert still_blocked.status is ToolInvocationStatus.NEEDS_REVIEW
    blocked_run = await fixture.service.run(state.investigation_id)
    assert blocked_run.stop_reason is StopReason.PERMISSION_REQUIRED
    assert fixture.tools["vt.search"].calls == [], "invalid proof must not permit later dispatch"


@pytest.mark.asyncio
async def test_interrupt_during_bridge_wait_preserves_turn_for_resume() -> None:
    call = _tool_call("vt.search", "content:resume", "check the pending payload")
    fixture = _Fixture(
        responses=[_decision(goal="check payload", next_action="finish", tools=(call,))],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    entered = asyncio.Event()
    release = asyncio.Event()
    original_submit = fixture.bridge_transport.submit
    first_submit = True

    async def delayed_submit(**kwargs):
        nonlocal first_submit
        if first_submit:
            first_submit = False
            entered.set()
            await release.wait()
        return await original_submit(**kwargs)

    fixture.bridge_transport.submit = delayed_submit
    worker = asyncio.create_task(fixture.service.run(state.investigation_id))
    await asyncio.wait_for(entered.wait(), timeout=2)

    stopped = await fixture.service.interrupt(state.investigation_id)
    assert stopped.stop_reason is StopReason.USER_INTERRUPT
    assert stopped.pending_context_snapshot_id is not None
    release.set()
    after_interrupt = await worker

    assert after_interrupt.stop_reason is StopReason.USER_INTERRUPT
    assert fixture.tools["vt.search"].calls == []
    assert fixture.bridge_transport.submission_count == 1

    await fixture.service.resume(state.investigation_id)
    resumed = await fixture.service.run(state.investigation_id)

    assert resumed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 1
    assert fixture.tools["vt.search"].calls == [call["arguments"]]


@pytest.mark.asyncio
async def test_cancelled_bridge_wait_commits_budget_reservation() -> None:
    fixture = _Fixture(responses=[_decision(goal="synthetic question", next_action="finish")])
    state = await fixture.start()
    entered = asyncio.Event()

    async def blocked_submit(**_kwargs):
        entered.set()
        await asyncio.Event().wait()

    fixture.bridge_transport.submit = blocked_submit
    worker = asyncio.create_task(fixture.service.run(state.investigation_id))
    await asyncio.wait_for(entered.wait(), timeout=2)
    await fixture.service.interrupt(state.investigation_id)
    worker.cancel()
    with pytest.raises(asyncio.CancelledError):
        await worker

    envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item
        for item in envelope.budget.reservations
        if item.operation_key.startswith(f"bridge:{state.investigation_id}:")
    )
    assert reservation.status is BudgetReservationStatus.COMMITTED
    assert reservation.committed_amounts[BudgetDimension.BRIDGE_TURNS] == 1
    assert envelope.budget.reserved[BudgetDimension.BRIDGE_TURNS] == 0


@pytest.mark.asyncio
async def test_resume_seals_orphaned_turn_before_clearing_pending_checkpoint() -> None:
    fixture = _Fixture(responses=[])
    state = await fixture.start()

    class _RecoveringTransport(ScriptedTurnTransport):
        def __init__(self):
            super().__init__([])
            self.recovery_calls = []

        async def reconcile_orphaned_investigation_turn(self, **kwargs):
            self.recovery_calls.append(kwargs)
            current = await fixture.states.get(state.investigation_id)
            updated = replace(
                current,
                version=current.version + 1,
                recent_actions=(
                    *current.recent_actions,
                    "bridge_turn_orphaned:0:next_attempt=1:turn_id=synthetic-turn",
                ),
            )
            await fixture.states.save_if_version(updated, expected_version=current.version)
            return "orphaned_running_turn"

    transport = _RecoveringTransport()
    fixture.service._bridge = BridgeInvestigator(transport)
    pending = replace(
        state,
        version=state.version + 1,
        pending_context_snapshot_id=uuid4(),
        pending_context_sha256="d" * 64,
        stop_reason=StopReason.USER_INTERRUPT,
    )
    await fixture.states.save_if_version(pending, expected_version=state.version)
    envelope = await fixture.envelopes.get(state.investigation_id)
    orphaned_budget = InvestigationBudget(limits=envelope.budget.limits)
    reservation_key = bridge_budget_operation_key(
        investigation_id=state.investigation_id,
        turn_index=0,
        context_sha256="d" * 64,
    )
    orphaned_budget.reserve(
        reservation_key,
        {
            BudgetDimension.BRIDGE_TURNS: 1,
            BudgetDimension.CONTEXT_BYTES: 100,
            BudgetDimension.CONTEXT_TOKENS: 25,
        },
    )
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1, budget=orphaned_budget),
        expected_version=envelope.version,
    )

    resumed = await fixture.service.resume(
        state.investigation_id,
        expected_version=pending.version,
        owner_job_terminal=True,
    )

    assert len(transport.recovery_calls) == 1
    assert transport.recovery_calls[0]["owner_job_terminal"] is True
    assert resumed.stop_reason is None
    assert resumed.pending_context_sha256 == "d" * 64
    assert investigation_bridge_attempt(resumed.recent_actions, 0) == 1
    recovered_envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item
        for item in recovered_envelope.budget.reservations
        if item.operation_key == reservation_key
    )
    assert reservation.status is BudgetReservationStatus.RELEASED
    assert recovered_envelope.budget.reserved[BudgetDimension.BRIDGE_TURNS] == 0


@pytest.mark.asyncio
async def test_interrupt_during_tool_wait_archives_result_and_blocks_next_tool() -> None:
    first = _tool_call("vt.search", "content:first", "check the first payload")
    second = _tool_call("vt.search", "content:second", "check the second payload")
    fixture = _Fixture(
        responses=[
            _decision(goal="check both payloads", next_action="finish", tools=(first, second))
        ],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    entered = asyncio.Event()
    release = asyncio.Event()

    async def delayed_tool():
        entered.set()
        await release.wait()

    fixture.tools["vt.search"].after_execute = delayed_tool
    worker = asyncio.create_task(fixture.service.run(state.investigation_id))
    await asyncio.wait_for(entered.wait(), timeout=2)

    stopped = await fixture.service.interrupt(state.investigation_id)
    assert stopped.stop_reason is StopReason.USER_INTERRUPT
    release.set()
    after_interrupt = await worker

    first_key, _ = _operation_and_observation_id(state.investigation_id, first)
    first_invocation = await fixture.invocations.get_by_operation_key(first_key)
    assert after_interrupt.stop_reason is StopReason.USER_INTERRUPT
    assert first_invocation is not None
    assert first_invocation.status is ToolInvocationStatus.SUCCEEDED
    assert len(first_invocation.response_refs) == 1
    assert len(await fixture.observations.list_for_investigation(state.investigation_id)) == 1
    assert fixture.tools["vt.search"].calls == [first["arguments"]]

    fixture.tools["vt.search"].after_execute = None
    await fixture.service.resume(state.investigation_id)
    resumed = await fixture.service.run(state.investigation_id)

    assert resumed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.tools["vt.search"].calls == [first["arguments"], second["arguments"]]
    assert len(await fixture.observations.list_for_investigation(state.investigation_id)) == 2


@pytest.mark.asyncio
async def test_interrupt_after_running_checkpoint_prevents_tool_dispatch() -> None:
    call = _tool_call("vt.search", "content:before-dispatch", "check before dispatch")
    fixture = _Fixture(
        responses=[_decision(goal="check payload", next_action="finish", tools=(call,))],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    original_update = fixture.invocations.update_if_version
    interrupted = False

    async def update_then_interrupt(invocation, *, expected_version):
        nonlocal interrupted
        saved = await original_update(invocation, expected_version=expected_version)
        if saved.status is ToolInvocationStatus.RUNNING and not interrupted:
            interrupted = True
            await fixture.service.interrupt(state.investigation_id)
        return saved

    fixture.invocations.update_if_version = update_then_interrupt
    result = await fixture.service.run(state.investigation_id)

    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    assert result.stop_reason is StopReason.USER_INTERRUPT
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.FAILED
    assert fixture.tools["vt.search"].calls == []


@pytest.mark.asyncio
async def test_external_timeout_requires_reconciliation_before_resume() -> None:
    call = _tool_call("vt.search", "content:uncertain", "check an uncertain operation")
    fixture = _Fixture(
        responses=[_decision(goal="check payload", next_action="finish", tools=(call,))],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()

    async def timeout_after_dispatch():
        raise TimeoutError("provider response was not confirmed")

    fixture.tools["vt.search"].after_execute = timeout_after_dispatch
    stopped = await fixture.service.run(state.investigation_id)

    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)
    invocation = await fixture.invocations.get_by_operation_key(operation_key)
    envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in envelope.budget.reservations if item.operation_key == operation_key
    )
    assert stopped.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert invocation is not None
    assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert reservation.status.value == "reserved"
    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)

    await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=InvocationReconciliationOutcome.NOT_EXECUTED,
        actor="analyst-1",
        reason="The provider confirms the request did not execute.",
        expected_version=invocation.version,
        idempotency_key="w16-timeout-not-executed",
    )
    resolved_envelope = await fixture.envelopes.get(state.investigation_id)
    resolved_reservation = next(
        item
        for item in resolved_envelope.budget.reservations
        if item.operation_key == operation_key
    )
    assert resolved_reservation.status.value == "released"

    fixture.tools["vt.search"].after_execute = None
    await fixture.service.resume(state.investigation_id)
    resumed = await fixture.service.run(state.investigation_id)
    assert resumed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.tools["vt.search"].calls == [call["arguments"]]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "outcome",
    [
        InvocationReconciliationOutcome.EXECUTED_WITH_RESULT,
        InvocationReconciliationOutcome.NOT_EXECUTED,
    ],
)
async def test_reconciliation_uses_cas_and_commits_or_releases_ambiguous_budget(outcome) -> None:
    call = _tool_call("vt.search", "content:ambiguous", "resolve an ambiguous lookup")
    fixture = _Fixture(responses=[], tools=(("vt.search", "vt"),))
    state = await fixture.start()
    operation_key, _ = _operation_and_observation_id(state.investigation_id, call)
    envelope = await fixture.envelopes.get(state.investigation_id)
    cost = {BudgetDimension.VT_UNITS: 1}
    envelope.budget.reserve(operation_key, cost)
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )
    invocation = ToolInvocation(
        investigation_id=state.investigation_id,
        operation_key=operation_key,
        tool_name=call["tool_name"],
        schema_version=call["schema_version"],
        normalized_parameters=call["arguments"],
        purpose=call["purpose"],
        reserved_budget=cost,
    )
    invocation = await fixture.invocations.add_if_absent(invocation)
    invocation = await fixture.invocations.update_if_version(
        invocation.transition(ToolInvocationStatus.RUNNING), expected_version=invocation.version
    )
    invocation = await fixture.invocations.update_if_version(
        invocation.transition(ToolInvocationStatus.NEEDS_REVIEW),
        expected_version=invocation.version,
    )

    with pytest.raises(ValueError, match="reconciled before resuming"):
        await fixture.service.resume(state.investigation_id)
    with pytest.raises(VersionConflictError, match="version changed"):
        await fixture.service.reconcile_invocation(
            state.investigation_id,
            invocation.id,
            outcome=outcome,
            actor="analyst-1",
            reason="Reject the stale reconciliation attempt.",
            expected_version=invocation.version - 1,
            idempotency_key="reconcile-stale-cas",
        )

    response_refs: tuple[UUID, ...] = ()
    if outcome is InvocationReconciliationOutcome.EXECUTED_WITH_RESULT:
        observation = EvidenceObservation(
            investigation_id=state.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256="f" * 64,
            source="analyst-review",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="ip",
            object_id="203.0.113.25",
        )
        await fixture.observations.append_if_absent(observation)
        response_refs = (observation.id,)
    resolved = await fixture.service.reconcile_invocation(
        state.investigation_id,
        invocation.id,
        outcome=outcome,
        actor="analyst-1",
        reason="Resolve the ambiguous provider outcome after review.",
        response_refs=response_refs,
        expected_version=invocation.version,
        idempotency_key="reconcile-cas-valid",
    )
    latest_envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )

    assert resolved.response_refs == response_refs
    assert len(fixture.analyst_decisions.items) == 1
    if outcome is InvocationReconciliationOutcome.EXECUTED_WITH_RESULT:
        assert resolved.status is ToolInvocationStatus.SUCCEEDED
        assert reservation.status.value == "committed"
        assert reservation.committed_amounts[BudgetDimension.VT_UNITS] == 1
    else:
        assert resolved.status is ToolInvocationStatus.FAILED
        assert reservation.status.value == "released"
        assert latest_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    await fixture.service.resume(state.investigation_id)


@pytest.mark.asyncio
async def test_adapter_nonexecution_claim_does_not_commit_external_budget() -> None:
    fixture = _Fixture(responses=[], tools=(("vt.download_sample", "vt", ToolEffect.ACQUIRE),))
    state = await fixture.start()
    operation_key = "synthetic-definitive-download-refusal"
    reserved = {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1}
    envelope = await fixture.envelopes.get(state.investigation_id)
    envelope.budget.reserve(operation_key, reserved)
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )

    result = ToolResult(
        status=ToolInvocationStatus.UNAVAILABLE,
        budget_consumed={BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 0},
        output={
            "confirmed_non_execution": True,
            "reason": "provider_download_route_not_found",
        },
    )
    await fixture.service._complete_tool_reservation(
        investigation_id=state.investigation_id,
        operation_key=operation_key,
        result=result,
        reserved_amounts=reserved,
        uncertain=False,
        external_effect=True,
    )

    latest_envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert reservation.committed_amounts == {}
    assert latest_envelope.budget.reserved[BudgetDimension.VT_UNITS] == 1
    assert latest_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    assert latest_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0


@pytest.mark.asyncio
async def test_adapter_cached_claim_without_framework_proof_keeps_reservation() -> None:
    fixture = _Fixture(responses=[], tools=(("vt.download_sample", "vt", ToolEffect.ACQUIRE),))
    state = await fixture.start()
    operation_key = "synthetic-cached-download-refusal"
    reserved = {BudgetDimension.VT_UNITS: 1, BudgetDimension.DOWNLOADS: 1}
    envelope = await fixture.envelopes.get(state.investigation_id)
    envelope.budget.reserve(operation_key, reserved)
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )

    await fixture.service._complete_tool_reservation(
        investigation_id=state.investigation_id,
        operation_key=operation_key,
        result=ToolResult(
            status=ToolInvocationStatus.UNAVAILABLE,
            budget_consumed={BudgetDimension.VT_UNITS: 0, BudgetDimension.DOWNLOADS: 0},
            output={
                "confirmed_non_execution": True,
                "cached": True,
                "reason": "provider_download_route_not_found",
            },
        ),
        reserved_amounts=reserved,
        uncertain=False,
        external_effect=True,
    )

    latest_envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert latest_envelope.budget.reserved[BudgetDimension.VT_UNITS] == 1
    assert latest_envelope.budget.reserved[BudgetDimension.DOWNLOADS] == 1
    assert latest_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 0
    assert latest_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 0


@pytest.mark.asyncio
async def test_untyped_advisor_failure_does_not_release_reserved_units() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    operation_key = "synthetic-advisor-pre-transport-rejection"
    reserved = {
        BudgetDimension.BRIDGE_TURNS: 1,
        BudgetDimension.TOOL_SECONDS: 240,
        BudgetDimension.CONTEXT_BYTES: 110_592,
    }
    envelope = await fixture.envelopes.get(state.investigation_id)
    envelope.budget.reserve(operation_key, reserved)
    await fixture.envelopes.save_if_version(
        replace(envelope, version=envelope.version + 1), expected_version=envelope.version
    )
    result = ToolResult(
        ToolInvocationStatus.UNAVAILABLE,
        error_code="advisor_unavailable",
        budget_consumed={
            BudgetDimension.BRIDGE_TURNS: 0,
            BudgetDimension.CONTEXT_BYTES: 4_096,
        },
        output={
            "advisor_status": "unavailable",
            "failure_stage": "pre_submission",
            "confirmed_non_execution": True,
            "bridge_invocation_id": None,
            "provider_usage": {"status": "not_submitted"},
        },
    )

    assert _confirmed_external_non_execution(result) is False
    await fixture.service._complete_tool_reservation(
        investigation_id=state.investigation_id,
        operation_key=operation_key,
        result=result,
        reserved_amounts=reserved,
        uncertain=False,
        external_effect=True,
    )

    latest_envelope = await fixture.envelopes.get(state.investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.status is BudgetReservationStatus.RESERVED
    assert latest_envelope.budget.reserved[BudgetDimension.BRIDGE_TURNS] == 1
    assert latest_envelope.budget.reserved[BudgetDimension.TOOL_SECONDS] == 240
    assert latest_envelope.budget.reserved[BudgetDimension.CONTEXT_BYTES] == 110_592


@pytest.mark.asyncio
async def test_g2_revocation_between_external_tools_blocks_the_second_call() -> None:
    first = _tool_call("vt.search", "content:one", "check the first payload")
    second = _tool_call("network.lookup", "203.0.113.9", "check the endpoint")
    fixture = _Fixture(
        responses=[
            _decision(goal="check both sources", next_action="continue", tools=(first, second)),
            _decision(goal="finish after revocation", next_action="finish"),
        ],
        tools=(("vt.search", "vt"), ("network.lookup", "network")),
    )
    state = await fixture.start()
    first_key, _ = _operation_and_observation_id(state.investigation_id, first)
    second_key, _ = _operation_and_observation_id(state.investigation_id, second)
    fixture.service._envelopes = _RevokeAfterCommittedOperation(fixture.envelopes, first_key)
    local_tool = _FakeTool("article.get_claims", fixture.execution_order)
    fixture.registry.register(
        ToolDefinition(
            name="article.get_claims",
            schema_version="1",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            effect=ToolEffect.LOCAL_READ,
            cost_model={},
            confidentiality=ToolConfidentiality.INTERNAL,
            limitations=("Local article context",),
            capability_key="article.read",
        ),
        local_tool,
    )
    fixture.service._available_capabilities = frozenset(
        (*fixture.available_capabilities, "article.read")
    )

    result = await fixture.service.run(state.investigation_id)

    latest = await fixture.envelopes.get(state.investigation_id)
    denied = await fixture.invocations.get_by_operation_key(
        f"{second_key}:g2-denied:{latest.version}"
    )
    assert result.stop_reason is StopReason.PERMISSION_REQUIRED
    assert fixture.execution_order == ["vt.search"]
    assert denied is not None
    assert denied.status is ToolInvocationStatus.POLICY_DENIED
    assert any("article.get_claims" in action for action in result.recent_actions)


@pytest.mark.asyncio
async def test_invalid_bridge_output_stops_after_two_corrections_until_new_request() -> None:
    invalid = {"reasoning_summary": "missing required decision fields"}
    fixture = _Fixture(
        responses=[
            invalid,
            invalid,
            invalid,
            _decision(goal="finish after the user request", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    rejected = await fixture.service.run(state.investigation_id)

    assert rejected.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert fixture.bridge_transport.submission_count == 3
    assert any(action.startswith("bridge_invalid_output:") for action in rejected.recent_actions)
    with pytest.raises(ValueError, match="new user request"):
        await fixture.service.resume(state.investigation_id)

    request_action = USER_REQUEST_ACTION_PREFIX + json.dumps(
        {"request_id": str(uuid4()), "text": "Continue with a corrected new request."},
        separators=(",", ":"),
    )
    updated = replace(
        rejected,
        version=rejected.version + 1,
        recent_actions=(*rejected.recent_actions, request_action),
    )
    await fixture.states.save_if_version(updated, expected_version=rejected.version)
    await fixture.service.resume(state.investigation_id)
    resumed = await fixture.service.run(state.investigation_id)

    assert resumed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 4


@pytest.mark.asyncio
async def test_corrected_bridge_turn_progresses_and_persists_the_rejection_trace() -> None:
    fixture = _Fixture(
        responses=[
            {"reasoning_summary": "missing required decision fields"},
            _decision(goal="finish after correcting the schema", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert result.turn_index == 1
    assert fixture.bridge_transport.submission_count == 2
    assert any(
        action.startswith("bridge_invalid_output:") and "schema validation" in action
        for action in result.recent_actions
    )


@pytest.mark.asyncio
async def test_new_sample_novelty_is_recalculated_against_narrative_hashes() -> None:
    sample_hash = "e" * 40
    fixture = _Fixture(
        responses=[],
        tools=(),
        narrative_hashes=(sample_hash.upper(),),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="analyst-test",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sample",
        object_id=sample_hash,
    )
    await fixture.observations.append_if_absent(observation)
    proposal = _proposal(
        FindingKind.NEW_SAMPLE.value,
        FindingAssessment.SUPPORTED.value,
        "The article narrative already names this sample; the observation adds a detail.",
        observation.id,
        subject_ref=sample_hash,
        novelty=NoveltyClass.NEW_TO_ARTICLE.value,
    )
    fixture.bridge_transport = ScriptedTurnTransport(
        [_decision(goal="record the sample", next_action="finish", findings=(proposal,))]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].novelty is NoveltyClass.ALREADY_DOCUMENTED_BUT_NEW_DETAIL
    assert any("recalculée côté serveur" in item for item in findings[0].limitations)
    assert any("snapshot" in item for item in findings[0].limitations)


@pytest.mark.asyncio
@pytest.mark.parametrize("subject_ref", [None, "not-a-hash"])
async def test_new_sample_without_valid_subject_ref_is_inconclusive(subject_ref) -> None:
    fixture = _Fixture(
        responses=[],
        tools=(),
    )
    state = await fixture.start()
    observation = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="analyst-test",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="sample",
        object_id="d" * 64,
    )
    await fixture.observations.append_if_absent(observation)
    proposal = _proposal(
        FindingKind.NEW_SAMPLE.value,
        FindingAssessment.SUPPORTED.value,
        "An unidentified sample may be relevant.",
        observation.id,
        subject_ref=subject_ref,
        novelty=NoveltyClass.NEW_TO_ARTICLE.value,
    )
    fixture.bridge_transport = ScriptedTurnTransport(
        [_decision(goal="record a possible sample", next_action="finish", findings=(proposal,))]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.INCONCLUSIVE
    assert findings[0].novelty is NoveltyClass.NOT_CONFIRMED
    assert any("subject_ref hash valide" in item for item in findings[0].limitations)


@pytest.mark.asyncio
async def test_unrelated_observation_downgrades_supported_and_withholds_fact_opinion() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    _, observation_id = _operation_and_observation_id(state.investigation_id, call)
    proposal = _proposal(
        FindingKind.NETWORK_BEHAVIOUR.value,
        FindingAssessment.SUPPORTED.value,
        "The cited object was observed to contact this endpoint.",
        observation_id,
        subject_ref="203.0.113.9",
    )
    opinion = _opinion_delta(_finding_id(state.investigation_id, proposal), observation_id)
    fixture.bridge_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="test a proposed relationship",
                next_action="finish",
                tools=(call,),
                findings=(proposal,),
                opinion=opinion,
            )
        ]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.QUALIFIED
    assert findings[0].limitations[-1] == "l'observation citée ne porte pas sur l'objet visé"
    analyst_opinion = await fixture.opinions.get(
        uuid5(_OPINION_NAMESPACE, str(state.investigation_id))
    )
    assert analyst_opinion is not None
    assert analyst_opinion.blocks == ()
    assert any("citation validation failed" in item for item in analyst_opinion.unresolved_points)


@pytest.mark.asyncio
async def test_claim_verification_requires_non_claim_source_evidence() -> None:
    fixture = _Fixture(responses=[], tools=())
    state = await fixture.start()
    claim_source = EvidenceObservation(
        investigation_id=state.investigation_id,
        kind=EvidenceKind.CLAIM_SOURCE,
        blob_id=uuid4(),
        sha256="f" * 64,
        source="article-source",
        source_version="1",
        observed_at=datetime.now(UTC),
        object_type="claim",
        object_id="C1",
    )
    await fixture.observations.append_if_absent(claim_source)
    proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.SUPPORTED.value,
        "The cited article claim has independent evidence.",
        claim_source.id,
        claim_refs=("C1",),
    )
    fixture.bridge_transport = ScriptedTurnTransport(
        [_decision(goal="verify the claim", next_action="finish", findings=(proposal,))]
    )
    fixture.service._bridge = BridgeInvestigator(fixture.bridge_transport)

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.INCONCLUSIVE
    assert any("observation de preuve" in item for item in findings[0].limitations)


def test_vt_read_ignores_source_do_not_submit_but_keeps_envelope_veto() -> None:
    fixture = _Fixture(responses=[], tools=())
    source_flagged_snapshot = replace(fixture.snapshot, do_not_submit=True)
    restricted_envelope = replace(
        fixture.envelope,
        disclosure=replace(fixture.envelope.disclosure, do_not_submit=True),
    )

    assert InvestigationService.vt_external_disclosure_allowed(
        source_flagged_snapshot, fixture.envelope, "vt.file_report"
    )
    assert not InvestigationService.vt_external_disclosure_allowed(
        fixture.snapshot, restricted_envelope, "vt.file_report"
    )
    assert InvestigationService.bridge_disclosure_allowed(source_flagged_snapshot, fixture.envelope)


@pytest.mark.asyncio
async def test_bridge_submits_when_source_permission_flags_are_false() -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
        external_allowed=False,
    )
    fixture.snapshot = replace(fixture.snapshot, do_not_submit=True)
    fixture.context_builder.article_snapshots.snapshot = fixture.snapshot
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.attempt_count == 1
    assert fixture.bridge_transport.submission_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("policy", ["external_llm_allowed", "do_not_submit"])
async def test_bridge_does_not_submit_when_envelope_disclosure_denies(policy: str) -> None:
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish")],
        tools=(),
    )
    disclosure = replace(fixture.envelope.disclosure, **{policy: policy == "do_not_submit"})
    fixture.envelope = replace(fixture.envelope, disclosure=disclosure)
    state = await fixture.start()

    result = await fixture.service.run(state.investigation_id)

    assert result.stop_reason is StopReason.MODEL_UNAVAILABLE
    assert fixture.bridge_transport.attempt_count == 0
    assert fixture.bridge_transport.submission_count == 0


@pytest.mark.asyncio
async def test_exhausted_confirmed_pre_submission_retries_resume_as_a_distinct_attempt(
    monkeypatch,
) -> None:
    failure = TurnSubmission(
        TurnSubmissionStatus.UNAVAILABLE,
        detail="composer not ready before submission",
        error_code="bridge_server_error",
        submission_state="pre_submission",
        retryable=True,
    )
    fixture = _Fixture(
        responses=[failure, failure, failure],
        tools=(),
        bridge_turn_budget=4,
    )
    initial = await fixture.start()
    complete_reservation = fixture.service._complete_bridge_reservation
    legacy_committed_keys = []

    async def complete_legacy_committed_reservation(
        investigation_id,
        operation_key,
        budget,
        *,
        blocked,
        confirmed_non_submission=False,
    ):
        if confirmed_non_submission:
            # Model an older worker that committed this key before the durable
            # Bridge error contract exposed that no request had been submitted.
            budget.commit(operation_key)
            legacy_committed_keys.append(operation_key)
        await complete_reservation(
            investigation_id,
            operation_key,
            budget,
            blocked=blocked,
            confirmed_non_submission=confirmed_non_submission,
        )

    monkeypatch.setattr(
        fixture.service,
        "_complete_bridge_reservation",
        complete_legacy_committed_reservation,
    )

    stopped = await fixture.service.run(initial.investigation_id)

    assert stopped.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert fixture.bridge_transport.submission_count == 3
    assert fixture.envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 0
    assert len(legacy_committed_keys) == 1
    assert investigation_bridge_attempt(stopped.recent_actions, stopped.turn_index) == 3
    investigation = await fixture.investigations.get(initial.investigation_id)
    assert investigation is not None
    stopped_view = build_investigation_operator_view(
        investigation=investigation,
        state=stopped,
        synthesis_sha256=fixture.snapshot.synthesis_sha256,
        envelope=await fixture.envelopes.get(initial.investigation_id),
        invocations=(),
        observations=(),
        opinion=None,
        jobs=(),
        available_tools=(),
        unavailable_tools=(),
    )
    assert stopped_view.payload["last_operation"]["status"] == "pre_submission_retry_exhausted"
    assert stopped_view.payload["next_idempotent_action"]["code"] == "resume_investigation"

    fixture.bridge_transport._responses.append(
        _decision(goal="Finish after the Bridge composer recovered.", next_action="finish")
    )
    resumed = await fixture.service.resume(initial.investigation_id)
    completed = await fixture.service.run(initial.investigation_id)

    assert resumed.stop_reason is None
    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 4
    assert any(key.endswith(":attempt=3") for key in fixture.bridge_transport._by_key)
    assert fixture.envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1


@pytest.mark.asyncio
async def test_unclassified_bridge_failure_is_not_retried() -> None:
    fixture = _Fixture(
        responses=[
            TurnSubmission(
                TurnSubmissionStatus.UNAVAILABLE,
                detail="transport result is uncertain",
                error_code="bridge_server_error",
                retryable=True,
            ),
            _decision(goal="Must not be called automatically.", next_action="finish"),
        ],
        tools=(),
    )
    initial = await fixture.start()

    stopped = await fixture.service.run(initial.investigation_id)

    assert stopped.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert fixture.bridge_transport.submission_count == 1


@pytest.mark.asyncio
async def test_resume_from_legacy_model_unavailable_retries_confirmed_failed_attempt() -> None:
    fixture = _Fixture(
        responses=[TurnSubmission(TurnSubmissionStatus.NEEDS_REVIEW, detail="old checkpoint")],
        tools=(),
        bridge_turn_budget=4,
    )
    initial = await fixture.start()
    old_stop = await fixture.service.run(initial.investigation_id)
    assert old_stop.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert old_stop.pending_context_sha256 is not None
    assert len(fixture.bridge_transport._by_key) == 1
    prior_key = next(iter(fixture.bridge_transport._by_key))

    # Recreate the deployed legacy state: a previous resume converted the
    # investigation stop to model_unavailable while retaining a failed Bridge
    # checkpoint. The turn itself explicitly proves no submission occurred.
    legacy_state = replace(
        old_stop,
        version=old_stop.version + 1,
        stop_reason=StopReason.MODEL_UNAVAILABLE,
    )
    await fixture.states.save_if_version(legacy_state, expected_version=old_stop.version)
    investigation = await fixture.investigations.get(initial.investigation_id)
    assert investigation is not None
    investigation.status = AnalystInvestigationStatus.EXHAUSTED
    await fixture.investigations.save(investigation)
    fixture.bridge_transport._by_key[prior_key] = TurnSubmission(
        TurnSubmissionStatus.UNAVAILABLE,
        detail="composer not ready before submission",
        error_code="bridge_server_error",
        submission_state="pre_submission",
        retryable=True,
    )
    fixture.bridge_transport._responses.append(
        _decision(goal="Finish after a safe distinct retry.", next_action="finish")
    )

    resumed = await fixture.service.resume(initial.investigation_id, owner_job_terminal=True)
    completed = await fixture.service.run(initial.investigation_id)

    assert resumed.stop_reason is None
    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2
    assert any(key.endswith(":attempt=1") for key in fixture.bridge_transport._by_key)
    assert fixture.envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 1


@pytest.mark.asyncio
async def test_cancel_during_pre_submission_backoff_releases_bridge_budget(monkeypatch) -> None:
    fixture = _Fixture(
        responses=[
            TurnSubmission(
                TurnSubmissionStatus.UNAVAILABLE,
                detail="composer not ready before submission",
                error_code="bridge_server_error",
                submission_state="pre_submission",
                retryable=True,
            )
        ],
        tools=(),
        bridge_turn_budget=2,
    )
    fixture.service._bridge = BridgeInvestigator(
        fixture.bridge_transport,
        minimum_submission_interval_seconds=0,
        submission_jitter_fraction=0,
        pre_submission_retry_base_seconds=0.05,
        pre_submission_retry_max_seconds=0.05,
    )
    initial = await fixture.start()

    async def cancel_backoff(_seconds: float) -> None:
        raise asyncio.CancelledError

    monkeypatch.setattr("cti_app.application.bridge_investigator.asyncio.sleep", cancel_backoff)
    with pytest.raises(asyncio.CancelledError):
        await fixture.service.run(initial.investigation_id)

    envelope = await fixture.envelopes.get(initial.investigation_id)
    assert envelope is not None
    assert envelope.budget.consumed[BudgetDimension.BRIDGE_TURNS] == 0
    assert envelope.budget.reserved[BudgetDimension.BRIDGE_TURNS] == 0
    assert envelope.budget.reservations[0].status.value == "released"


@pytest.mark.asyncio
async def test_ambiguous_bridge_resume_reuses_submission_identity() -> None:
    fixture = _Fixture(
        responses=[
            TurnSubmission(TurnSubmissionStatus.NEEDS_REVIEW, detail="simulated uncertain submit"),
            _decision(goal="this response must not be submitted", next_action="finish"),
        ],
        tools=(),
    )
    state = await fixture.start()
    first = await fixture.service.run(state.investigation_id)
    await fixture.service.resume(state.investigation_id)
    second = await fixture.service.run(state.investigation_id)

    assert first.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert second.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert fixture.bridge_transport.submission_count == 1
    assert len(fixture.bridge_transport._responses) == 1


@pytest.mark.asyncio
async def test_crash_replay_does_not_execute_succeeded_tool_twice() -> None:
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    fixture = _Fixture(
        responses=[
            _decision(
                goal="check a payload",
                next_action="finish",
                tools=(call,),
                findings=(),
            )
        ],
        tools=(("vt.search", "vt"),),
    )
    state = await fixture.start()
    fixture.tools["vt.search"].observation_object_type = "ip"
    fixture.tools["vt.search"].observation_object_id = "203.0.113.9"
    _, observation_id = _operation_and_observation_id(state.investigation_id, call)
    proposal = _proposal(
        FindingKind.NETWORK_BEHAVIOUR.value,
        FindingAssessment.SUPPORTED.value,
        "A passive relationship result connects the endpoint.",
        observation_id,
        subject_ref="203.0.113.9",
    )
    opinion = _opinion_delta(_finding_id(state.investigation_id, proposal), observation_id)
    # Use a new idempotent scripted response with the evidence reference known before execution.
    fixture.bridge_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="check a payload",
                next_action="finish",
                tools=(call,),
                findings=(proposal,),
                opinion=opinion,
            )
        ]
    )
    fixture.bridge = BridgeInvestigator(fixture.bridge_transport)
    fixture.service._bridge = fixture.bridge
    fixture.states.fail_once_at = fixture.states.save_count + 2

    with pytest.raises(RuntimeError, match="simulated process crash"):
        await fixture.service.run(state.investigation_id)
    replayed = await fixture.service.run(state.investigation_id)

    assert replayed.stop_reason is StopReason.AGENT_FINISHED
    assert len(fixture.tools["vt.search"].calls) == 1
    assert fixture.bridge_transport.submission_count == 1
    assert await fixture.observations.get(observation_id) is not None


@pytest.mark.asyncio
async def test_supported_finding_with_nonexistent_citations_is_downgraded() -> None:
    proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.SUPPORTED.value,
        "The address is confirmed.",
        uuid4(),
        claim_refs=("not-a-claim",),
    )
    fixture = _Fixture(
        responses=[_decision(goal="finish", next_action="finish", findings=(proposal,))],
        tools=(),
    )
    state = await fixture.start()

    await fixture.service.run(state.investigation_id)

    findings = await fixture.findings.list_for_investigation(state.investigation_id)
    assert len(findings) == 1
    assert findings[0].assessment is FindingAssessment.INCONCLUSIVE
    assert findings[0].observation_refs == ()
    assert any("downgraded" in item for item in findings[0].limitations)


def test_tool_operation_and_finding_id_helpers_are_deterministic() -> None:
    investigation_id = uuid4()
    call = _tool_call("vt.search", "content:abc", "check a related payload")
    operation_a = _operation_and_observation_id(investigation_id, call)
    operation_b = _operation_and_observation_id(investigation_id, call)
    proposal = _proposal("network_behaviour", "supported", "Observed endpoint.", uuid4())

    assert operation_a == operation_b
    assert _finding_id(investigation_id, proposal) == _finding_id(investigation_id, proposal)
