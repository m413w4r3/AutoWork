from __future__ import annotations

import hashlib
import sys
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application.analyst_handoff import AnalystHandoffPolicy
from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.investigation_contracts import ArticleClaimV1, ArticleContextV1
from cti_app.application.investigation_local_tools import (
    GHIDRA_DECOMPILE_FUNCTION_SCHEMA,
    GHIDRA_LIST_FUNCTIONS_SCHEMA,
    GHIDRA_LIST_IMPORTS_SCHEMA,
    GHIDRA_SEARCH_SCHEMA,
    GHIDRA_TOOL_NAMES,
    GHIDRA_XREFS_SCHEMA,
    LOCAL_RUN_COMMAND,
    local_tool_capabilities,
    register_local_analysis_tools,
)
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationEnvelopeRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.investigation_state import (
    InMemoryInvestigationContextSnapshotRepository,
    InMemoryInvestigationStateRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
    validate_json_schema,
)
from cti_app.application.local_analysis import (
    GhidraUnavailableError,
    LocalCommandCatalog,
    LocalCommandService,
    SampleBytesUnavailableError,
    default_local_command_specs,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import ArticleSnapshot, ResearchContextBuilder
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationEnvelope,
    SampleAssociation,
    SampleOrigin,
    TLPLevel,
    ToolInvocationStatus,
    canonical_json_bytes,
    tool_operation_key,
)
from cti_app.domain.investigation_state import StopReason
from cti_app.domain.local_analysis import (
    GhidraObservation,
    GhidraOperation,
    LocalCommandId,
    LocalCommandResult,
    LocalCommandStatus,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)

_SAMPLE_BYTES = b"\x7fELF" + bytes(range(64))
_SAMPLE_SHA256 = hashlib.sha256(_SAMPLE_BYTES).hexdigest()
_EVIDENCE_NAMESPACE = UUID("f84fd123-5fbf-49dc-a54d-e63af924558d")
_FINDING_NAMESPACE = UUID("3e2a06e9-660e-44a7-9a9c-6db60a16ff55")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")


class _Samples:
    async def get_sample_bytes(self, sample_sha256: str, *, max_bytes: int) -> bytes:
        if sample_sha256 != _SAMPLE_SHA256 or len(_SAMPLE_BYTES) > max_bytes:
            raise SampleBytesUnavailableError("fixture sample is unavailable")
        return _SAMPLE_BYTES

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        return await self.get_sample_bytes(sample_sha256, max_bytes=len(_SAMPLE_BYTES))


class _Associations:
    def __init__(self, association: SampleAssociation) -> None:
        self.association = association

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        if self.association.investigation_id != investigation_id:
            return ()
        return (self.association,)


class _Runner:
    def __init__(
        self,
        *,
        status: LocalCommandStatus = LocalCommandStatus.SUCCEEDED,
        output: str = "ELF 64-bit LSB executable",
    ) -> None:
        self.status = status
        self.output = output
        self.calls: list[tuple[LocalCommandId, Mapping[str, object]]] = []

    async def run(
        self,
        capability,
        *,
        sample_sha256: str,
        sample_bytes: bytes,
        options: Mapping[str, object],
    ) -> LocalCommandResult:
        assert hashlib.sha256(sample_bytes).hexdigest() == sample_sha256
        self.calls.append((capability.spec.id, dict(options)))
        return LocalCommandResult(
            command_id=capability.spec.id,
            status=self.status,
            sample_sha256=sample_sha256,
            output=self.output,
            normalized_command=(capability.spec.binary, "<sample>", "<options_sha256:fixture>"),
            executable_version=capability.version,
            exit_code=0
            if self.status in {LocalCommandStatus.SUCCEEDED, LocalCommandStatus.OUTPUT_TRUNCATED}
            else None,
            duration_seconds=0.25,
            started_at=datetime.now(UTC),
            detail_code="fixture" if self.status is not LocalCommandStatus.SUCCEEDED else None,
        )


class _MemoryArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}

    async def put_canonical_json(self, payload: Mapping[str, Any], *, bucket: str):
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        digest = hashlib.sha256(encoded).hexdigest()
        blob_id = uuid5(_EVIDENCE_NAMESPACE, f"{bucket}:{digest}")
        self.payloads[blob_id] = dict(payload)
        return blob_id, digest


class _ToolObservationWriter:
    def __init__(self, repository: InMemoryEvidenceObservationRepository) -> None:
        self.repository = repository

    async def archive(self, invocation, result):
        payload = canonical_json_bytes(dict(result.output))
        observation = EvidenceObservation(
            id=uuid5(_EVIDENCE_NAMESPACE, invocation.operation_key),
            investigation_id=invocation.investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid5(_EVIDENCE_NAMESPACE, f"tool-output:{invocation.operation_key}"),
            sha256=hashlib.sha256(payload).hexdigest(),
            source=invocation.tool_name,
            source_version=invocation.schema_version,
            observed_at=datetime.now(UTC),
            object_type="tool_call",
            object_id=invocation.operation_key,
            limitations=("Tool output is untrusted test data.",),
            invocation_ref=invocation.id,
        )
        return (await self.repository.append_if_absent(observation),)


class _Ghidra:
    def __init__(self, *, unavailable: bool = False) -> None:
        self.unavailable = unavailable
        self.calls: list[tuple[GhidraOperation, str, str | None, str | None, int]] = []

    async def analyze(
        self,
        operation: GhidraOperation,
        sample_sha256: str,
        *,
        address: str | None = None,
        query: str | None = None,
        limit: int = 100,
    ) -> GhidraObservation:
        self.calls.append((operation, sample_sha256, address, query, limit))
        if self.unavailable:
            raise GhidraUnavailableError("fixture endpoint is offline")
        return GhidraObservation(
            operation=operation,
            sample_sha256=sample_sha256,
            ghidra_version="11.0",
            plugin_version="1.2.0",
            address=address,
            observed_at=datetime.now(UTC),
            data={"name": "FUN_00401000", "decompilation": "return 0;"},
        )


class _Harness:
    def __init__(
        self,
        *,
        command_status: LocalCommandStatus = LocalCommandStatus.SUCCEEDED,
        command_output: str = "ELF 64-bit LSB executable",
        ghidra_unavailable: bool = False,
        max_local_output_chars: int = 12_000,
    ) -> None:
        self.investigation_id = uuid4()
        self.associations = _Associations(
            SampleAssociation(
                investigation_id=self.investigation_id,
                sample_sha256=_SAMPLE_SHA256,
                origin=SampleOrigin.SEED,
            )
        )
        self.samples = _Samples()
        self.command_catalog = LocalCommandCatalog(
            specs=default_local_command_specs(),
            which=lambda binary: None if binary == "strings" else sys.executable,
        )
        self.runner = _Runner(status=command_status, output=command_output)
        self.command_service = LocalCommandService(
            self.samples,
            self.runner,
            self.command_catalog,
            enabled=True,
        )
        self.ghidra = _Ghidra(unavailable=ghidra_unavailable)
        self.observations = InMemoryEvidenceObservationRepository()
        self.artifact_store = _MemoryArtifactStore()
        self.recorder = ProductionEvidenceRecorder(self.artifact_store, self.observations)
        self.registry = InMemoryToolRegistry()
        register_local_analysis_tools(
            self.registry,
            command_service=self.command_service,
            command_catalog=self.command_catalog,
            ghidra=self.ghidra,
            sample_bytes=self.samples,
            sample_associations=self.associations,
            recorder=self.recorder,
            max_local_output_chars=max_local_output_chars,
        )

    def context(self, *, investigation_id: UUID | None = None, key: str = "fixture-operation"):
        target_id = investigation_id or self.investigation_id
        budget = InvestigationBudget(
            limits={BudgetDimension.TOOL_SECONDS: 600, BudgetDimension.BRIDGE_TURNS: 12}
        )
        envelope = InvestigationEnvelope(
            investigation_id=target_id,
            allowed_providers=(),
            budget=budget,
            acquire_hits_allowed=False,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
            ),
            approval_decision_id=uuid4(),
            approval_actor_id="f2-test-analyst",
        )
        return ToolExecutionContext(
            investigation_id=target_id,
            envelope=envelope,
            budget=budget,
            idempotency_key=key,
            available_capabilities=local_tool_capabilities(self.command_catalog),
        )


def _tool_names(registry: InMemoryToolRegistry) -> set[str]:
    return {name for name, _version in registry._tools}


def _local_arguments(**overrides: Any) -> dict[str, Any]:
    return {
        "command_id": "file",
        "sample_sha256": _SAMPLE_SHA256,
        "options": {},
        **overrides,
    }


async def _dispatch(
    registry: InMemoryToolRegistry,
    context: ToolExecutionContext,
    name: str,
    arguments: Mapping[str, Any],
    *,
    operation_key: str | None = None,
):
    return await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name=name,
        schema_version="1",
        arguments=arguments,
        purpose="f2 deterministic test operation",
        operation_key=operation_key or context.idempotency_key,
    )


def test_schemas_capabilities_and_dynamic_command_enum() -> None:
    harness = _Harness()
    registered = harness.registry.get(LOCAL_RUN_COMMAND, "1")
    assert registered is not None
    local, _adapter = registered
    assert local is not None
    assert local.input_schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert tuple(local.input_schema["properties"]["command_id"]["enum"]) == (
        "file",
        "objdump",
        "readelf",
        "yara",
    )
    assert "local.run_command" in local_tool_capabilities(harness.command_catalog)
    assert not local_tool_capabilities(harness.command_catalog, ghidra_enabled=False) - {
        "local.run_command"
    }

    valid = _local_arguments()
    validate_json_schema(valid, local.input_schema)
    with pytest.raises(ValueError):
        validate_json_schema({**valid, "command_id": "strings"}, local.input_schema)
    for schema in (
        GHIDRA_LIST_FUNCTIONS_SCHEMA,
        GHIDRA_LIST_IMPORTS_SCHEMA,
        GHIDRA_XREFS_SCHEMA,
        GHIDRA_DECOMPILE_FUNCTION_SCHEMA,
        GHIDRA_SEARCH_SCHEMA,
    ):
        assert schema["$schema"] == "https://json-schema.org/draft/2020-12/schema"
    assert set(GHIDRA_TOOL_NAMES) <= _tool_names(harness.registry)


@pytest.mark.asyncio
async def test_unknown_command_and_non_catalogued_options_are_refused() -> None:
    harness = _Harness()
    context = harness.context()
    registered = harness.registry.get(LOCAL_RUN_COMMAND, "1")
    assert registered is not None
    _definition, adapter = registered

    direct = await adapter.execute(_local_arguments(command_id="echo"), context)
    assert direct.status is ToolInvocationStatus.POLICY_DENIED
    dispatched = await _dispatch(
        harness.registry, context, LOCAL_RUN_COMMAND, _local_arguments(command_id="echo")
    )
    assert dispatched.status is ToolInvocationStatus.FAILED
    assert harness.runner.calls == []

    invalid_options = _local_arguments(options={"free_argument": "--help; touch /tmp/nope"})
    denied = await _dispatch(harness.registry, context, LOCAL_RUN_COMMAND, invalid_options)
    assert denied.status is ToolInvocationStatus.POLICY_DENIED
    assert harness.runner.calls == []


@pytest.mark.asyncio
async def test_metacharacters_remain_yara_data_and_never_select_a_command() -> None:
    harness = _Harness()
    context = harness.context(key="yara-metacharacters")
    pattern = "$(touch /tmp/autowork-f2-must-not-run); ; | &&"
    result = await _dispatch(
        harness.registry,
        context,
        LOCAL_RUN_COMMAND,
        _local_arguments(command_id="yara", options={"patterns": [pattern], "match_mode": "any"}),
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert harness.runner.calls == [
        (LocalCommandId.YARA, {"patterns": (pattern,), "match_mode": "any"})
    ]
    assert result.output["command_id"] == "yara"
    assert result.output["untrusted_data"] is True


@pytest.mark.asyncio
async def test_sample_must_be_associated_with_this_investigation() -> None:
    harness = _Harness()
    context = harness.context(investigation_id=uuid4())
    result = await _dispatch(
        harness.registry,
        context,
        LOCAL_RUN_COMMAND,
        _local_arguments(),
    )
    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert harness.runner.calls == []
    assert harness.ghidra.calls == []


@pytest.mark.asyncio
async def test_missing_command_binary_maps_to_unavailable_and_is_not_announced() -> None:
    harness = _Harness()
    registered = harness.registry.get(LOCAL_RUN_COMMAND, "1")
    assert registered is not None
    definition, adapter = registered
    assert definition is not None
    assert "strings" not in definition.input_schema["properties"]["command_id"]["enum"]

    result = await adapter.execute(
        _local_arguments(command_id="strings"), harness.context(key="missing-strings")
    )
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert harness.runner.calls == []
    observation = await harness.observations.get(result.observation_refs[0])
    assert observation is not None
    assert observation.kind is EvidenceKind.BINARY_STRING
    assert observation.object_id == _SAMPLE_SHA256


@pytest.mark.parametrize(
    "status",
    [LocalCommandStatus.TIMEOUT, LocalCommandStatus.OUTPUT_TRUNCATED],
)
@pytest.mark.asyncio
async def test_timeout_and_truncation_are_partial_with_an_explicit_marker(status) -> None:
    harness = _Harness(
        command_status=status, command_output="partial output", max_local_output_chars=96
    )
    result = await _dispatch(
        harness.registry,
        harness.context(key=f"partial-{status.value}"),
        LOCAL_RUN_COMMAND,
        _local_arguments(),
    )
    assert result.status is ToolInvocationStatus.PARTIAL
    assert "[TRUNCATED:" in result.output["output"]
    observation = await harness.observations.get(result.observation_refs[0])
    assert observation is not None
    payload = harness.artifact_store.payloads[observation.blob_id]
    assert payload["output_truncated"] is True
    assert "[TRUNCATED:" in payload["output"]
    assert observation.kind is EvidenceKind.BINARY_ANALYSIS
    assert (
        observation.sha256
        == hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(payload)).hexdigest()
    )


@pytest.mark.asyncio
async def test_dispatcher_replay_is_idempotent_and_records_typed_command_evidence() -> None:
    harness = _Harness()
    arguments = _local_arguments()
    operation_key = tool_operation_key(
        investigation_id=harness.investigation_id,
        tool_name=LOCAL_RUN_COMMAND,
        schema_version="1",
        normalized_parameters=arguments,
        purpose="f2 deterministic test operation",
    )
    context = harness.context(key=operation_key)
    dispatcher = RegistryToolDispatcher(harness.registry)
    first = await dispatcher.dispatch(
        context,
        tool_name=LOCAL_RUN_COMMAND,
        schema_version="1",
        arguments=arguments,
        purpose="f2 deterministic test operation",
        operation_key=operation_key,
    )
    second = await dispatcher.dispatch(
        context,
        tool_name=LOCAL_RUN_COMMAND,
        schema_version="1",
        arguments=arguments,
        purpose="f2 deterministic test operation",
        operation_key=operation_key,
    )
    assert first == second
    assert len(harness.runner.calls) == 1
    assert len(await harness.observations.list_for_investigation(harness.investigation_id)) == 1
    observation = await harness.observations.get(first.observation_refs[0])
    assert observation is not None
    assert observation.kind is EvidenceKind.BINARY_ANALYSIS
    assert observation.object_id == _SAMPLE_SHA256
    assert (
        "isolation réseau seule, pas de confinement du système de fichiers"
        in observation.limitations
    )
    assert observation.source_version.startswith("Python ")
    payload = harness.artifact_store.payloads[observation.blob_id]
    assert payload["command"] == ["file", "<sample>", "<options_sha256:fixture>"]
    assert payload["sample_sha256"] == _SAMPLE_SHA256
    assert payload["untrusted_data"] is True


@pytest.mark.asyncio
async def test_ghidra_fixture_observation_is_traceable_and_registry_is_read_only() -> None:
    harness = _Harness()
    args = {"sample_sha256": _SAMPLE_SHA256, "address": "0x401000"}
    result = await _dispatch(
        harness.registry,
        harness.context(key="ghidra-fixture"),
        "ghidra.decompile_function",
        args,
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert harness.ghidra.calls[0][0] is GhidraOperation.DECOMPILE_FUNCTION
    names = _tool_names(harness.registry)
    assert not any("write" in name or "rename" in name or "comment" in name for name in names)
    assert harness.registry.get("ghidra.write", "1") is None

    observation = await harness.observations.get(result.observation_refs[0])
    assert observation is not None
    assert observation.kind is EvidenceKind.BINARY_ANALYSIS
    assert observation.object_id == _SAMPLE_SHA256
    payload = harness.artifact_store.payloads[observation.blob_id]
    assert payload["sample_sha256"] == _SAMPLE_SHA256
    assert payload["address"] == "0x401000"
    assert payload["ghidra_version"] == "11.0"
    assert payload["plugin_version"] == "1.2.0"


class _InvestigationRepository:
    def __init__(self, investigation: AnalystInvestigation) -> None:
        self.items = {investigation.id: investigation}

    async def get(self, investigation_id: UUID):
        return self.items.get(investigation_id)

    async def get_for_run(self, run_id: UUID):
        return next(
            (item for item in self.items.values() if item.production_run_id == run_id), None
        )

    async def add(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation

    async def save(self, investigation: AnalystInvestigation) -> None:
        self.items[investigation.id] = investigation


class _Handoff:
    def __init__(self, investigation: AnalystInvestigation) -> None:
        self.investigation = investigation

    async def ensure_for_verified_synthesis(self, **_kwargs):
        return SimpleNamespace(investigation_id=self.investigation.id)


class _ArticlePort:
    def __init__(self, snapshot: ArticleSnapshot) -> None:
        self.snapshot = snapshot

    async def resolve(self, synthesis_artifact_id: UUID) -> ArticleSnapshot:
        assert synthesis_artifact_id == self.snapshot.synthesis_artifact_id
        return self.snapshot

    async def get_claims(self, investigation_id: UUID, claim_ids: Sequence[str] = ()):
        del investigation_id
        if not claim_ids:
            return self.snapshot.article.claims
        selected = set(claim_ids)
        return tuple(item for item in self.snapshot.article.claims if item.id in selected)


class _OpinionTransport(ScriptedTurnTransport):
    def __init__(self, observations: InMemoryEvidenceObservationRepository) -> None:
        super().__init__(
            [
                {
                    "reasoning_summary": (
                        "Try the optional decompiler, then continue locally if it is offline."
                    ),
                    "goal": "Inspect one function in the quarantined seed.",
                    "hypothesis_refs": [],
                    "proposed_tool_calls": [
                        {
                            "tool_name": "ghidra.decompile_function",
                            "schema_version": "1",
                            "arguments": {"sample_sha256": _SAMPLE_SHA256, "address": "0x401000"},
                            "purpose": "Inspect the entry point with the optional decompiler.",
                        }
                    ],
                    "finding_proposals": [],
                    "opinion_delta": None,
                    "next_action": "continue",
                },
                {
                    "reasoning_summary": (
                        "Ghidra is unavailable, so use the installed local file command."
                    ),
                    "goal": "Identify the local file format.",
                    "hypothesis_refs": [],
                    "proposed_tool_calls": [
                        {
                            "tool_name": LOCAL_RUN_COMMAND,
                            "schema_version": "1",
                            "arguments": _local_arguments(),
                            "purpose": "Identify the already quarantined sample locally.",
                        }
                    ],
                    "finding_proposals": [],
                    "opinion_delta": None,
                    "next_action": "continue",
                },
            ]
        )
        self.observations = observations

    async def submit(self, **kwargs: Any):
        turn_index = kwargs["turn_index"]
        if turn_index == 2:
            investigation_id = kwargs["investigation_id"]
            evidence = await self.observations.list_for_investigation(investigation_id)
            observation = next(
                item for item in evidence if item.kind is EvidenceKind.BINARY_ANALYSIS
            )
            proposal = {
                "kind": FindingKind.CAPABILITY.value,
                "assessment": FindingAssessment.SUPPORTED.value,
                "statement": (
                    "The quarantined file is identified as an ELF executable by the local "
                    "file tool."
                ),
                "subject_ref": _SAMPLE_SHA256,
                "observation_refs": [str(observation.id)],
                "claim_refs": [],
                "novelty": None,
                "scope": "This sample and this local tool result only.",
                "limitations": ["Static file identification is not runtime behavior."],
            }
            proposal_digest = hashlib.sha256(canonical_json_bytes(proposal)).hexdigest()
            finding_id = uuid5(_FINDING_NAMESPACE, f"{investigation_id}:{proposal_digest}")
            self._responses.append(
                {
                    "reasoning_summary": (
                        "Cite the local observation in the finding and draft opinion."
                    ),
                    "goal": "Summarize the supported local result.",
                    "hypothesis_refs": [],
                    "proposed_tool_calls": [],
                    "finding_proposals": [proposal],
                    "opinion_delta": {
                        "blocks": [
                            {
                                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                                "text": proposal["statement"],
                                "finding_refs": [str(finding_id)],
                                "evidence_refs": [str(observation.id)],
                                "qualification": OpinionClaimQualification.FACT.value,
                                "certainty": 0.9,
                            }
                        ],
                        "unresolved_points": [],
                    },
                    "next_action": "finish",
                }
            )
        return await super().submit(**kwargs)


@pytest.mark.asyncio
async def test_real_investigation_loop_continues_after_ghidra_unavailable() -> None:
    run_id = uuid4()
    subject_id = uuid4()
    synthesis = ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="1" * 64,
        status=ProductionArtifactStatus.VERIFIED,
    )
    publication_id = uuid4()
    investigation = AnalystInvestigation.from_verified_synthesis(
        synthesis=synthesis,
        budget=LoopBudget(
            max_cycles=8,
            max_pivot_runs=8,
            max_hits_acquired=4,
            max_new_samples=4,
            max_vt_read_units=10,
        ),
    )
    snapshot = ArticleSnapshot(
        subject_id=subject_id,
        production_run_id=run_id,
        synthesis_artifact_id=synthesis.id,
        synthesis_sha256="2" * 64,
        article=ArticleContextV1(
            short_summary="A verified binary sample is under investigation.",
            claims=(ArticleClaimV1(id="C1", statement="The sample is a payload.", source_refs=()),),
            documented_samples=(_SAMPLE_SHA256,),
            known_chain=(),
            open_questions=("What file format does the sample use?",),
        ),
        base_publication_artifact_id=publication_id,
        base_publication_sha256="3" * 64,
        tlp="GREEN",
        do_not_submit=False,
        external_llm_allowed=True,
    )
    investigations = _InvestigationRepository(investigation)
    evidence = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()
    opinions = InMemoryAnalystOpinionRepository()
    invocations = InMemoryToolInvocationRepository()
    envelopes = InMemoryInvestigationEnvelopeRepository()
    registry = InMemoryToolRegistry()
    catalog = LocalCommandCatalog(
        specs=default_local_command_specs(),
        which=lambda binary: None if binary == "strings" else sys.executable,
    )
    samples = _Samples()
    runner = _Runner()
    command_service = LocalCommandService(samples, runner, catalog, enabled=True)
    recorder = ProductionEvidenceRecorder(_MemoryArtifactStore(), evidence)
    associations = _Associations(
        SampleAssociation(
            investigation_id=investigation.id,
            sample_sha256=_SAMPLE_SHA256,
            origin=SampleOrigin.SEED,
        )
    )
    capabilities = local_tool_capabilities(catalog, ghidra_enabled=True)
    register_local_analysis_tools(
        registry,
        command_service=command_service,
        command_catalog=catalog,
        ghidra=_Ghidra(unavailable=True),
        sample_bytes=samples,
        sample_associations=associations,
        recorder=recorder,
    )
    transport = _OpinionTransport(evidence)
    service = InvestigationService(
        post_synthesis=_Handoff(investigation),
        investigations=investigations,
        states=InMemoryInvestigationStateRepository(),
        context_snapshots=InMemoryInvestigationContextSnapshotRepository(),
        envelopes=envelopes,
        context_builder=ResearchContextBuilder(
            article_snapshots=_ArticlePort(snapshot),
            observations=evidence,
            findings=findings,
        ),
        bridge=BridgeInvestigator(transport),
        registry=registry,
        dispatcher=RegistryToolDispatcher(registry),
        invocations=invocations,
        observations=evidence,
        findings=findings,
        opinions=opinions,
        observation_writer=_ToolObservationWriter(evidence),
        available_capabilities=capabilities,
    )
    budget = InvestigationBudget(
        limits={BudgetDimension.BRIDGE_TURNS: 8, BudgetDimension.TOOL_SECONDS: 600}
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation.id,
        allowed_providers=(),
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="f2-loop-analyst",
    )
    await service.start(
        run=SimpleNamespace(id=run_id, subject_id=subject_id),
        synthesis=synthesis,
        extraction_artifacts=(),
        extraction_items=(),
        policy=AnalystHandoffPolicy(tlp="GREEN", do_not_submit=False, external_llm_allowed=True),
        envelope=envelope,
    )
    state = await service.run(investigation.id)

    assert state.stop_reason is StopReason.AGENT_FINISHED, (
        state.stop_reason,
        state.turn_index,
        transport.submission_count,
    )
    assert transport.submission_count == 3
    assert len(runner.calls) == 1
    unavailable_key = tool_operation_key(
        investigation_id=investigation.id,
        tool_name="ghidra.decompile_function",
        schema_version="1",
        normalized_parameters={"sample_sha256": _SAMPLE_SHA256, "address": "0x401000"},
        purpose="Inspect the entry point with the optional decompiler.",
    )
    unavailable = await invocations.get_by_operation_key(unavailable_key)
    assert unavailable is not None
    assert unavailable.status is ToolInvocationStatus.UNAVAILABLE

    finding = next(iter(await findings.list_for_investigation(investigation.id)))
    assert finding.assessment is FindingAssessment.SUPPORTED
    assert finding.observation_refs
    opinion = await opinions.get(uuid5(_OPINION_NAMESPACE, str(investigation.id)))
    assert opinion is not None
    assert opinion.blocks[0].finding_refs == (finding.id,)
    assert opinion.blocks[0].evidence_refs[0] in finding.observation_refs
