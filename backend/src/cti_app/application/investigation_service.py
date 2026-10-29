"""Agent-directed investigation orchestration over the A1 contracts and ports."""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import fields, replace
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid5

from cti_app.application.analyst_handoff import AnalystHandoffPolicy, AnalystPostSynthesisService
from cti_app.application.article_novelty import (
    ArticleNoveltyService,
    NoveltyCandidate,
    NoveltyCandidateKind,
)
from cti_app.application.blob_storage import BlobReadLimitExceededError
from cti_app.application.bridge_investigator import (
    BridgeInvestigator,
    BridgeTurnResult,
    TurnSubmissionStatus,
)
from cti_app.application.evidence_recorder import (
    ProductionEvidenceRecorder,
    _sanitize_json,
    sanitize_evidence_text,
)
from cti_app.application.external_effects import external_effect_source_do_not_submit_blocks
from cti_app.application.investigation_contracts import (
    AgentTurnDecisionV1,
    InvestigationContextV1,
    parse_investigation_context,
    validate_decision_hypothesis_references,
)
from cti_app.application.investigation_findings import (
    constrain_supported_assessment,
)
from cti_app.application.investigation_persistence import (
    AnalystOpinionRepository,
    EvidenceObservationRepository,
    FindingRepository,
    InvestigationBudgetRepository,
    InvestigationBudgetSnapshot,
    InvestigationEnvelopeRepository,
    SampleAssociationRepository,
    ToolInvocationRepository,
    VersionConflictError,
)
from cti_app.application.investigation_state import (
    InvestigationContextSnapshotRepository,
    InvestigationStateRepository,
    InvestigationStateVersionConflict,
)
from cti_app.application.investigation_tools import (
    ToolDispatcher,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
    tool_operation_cache_identity,
)
from cti_app.application.investigation_vt_tools import (
    _normalize_object_id,
    is_trusted_vt_passive_read_adapter,
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
    SampleRepository,
    SourceDocumentRepository,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import (
    PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE,
    PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE,
    PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE,
    USER_REQUEST_ACTION_PREFIX,
    ArticleSnapshot,
    ResearchContextBuilder,
    provider_authorization_blocked_capabilities,
    provider_download_blocked_capabilities,
)
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.application.virustotal import (
    VirusTotalInvalidInputError,
    normalize_file_hash,
)
from cti_app.domain.analyst_opinion import (
    AnalystObservationV1,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    investigation_opinion_id,
    opinion_blocks_with_observations,
    verify_opinion_citations,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.entities import SampleState
from cti_app.domain.external_effects import (
    EvidenceReference,
    ExternalEffectAuthority,
    ExternalEffectAuthorityKind,
    ExternalEffectValidationError,
)
from cti_app.domain.investigation import (
    AgendaItem,
    AgentNextAction,
    BudgetDimension,
    BudgetExceeded,
    BudgetReservation,
    BudgetReservationStatus,
    EnvelopeAuthorityKind,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    FrameworkPreSubmissionProof,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    InvestigationFinding,
    NoveltyClass,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
    canonical_json_bytes,
    thaw_json,
    tool_operation_key,
)
from cti_app.domain.investigation_state import (
    Hypothesis,
    HypothesisStatus,
    InvestigationWorkingState,
    StopReason,
    investigation_bridge_attempt,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    AnalystInvestigationStatus,
    ProductionArtifact,
    ProductionRun,
)
from cti_app.domain.virustotal import VirusTotalObjectType

_logger = logging.getLogger(__name__)
_SAFE_LOG_TOKEN = re.compile(r"[^A-Za-z0-9_.-]+")

_FINDING_NAMESPACE = UUID("3e2a06e9-660e-44a7-9a9c-6db60a16ff55")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")
_INVOCATION_RECONCILIATION_NAMESPACE = UUID("ac8d1f15-c6ca-4808-97db-d3b63d34f851")
_INVALID_BRIDGE_ACTION_PREFIX = "bridge_invalid_output:"
MAX_OPINION_DRAFT_CORRECTIONS = 2
MAX_SAMPLE_ANALYSIS_FINISH_REFUSALS = 2
MAX_IDENTICAL_DEFINITE_TOOL_FAILURES = 2
MAX_STATE_VERSION_CONFLICT_RETRIES = 4
_RECENT_ACTIONS_LIMIT = 32
_OPERATOR_REQUEST_MARKER_PREFIX = "api:user-request:"
_VT_COORDINATED_READ_TOOLS = frozenset(
    {
        "vt.file_report",
        "vt.object_report",
        "vt.relationships",
        "vt.intelligence_search",
        "vt.behaviour",
        "vt.behaviour_pcap",
        "vt.download_sample",
    }
)
_VT_PASSIVE_READ_TOOLS = _VT_COORDINATED_READ_TOOLS - {"vt.download_sample"}
_VT_SOURCE_REFS_PARAMETER = "_vt_external_effect_source_refs"
_ADVISOR_PREFLIGHT_REFUSAL_PARAMETER = "_advisor_preflight_refusal_expected"


class InvocationReconciliationOutcome(StrEnum):
    EXECUTED_WITH_RESULT = "executed_with_result"
    NOT_EXECUTED = "not_executed"
    ABANDON = "abandon"


class _BudgetSnapshotPersistenceError(RuntimeError):
    """Envelope and normalized budget could not be persisted atomically."""


class ToolObservationWriter(Protocol):
    async def archive(
        self, invocation: ToolInvocation, result: ToolResult
    ) -> tuple[EvidenceObservation, ...]: ...


class FrameworkProofDescriptorReader(Protocol):
    async def blob_metadata(self, blob_id: UUID) -> Mapping[str, Any] | None: ...


_MAX_FRAMEWORK_PRE_SUBMISSION_PROOF_BYTES = 64 * 1024


class ProductionArtifactObservationWriter:
    """Archive bounded tool summaries as canonical evidence blobs."""

    def __init__(
        self,
        artifact_store: ProductionArtifactStore,
        observations: EvidenceObservationRepository,
    ) -> None:
        self._recorder = ProductionEvidenceRecorder(artifact_store, observations)

    async def archive(
        self, invocation: ToolInvocation, result: ToolResult
    ) -> tuple[EvidenceObservation, ...]:
        framework_proof = result.framework_pre_submission_proof
        has_framework_proof = framework_proof is not None
        reason = result.output.get("reason")
        provider_auth_refusal = (
            result.status is ToolInvocationStatus.UNAVAILABLE
            and reason == "provider_authorization_refused"
        )
        download_refusal_object_type = (
            {
                "provider_download_route_not_found": PROVIDER_DOWNLOAD_ROUTE_REFUSAL_OBJECT_TYPE,
                "provider_download_authorization_refused": (
                    PROVIDER_DOWNLOAD_AUTH_REFUSAL_OBJECT_TYPE
                ),
            }.get(reason)
            if isinstance(reason, str)
            else None
        )
        provider_download_refusal = (
            result.status is ToolInvocationStatus.UNAVAILABLE
            and download_refusal_object_type is not None
        )
        if (
            result.observation_refs
            and invocation.tool_name not in {"vt.download_sample", "sample.analyze"}
            and not provider_auth_refusal
            and not provider_download_refusal
            and not has_framework_proof
        ):
            return ()
        safe_result = _sanitize_json(thaw_json(result.output))
        if not safe_result:
            if not has_framework_proof:
                return ()
            safe_result = {}
        if framework_proof is None:
            # An adapter response is untrusted and cannot attest that the
            # framework rejected an operation before submission.
            safe_result.pop("framework_pre_submission_proof", None)
        else:
            safe_result["framework_pre_submission_proof"] = {"object_type": framework_proof.value}
        payload = {
            "schema": "investigation-tool-observation.v1",
            "tool_name": invocation.tool_name,
            "schema_version": invocation.schema_version,
            "operation_key": invocation.operation_key,
            "result": safe_result,
        }
        observation = await self._recorder.record_json(
            investigation_id=invocation.investigation_id,
            kind=EvidenceKind.OTHER,
            payload=payload,
            source=invocation.tool_name,
            source_version=invocation.schema_version,
            object_type=(
                framework_proof.value
                if framework_proof is not None
                else PROVIDER_AUTHORIZATION_REFUSAL_OBJECT_TYPE
                if provider_auth_refusal
                else download_refusal_object_type
                or (
                    "tool_result"
                    if invocation.tool_name in {"vt.download_sample", "sample.analyze"}
                    else "tool_call"
                )
            ),
            object_id=(
                invocation.operation_key
                if framework_proof is not None
                else invocation.tool_name
                if invocation.tool_name in {"vt.download_sample", "sample.analyze"}
                else invocation.operation_key
            ),
            limitations=("Tool output is untrusted provider or analysis data.",),
            invocation_ref=invocation.id,
            idempotency_key=f"tool_call:{invocation.operation_key}",
        )
        return (observation,)


class ProductionArtifactContextSnapshotRepository:
    """Keep a pending Bridge context in immutable canonical blob storage."""

    def __init__(self, artifact_store: ProductionArtifactStore) -> None:
        self._artifact_store = artifact_store

    async def store(self, canonical_json: str) -> UUID:
        try:
            payload = json.loads(canonical_json)
        except json.JSONDecodeError as exc:
            raise ValueError("Context snapshot must be valid JSON") from exc
        if (
            not isinstance(payload, dict)
            or canonical_json_bytes(payload).decode() != canonical_json
        ):
            raise ValueError("Context snapshot must use canonical JSON encoding")
        blob_id, _ = await self._artifact_store.put_canonical_json(
            {"schema": "investigation-context-snapshot.v1", "context": payload},
            bucket="investigation-context-snapshots",
        )
        return blob_id

    async def load(self, snapshot_id: UUID) -> str | None:
        stored = await self._artifact_store.read_json(snapshot_id)
        if stored.get("schema") != "investigation-context-snapshot.v1" or not isinstance(
            stored.get("context"), dict
        ):
            return None
        return canonical_json_bytes(stored["context"]).decode()


class InvestigationService:
    def __init__(
        self,
        *,
        post_synthesis: AnalystPostSynthesisService,
        investigations: AnalystInvestigationRepository,
        states: InvestigationStateRepository,
        context_snapshots: InvestigationContextSnapshotRepository,
        envelopes: InvestigationEnvelopeRepository,
        context_builder: ResearchContextBuilder,
        bridge: BridgeInvestigator,
        registry: ToolRegistry,
        dispatcher: ToolDispatcher,
        invocations: ToolInvocationRepository,
        observations: EvidenceObservationRepository,
        findings: FindingRepository,
        opinions: AnalystOpinionRepository,
        sample_associations: SampleAssociationRepository | None = None,
        samples: SampleRepository | None = None,
        source_documents: SourceDocumentRepository | None = None,
        budget_snapshots: InvestigationBudgetRepository | None = None,
        observation_writer: ToolObservationWriter | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        evidence_summaries: FrameworkProofDescriptorReader | None = None,
        available_capabilities: frozenset[str] = frozenset(),
        author_id: str = "bridge-investigator",
        no_progress_limit: int = 2,
        max_turns_per_run: int = 64,
        commit_uow: Callable[[], Awaitable[None]] | None = None,
        rollback_uow: Callable[[], Awaitable[None]] | None = None,
        audit_decisions: AnalystDecisionRepository | None = None,
        effective_policy_for: Callable[[UUID], Awaitable[Any | None]] | None = None,
    ) -> None:
        if no_progress_limit < 1 or max_turns_per_run < 1:
            raise ValueError("Investigation loop limits must be positive")
        self._post_synthesis = post_synthesis
        self._investigations = investigations
        self._states = states
        self._context_snapshots = context_snapshots
        self._envelopes = envelopes
        self._context_builder = context_builder
        self._bridge = bridge
        self._registry = registry
        self._dispatcher = dispatcher
        self._invocations = invocations
        self._observations = observations
        self._findings = findings
        self._opinions = opinions
        self._sample_associations = sample_associations
        self._samples = samples
        self._source_documents = source_documents
        self._budget_snapshots = budget_snapshots
        self._observation_writer = observation_writer
        self._artifact_store = artifact_store
        self._evidence_summaries = evidence_summaries
        self._available_capabilities = frozenset(available_capabilities)
        self._author_id = author_id
        self._no_progress_limit = no_progress_limit
        self._max_turns_per_run = max_turns_per_run
        self._commit_uow = commit_uow
        self._rollback_uow = rollback_uow
        self._effective_policy_for = effective_policy_for
        owner = getattr(commit_uow, "__self__", None)
        self._audit_decisions = audit_decisions or getattr(owner, "analyst_decisions", None)

    @property
    def available_capabilities(self) -> frozenset[str]:
        return self._available_capabilities

    @staticmethod
    def external_llm_allowed(
        snapshot: ArticleSnapshot, envelope: InvestigationEnvelope | None
    ) -> bool:
        return InvestigationService._external_allowed(snapshot, envelope)

    @staticmethod
    def vt_external_disclosure_allowed(
        snapshot: ArticleSnapshot,
        envelope: InvestigationEnvelope | None,
        family: str,
    ) -> bool:
        if not InvestigationService._external_tlp_allowed(snapshot, envelope):
            return False
        assert envelope is not None
        return bool(
            snapshot.external_llm_allowed
            and envelope.disclosure.external_llm_allowed
            and not external_effect_source_do_not_submit_blocks(family, snapshot.do_not_submit)
        )

    @staticmethod
    def bridge_disclosure_allowed(
        snapshot: ArticleSnapshot, envelope: InvestigationEnvelope | None
    ) -> bool:
        if not InvestigationService._external_tlp_allowed(snapshot, envelope):
            return False
        assert envelope is not None
        return bool(snapshot.external_llm_allowed and envelope.disclosure.external_llm_allowed)

    @staticmethod
    def unapproved_budget_for(investigation: AnalystInvestigation) -> InvestigationBudget:
        return InvestigationService._unapproved_budget(investigation)

    async def commit_current_uow(self) -> None:
        await self._commit_current_uow()

    async def start(
        self,
        *,
        run: ProductionRun,
        synthesis: ProductionArtifact,
        extraction_artifacts: Iterable[ProductionArtifact],
        extraction_items: Iterable[Any],
        policy: AnalystHandoffPolicy,
        envelope: InvestigationEnvelope | None,
        agenda: InvestigationAgenda | None = None,
        hypotheses: tuple[Hypothesis, ...] = (),
        execution_mode: InvestigationExecutionMode | None = None,
        uow: Any | None = None,
    ) -> InvestigationWorkingState:
        """Create/reuse the post-synthesis investigation and its version-one state."""
        handoff = await self._post_synthesis.ensure_for_verified_synthesis(
            run=run,
            synthesis=synthesis,
            extraction_artifacts=tuple(extraction_artifacts),
            extraction_items=tuple(extraction_items),
            policy=policy,
            uow=uow,
        )
        if handoff is None:
            raise ValueError("No investigation was created from the verified synthesis")
        investigation = await self._investigations.get(handoff.investigation_id)
        if investigation is None or investigation.synthesis_artifact_id != synthesis.id:
            raise ValueError("Post-synthesis handoff does not point to the requested synthesis")
        existing_state = await self._states.get(investigation.id)
        if existing_state is not None:
            if execution_mode is not None and existing_state.execution_mode is not execution_mode:
                raise ValueError("Investigation execution mode is immutable after state creation")
            return existing_state
        if envelope is not None and envelope.investigation_id != investigation.id:
            raise ValueError("Investigation envelope belongs to another investigation")
        existing_envelope = await self._envelopes.get(investigation.id)
        if existing_envelope is None and envelope is not None:
            envelope = await self._envelopes.save_if_version(envelope, expected_version=None)
            await self._save_budget_snapshot(envelope)
        elif (
            existing_envelope is not None
            and envelope is not None
            and existing_envelope.approval_decision_id != envelope.approval_decision_id
        ):
            raise ValueError("An idempotent start cannot replace the approved envelope")
        elif existing_envelope is not None:
            envelope = existing_envelope

        snapshot = await self._context_builder.article_snapshots.resolve(synthesis.id)
        resolved_agenda = agenda or _agenda_from_snapshot(snapshot)
        state = InvestigationWorkingState(
            investigation_id=investigation.id,
            agenda=resolved_agenda,
            hypotheses=hypotheses,
            base_publication_artifact_id=snapshot.base_publication_artifact_id,
            base_publication_sha256=snapshot.base_publication_sha256,
            execution_mode=execution_mode or InvestigationExecutionMode.BRIDGE_LED,
        )
        await self._states.save_if_version(state, expected_version=None)
        if investigation.status is AnalystInvestigationStatus.QUEUED:
            investigation.start()
            await self._investigations.save(investigation)
        return state

    async def resume(
        self,
        investigation_id: UUID,
        *,
        expected_version: int | None = None,
        owner_job_terminal: bool = False,
    ) -> InvestigationWorkingState:
        state = await self._require_state(investigation_id)
        if expected_version is not None and state.version != expected_version:
            raise InvestigationStateVersionConflict("Investigation state version changed")
        investigation = await self._require_investigation(investigation_id)
        if (
            state.execution_mode is InvestigationExecutionMode.BRIDGE_LED
            and owner_job_terminal
            and state.pending_context_sha256 is not None
        ):
            attempt = investigation_bridge_attempt(state.recent_actions, state.turn_index)
            disposition = await self._bridge.reconcile_orphaned_turn(
                investigation_id=investigation_id,
                turn_index=state.turn_index,
                context_sha256=state.pending_context_sha256,
                attempt=attempt,
                owner_job_terminal=True,
            )
            envelope = await self._envelopes.get(investigation_id)
            if envelope is not None:
                changed = False
                for prior_attempt in range(attempt + 1):
                    reservation_key = bridge_budget_operation_key(
                        investigation_id=investigation_id,
                        turn_index=state.turn_index,
                        context_sha256=state.pending_context_sha256,
                        attempt=prior_attempt,
                    )
                    status = _reservation_status(envelope.budget, reservation_key)
                    if status is not BudgetReservationStatus.RESERVED:
                        continue
                    if prior_attempt < attempt or disposition == "orphaned_running_turn":
                        envelope.budget.release(reservation_key)
                        changed = True
                    elif disposition in {"captured_output", "needs_review"}:
                        envelope.budget.commit(reservation_key)
                        changed = True
                if changed:
                    await self._save_envelope_version(envelope)
                    await self._commit_current_uow()
            if disposition == "needs_review":
                raise ValueError(
                    "The pending Bridge turn has output or provider state requiring reconciliation"
                )
            if disposition == "active_job":
                raise ValueError("The pending Bridge turn still has an active owner job")
            if disposition == "orphaned_running_turn":
                state = await self._require_state(investigation_id)
        running_invocations = tuple(
            item
            for item in await self._invocations.list_for_investigation(investigation_id)
            if item.status is ToolInvocationStatus.RUNNING
        )
        for invocation in running_invocations:
            reviewed = invocation.transition(ToolInvocationStatus.NEEDS_REVIEW)
            await self._invocations.update_if_version(reviewed, expected_version=invocation.version)
        if running_invocations:
            await self._commit_current_uow()
        proof_resolved = False
        proof_rejections: dict[UUID, str] = {}
        for invocation in await self._unresolved_tool_invocations(investigation_id):
            if invocation.status is not ToolInvocationStatus.NEEDS_REVIEW:
                continue
            proof, rejection = await self._framework_pre_submission_proof(invocation)
            if proof is None:
                if rejection is not None:
                    proof_rejections[invocation.id] = rejection
                continue
            resolved = invocation.resolve_pre_submission_failure()
            await self._invocations.update_if_version(resolved, expected_version=invocation.version)
            await self._complete_tool_reservation(
                investigation_id=invocation.investigation_id,
                operation_key=invocation.operation_key,
                result=ToolResult(
                    status=ToolInvocationStatus.FAILED,
                    error_code=proof.value,
                    framework_pre_submission_proof=proof,
                ),
                reserved_amounts=invocation.reserved_budget,
                uncertain=False,
                external_effect=True,
            )
            proof_resolved = True
        if proof_resolved:
            await self._commit_current_uow()
        unresolved = await self._unresolved_tool_invocations(investigation_id)
        if unresolved:
            unresolved_details = []
            for item in unresolved[:8]:
                rejection = proof_rejections.get(item.id)
                unresolved_details.append(
                    f"{item.id} ({rejection})" if rejection is not None else str(item.id)
                )
            raise ValueError(
                "Ambiguous tool invocations must be reconciled before resuming: "
                + ", ".join(unresolved_details)
            )
        if (
            state.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
            and state.recent_actions
            and state.recent_actions[-1].startswith(_INVALID_BRIDGE_ACTION_PREFIX)
        ):
            raise ValueError("A new user request is required after invalid Bridge output")
        if state.stop_reason is not None:
            resumed = state.resume()
            await self._save_state(state, resumed)
            state = resumed
        if investigation.status in {
            AnalystInvestigationStatus.AWAITING_REVIEW,
            AnalystInvestigationStatus.COMPLETED,
            AnalystInvestigationStatus.EXHAUSTED,
            AnalystInvestigationStatus.CANCELLED,
        }:
            investigation.status = AnalystInvestigationStatus.RUNNING
            investigation.finished_at = None
            investigation._bump()
            await self._investigations.save(investigation)
        return state

    async def _unresolved_tool_invocations(
        self, investigation_id: UUID
    ) -> tuple[ToolInvocation, ...]:
        return tuple(
            item
            for item in await self._invocations.list_for_investigation(investigation_id)
            if item.status in {ToolInvocationStatus.RUNNING, ToolInvocationStatus.NEEDS_REVIEW}
        )

    async def _framework_pre_submission_proof(
        self, invocation: ToolInvocation
    ) -> tuple[FrameworkPreSubmissionProof | None, str | None]:
        envelope = await self._envelopes.get(invocation.investigation_id)
        if envelope is None:
            return None, None
        if not invocation.response_refs:
            return None, None
        multiple_refs = len(invocation.response_refs) > 1
        if multiple_refs and invocation.tool_name != "advisor.consult":
            return None, "framework_proof_reference_count_mismatch"
        # Advisor results can reference both source evidence and the framework
        # proof artifact. Verify each ref instead of requiring a single ref.
        if len(invocation.response_refs) != len(set(invocation.response_refs)):
            return None, "framework_proof_reference_count_mismatch"
        referenced_observations: list[EvidenceObservation] = []
        for observation_id in invocation.response_refs:
            observation = await self._observations.get(observation_id)
            if observation is None or observation.investigation_id != invocation.investigation_id:
                return None, "framework_proof_reference_count_mismatch"
            referenced_observations.append(observation)
        reservation = next(
            (
                item
                for item in envelope.budget.reservations
                if item.operation_key == invocation.operation_key
            ),
            None,
        )
        if (
            reservation is None
            or reservation.status
            not in {BudgetReservationStatus.RELEASED, BudgetReservationStatus.RESERVED}
            or dict(reservation.amounts) != dict(invocation.reserved_budget)
        ):
            return None, None
        first_rejection: str | None = None
        for observation in referenced_observations:
            if (
                observation.invocation_ref != invocation.id
                or observation.source != invocation.tool_name
                or observation.source_version != invocation.schema_version
                or observation.object_id != invocation.operation_key
            ):
                continue
            try:
                proof = FrameworkPreSubmissionProof(observation.object_type)
            except ValueError:
                if (
                    observation.object_type not in {"tool_call", "tool_result"}
                    or invocation.status is not ToolInvocationStatus.NEEDS_REVIEW
                    or invocation.consumed_budget
                ):
                    continue
                proof = FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
            if (
                multiple_refs
                and proof is not FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
            ):
                continue
            rejection = await self._verify_framework_pre_submission_proof(
                invocation, observation, proof
            )
            if rejection is None:
                return proof, None
            if first_rejection is None:
                first_rejection = rejection
        if (
            first_rejection is None
            and invocation.response_refs
            and (self._artifact_store is None or self._evidence_summaries is None)
        ):
            first_rejection = "framework_proof_verifier_unavailable"
        return None, first_rejection

    async def _verify_framework_pre_submission_proof(
        self,
        invocation: ToolInvocation,
        observation: EvidenceObservation,
        proof: FrameworkPreSubmissionProof,
    ) -> str | None:
        if self._artifact_store is None or self._evidence_summaries is None:
            return "framework_proof_verifier_unavailable"
        try:
            descriptor = await self._evidence_summaries.blob_metadata(observation.blob_id)
        except Exception:
            return "framework_proof_descriptor_unreadable"
        if descriptor is None:
            return "framework_proof_descriptor_missing"
        descriptor_sha256 = descriptor.get("sha256")
        descriptor_size = descriptor.get("byte_size")
        mime_type = descriptor.get("mime_type")
        if (
            descriptor_sha256 != observation.sha256
            or isinstance(descriptor_size, bool)
            or not isinstance(descriptor_size, int)
            or descriptor_size < 0
            or not isinstance(mime_type, str)
            or mime_type.split(";", 1)[0].strip().casefold() != "application/json"
        ):
            return "framework_proof_descriptor_mismatch"
        if descriptor_size > _MAX_FRAMEWORK_PRE_SUBMISSION_PROOF_BYTES:
            return "framework_proof_blob_size_overflow"
        try:
            raw = await self._artifact_store.read_bytes(
                observation.blob_id,
                max_bytes=_MAX_FRAMEWORK_PRE_SUBMISSION_PROOF_BYTES,
            )
        except BlobReadLimitExceededError:
            return "framework_proof_blob_size_overflow"
        except Exception:
            return "framework_proof_blob_unreadable"
        if not isinstance(raw, bytes):
            return "framework_proof_blob_unreadable"
        if len(raw) != descriptor_size:
            return "framework_proof_blob_size_mismatch"
        if hashlib.sha256(raw).hexdigest() != observation.sha256:
            return "framework_proof_blob_hash_mismatch"
        try:
            payload = json.loads(raw)
        except Exception:
            return "framework_proof_payload_invalid_json"
        if not isinstance(payload, dict):
            return "framework_proof_payload_mismatch"
        try:
            canonical_payload = ProductionArtifactStore.canonical_json_bytes(payload)
        except (TypeError, ValueError):
            return "framework_proof_payload_invalid_json"
        if canonical_payload != raw:
            return "framework_proof_payload_not_canonical"
        result = payload.get("result")
        if (
            payload.get("schema") != "investigation-tool-observation.v1"
            or payload.get("tool_name") != invocation.tool_name
            or payload.get("schema_version") != invocation.schema_version
            or payload.get("operation_key") != invocation.operation_key
            or not isinstance(result, dict)
        ):
            return "framework_proof_payload_mismatch"
        if observation.object_type == proof.value:
            proof_payload = result.get("framework_pre_submission_proof")
            if not isinstance(proof_payload, dict) or proof_payload != {
                "object_type": observation.object_type
            }:
                return "framework_proof_payload_mismatch"
            return None

        legacy_refusal_codes = {
            "virustotal_external_effect_refused_source_references_mismatch",
            "vt_refused_source_references_mismatch",
        }
        legacy_refusal_code = next(
            (
                value
                for value in (
                    result.get("error_code"),
                    result.get("refusal_reason"),
                    result.get("reason"),
                )
                if isinstance(value, str)
            ),
            None,
        )
        if (
            proof is not FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
            or observation.object_type not in {"tool_call", "tool_result"}
            or not invocation.tool_name.startswith("vt.")
            or result.get("status") != ToolInvocationStatus.NEEDS_REVIEW.value
            or result.get("outcome") != "external_effect_unconfirmed"
            or result.get("adapter_output_archived") is not False
            or result.get("external_effect_claim_id") is not None
            or result.get("claim_id") is not None
            or legacy_refusal_code not in legacy_refusal_codes
            or "framework_pre_submission_proof" in result
        ):
            return "framework_proof_payload_mismatch"
        return None

    async def interrupt(
        self, investigation_id: UUID, *, expected_version: int | None = None
    ) -> InvestigationWorkingState:
        return await self.conclude(
            investigation_id,
            StopReason.USER_INTERRUPT,
            expected_state_version=expected_version,
        )

    async def conclude(
        self,
        investigation_id: UUID,
        reason: StopReason,
        *,
        expected_state_version: int | None = None,
    ) -> InvestigationWorkingState:
        state = await self._require_state(investigation_id)
        if expected_state_version is not None and state.version != expected_state_version:
            raise InvestigationStateVersionConflict("Investigation state version changed")
        investigation = await self._require_investigation(investigation_id)
        updated = state.conclude(reason)
        if updated is not state:
            await self._save_state(state, updated)
        if investigation.status in {
            AnalystInvestigationStatus.RUNNING,
            AnalystInvestigationStatus.AWAITING_REVIEW,
        }:
            previous_version = investigation.version
            investigation.conclude(reason)
            if investigation.version != previous_version:
                await self._investigations.save(investigation)
        return updated

    async def run(self, investigation_id: UUID) -> InvestigationWorkingState:
        state = await self._require_state(investigation_id)
        investigation = await self._require_investigation(investigation_id)
        if state.execution_mode is InvestigationExecutionMode.OPERATOR_LED:
            return state
        if state.stop_reason is not None:
            return state
        if investigation.status is AnalystInvestigationStatus.QUEUED:
            investigation.start()
            await self._investigations.save(investigation)
        if investigation.status is not AnalystInvestigationStatus.RUNNING:
            return state

        for _ in range(self._max_turns_per_run):
            state = await self._require_state(investigation_id)
            if state.stop_reason is not None:
                return state
            envelope = await self._envelopes.get(investigation_id)
            if envelope is None and state.turn_index >= investigation.budget.max_cycles:
                return await self.conclude(investigation_id, StopReason.BUDGET_EXHAUSTED)
            budget = (
                envelope.budget if envelope is not None else self._unapproved_budget(investigation)
            )
            snapshot = await self._context_builder.article_snapshots.resolve(
                investigation.synthesis_artifact_id
            )
            tool_context = ToolExecutionContext(
                investigation_id=investigation_id,
                envelope=envelope,
                budget=budget,
                idempotency_key=f"context:{state.turn_index}",
                available_capabilities=self._available_capabilities,
            )
            if state.pending_context_snapshot_id is not None:
                pending_json = await self._context_snapshots.load(state.pending_context_snapshot_id)
                if (
                    pending_json is None
                    or hashlib.sha256(pending_json.encode("utf-8")).hexdigest()
                    != state.pending_context_sha256
                ):
                    return await self.conclude(investigation_id, StopReason.SUBMISSION_NEEDS_REVIEW)
                context = parse_investigation_context(pending_json)
            else:
                context = await self._context_builder.build(
                    investigation_id=investigation_id,
                    synthesis_artifact_id=investigation.synthesis_artifact_id,
                    state=state,
                    tool_context=tool_context,
                    budget_remaining={
                        dimension.value: amount for dimension, amount in budget.remaining.items()
                    },
                    registry=self._registry,
                )
            context_bytes = context.canonical_bytes()
            stopped = await self._requested_stop(investigation_id)
            if stopped is not None:
                return stopped
            allowed = self.bridge_disclosure_allowed(snapshot, envelope)
            if not allowed:
                # Context construction is the local read path; no model transport runs.
                action = self._external_policy_block_action(snapshot, envelope, state.turn_index)
                if action not in state.recent_actions:
                    updated = replace(
                        state,
                        version=state.version + 1,
                        recent_actions=(*state.recent_actions, action)[-_RECENT_ACTIONS_LIMIT:],
                    )
                    await self._save_state(state, updated)
                    await self._commit_current_uow()
                    state = updated
                return await self.conclude(investigation_id, StopReason.MODEL_UNAVAILABLE)

            async def before_bridge_submission(
                approved_envelope: InvestigationEnvelope | None = envelope,
            ) -> bool:
                return await self._bridge_submission_allowed(investigation_id, approved_envelope)

            context_size_bytes = len(context_bytes)
            context_sha256 = hashlib.sha256(context_bytes).hexdigest()
            user_request_ids = tuple(
                dict.fromkeys(
                    item.request_id
                    for item in (
                        *context.operator_requests,
                        *context.investigation.user_requests,
                    )
                )
            )
            if state.pending_context_snapshot_id is None:
                context_snapshot_id = await self._context_snapshots.store(
                    context_bytes.decode("utf-8")
                )
                pending = state.with_pending_context(context_snapshot_id, context_sha256)
                await self._save_state(state, pending)
                state = pending
            stopped = await self._requested_stop(investigation_id)
            if stopped is not None:
                return stopped
            bridge_attempt = investigation_bridge_attempt(state.recent_actions, state.turn_index)
            bridge_key = bridge_budget_operation_key(
                investigation_id=investigation_id,
                turn_index=state.turn_index,
                context_sha256=context_sha256,
                attempt=bridge_attempt,
            )
            try:
                reservation_before = _reservation_status(budget, bridge_key)
                budget.reserve(
                    bridge_key,
                    {
                        BudgetDimension.BRIDGE_TURNS: 1,
                        BudgetDimension.CONTEXT_BYTES: context_size_bytes,
                        BudgetDimension.CONTEXT_TOKENS: (context_size_bytes + 3) // 4,
                    },
                )
            except BudgetExceeded:
                return await self.conclude(investigation_id, StopReason.BUDGET_EXHAUSTED)
            if envelope is not None and reservation_before is not BudgetReservationStatus.COMMITTED:
                envelope = await self._save_envelope_version(envelope)
                # Acquisition tools use their own UoW. Commit this approved
                # budget snapshot before the Bridge/adapter can read it there.
                await self._commit_current_uow()
            bridge_external_effect_authority: ExternalEffectAuthority | None = None
            if envelope is not None and self._budget_snapshots is not None:
                bridge_reservation = next(
                    (
                        item
                        for item in envelope.budget.reservations
                        if item.operation_key == bridge_key
                        and item.status is BudgetReservationStatus.RESERVED
                    ),
                    None,
                )
                if bridge_reservation is not None:
                    authority_kind = (
                        ExternalEffectAuthorityKind.OSINT_POLICY
                        if envelope.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY
                        else ExternalEffectAuthorityKind.HUMAN_G2
                    )
                    bridge_external_effect_authority = ExternalEffectAuthority(
                        investigation_id=investigation_id,
                        invocation_id=None,
                        operation_key=bridge_key,
                        reservation_amounts={
                            (
                                dimension.value
                                if isinstance(dimension, BudgetDimension)
                                else str(dimension)
                            ): int(amount)
                            for dimension, amount in bridge_reservation.amounts.items()
                        },
                        expected_envelope_version=envelope.version,
                        approval_decision_id=envelope.approval_decision_id,
                        invocation_arguments_sha256=None,
                        authority_kind=authority_kind,
                        policy_event_id=envelope.inherited_policy_event_id,
                        policy_version=(
                            str(envelope.inherited_policy_version)
                            if envelope.inherited_policy_version is not None
                            else None
                        ),
                        pinned_subject_id=(
                            investigation.subject_id
                            if authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY
                            else None
                        ),
                    )
            try:
                bridge_external_effect_evidence_refs = (
                    await self._bridge_context_evidence_references(
                        investigation_id=investigation_id,
                        context=context,
                        snapshot=snapshot,
                    )
                    if (
                        bridge_external_effect_authority is not None
                        and self._bridge.requires_external_effect_authority
                    )
                    else ()
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    self._complete_bridge_reservation(
                        investigation_id,
                        bridge_key,
                        budget,
                        blocked=True,
                        confirmed_non_submission=True,
                    )
                )
                await asyncio.shield(self._commit_current_uow())
                raise
            except Exception as exc:
                _logger.info(
                    "Bridge external-effect source resolution refused (%s)",
                    type(exc).__name__,
                )
                await self._complete_bridge_reservation(
                    investigation_id,
                    bridge_key,
                    budget,
                    blocked=True,
                    confirmed_non_submission=True,
                )
                await self._commit_current_uow()
                return await self.conclude(investigation_id, StopReason.MODEL_UNAVAILABLE)
            try:
                turn = await self._bridge.turn(
                    investigation_id=investigation_id,
                    subject_id=investigation.subject_id,
                    turn_index=state.turn_index,
                    attempt=bridge_attempt,
                    context=context,
                    external_llm_allowed=allowed,
                    on_invalid_output=lambda key, detail: self._record_bridge_rejection(
                        investigation_id, key, detail
                    ),
                    before_submission=before_bridge_submission,
                    external_effect_binding_context={
                        "authority_context": bridge_external_effect_authority,
                        "evidence_refs": bridge_external_effect_evidence_refs,
                    },
                )
            except asyncio.CancelledError as exc:
                # Once transport submission starts, the external provider may
                # have accepted the request. A cancellation while waiting between
                # confirmed pre-submission failures has no such external effect.
                submitted = getattr(exc, "bridge_submissions_made", 1)
                await asyncio.shield(
                    self._complete_bridge_reservation(
                        investigation_id,
                        bridge_key,
                        budget,
                        blocked=submitted == 0,
                    )
                )
                await asyncio.shield(self._commit_current_uow())
                raise
            await self._complete_bridge_reservation(
                investigation_id,
                bridge_key,
                budget,
                blocked=turn.submissions_made == 0,
                confirmed_non_submission=(
                    turn.status is TurnSubmissionStatus.PRE_SUBMISSION_RETRY_EXHAUSTED
                    and turn.submission_state == "pre_submission"
                    and turn.submissions_made == 0
                ),
            )
            await self._commit_current_uow()
            state = await self._require_state(investigation_id)
            if state.stop_reason is not None:
                return state
            if turn.status is TurnSubmissionStatus.PRE_SUBMISSION_RETRY_EXHAUSTED:
                next_attempt = bridge_attempt + turn.attempts_made
                action = (
                    "bridge_turn_pre_submission_retry_exhausted:"
                    f"{state.turn_index}:next_attempt={next_attempt}"
                )
                if action not in state.recent_actions:
                    updated = replace(
                        state,
                        version=state.version + 1,
                        recent_actions=(*state.recent_actions, action)[-32:],
                    )
                    await self._save_state(state, updated)
                    await self._commit_current_uow()
                    state = updated
            latest_envelope = await self._envelopes.get(investigation_id)
            if _envelope_reduced_since(envelope, latest_envelope, family="bridge.turn"):
                await self._record_policy_reduction(
                    investigation_id=investigation_id,
                    attempted_operation="bridge.turn",
                    approved_envelope=envelope,
                    current_envelope=latest_envelope,
                    budget=budget,
                )
                return await self.conclude(investigation_id, StopReason.PERMISSION_REQUIRED)
            # Bridge reservation completion writes a newer envelope version. Pass
            # that durable snapshot onward so tool reservations do not CAS against
            # the pre-Bridge version.
            envelope = latest_envelope
            if envelope is not None:
                budget = envelope.budget
            stop = await self._bridge_stop(turn)
            if stop is not None:
                if turn.status is TurnSubmissionStatus.INVALID_OUTPUT:
                    rejected = state.with_bridge_validation_failure(turn.detail)
                    await self._save_state(state, rejected)
                    state = rejected
                concluded = await self.conclude(investigation_id, stop)
                await self._commit_current_uow()
                return concluded
            decision = turn.decision
            if decision is None:
                return await self.conclude(investigation_id, StopReason.MODEL_UNAVAILABLE)
            validation_issue = validate_decision_hypothesis_references(
                decision,
                known_hypothesis_ids={item.id for item in state.hypotheses},
            )
            if validation_issue is not None:
                rejected = state.with_bridge_validation_failure(
                    f"decision_rejected:{validation_issue.code.value}"
                )
                await self._save_state(state, rejected)
                concluded = await self.conclude(
                    investigation_id, StopReason.SUBMISSION_NEEDS_REVIEW
                )
                await self._commit_current_uow()
                return concluded
            seen_operation_keys = {
                _operation_key_base(item.operation_key)
                for item in await self._invocations.list_for_investigation(investigation_id)
            }
            outcome = await self._process_turn(
                investigation=investigation,
                envelope=envelope,
                budget=budget,
                state=state,
                decision=decision,
                context=context,
                context_sha256=turn.context_sha256,
                user_request_ids=user_request_ids,
            )
            latest_state = outcome[0]
            investigation = await self._require_investigation(investigation_id)
            next_action = decision.next_action
            if outcome[1] is not None:
                return await self.conclude(investigation_id, outcome[1])
            if next_action is AgentNextAction.FINISH:
                opinion_id = investigation_opinion_id(investigation_id)
                opinion = await self._opinions.get(opinion_id)
                opinion_eligible_findings = tuple(
                    item
                    for item in await self._findings.list_for_investigation(investigation_id)
                    if item.assessment is not FindingAssessment.INCONCLUSIVE
                )
                rejected_opinion_attempt = (
                    f"opinion_attempt_rejected:{turn.context_sha256}" in latest_state.recent_actions
                )
                missing_required_opinion = bool(opinion_eligible_findings) and not (
                    opinion and opinion.renderable_blocks
                )
                if missing_required_opinion or rejected_opinion_attempt:
                    retries = sum(
                        action.startswith(
                            (
                                "opinion_draft_required:",
                                "opinion_rejected_requires_correction:",
                            )
                        )
                        for action in latest_state.recent_actions
                    )
                    current_envelope = await self._envelopes.get(investigation_id)
                    remaining_turns = (
                        current_envelope.budget.remaining.get(BudgetDimension.BRIDGE_TURNS)
                        if current_envelope is not None
                        else budget.remaining.get(BudgetDimension.BRIDGE_TURNS)
                    )
                    if retries >= MAX_OPINION_DRAFT_CORRECTIONS or remaining_turns == 0:
                        stopped = latest_state.conclude(StopReason.SUBMISSION_NEEDS_REVIEW)
                        await self._save_state(latest_state, stopped)
                        await self._commit_current_uow()
                        return stopped
                    correction_code = (
                        "opinion_rejected_requires_correction"
                        if rejected_opinion_attempt
                        else "opinion_draft_required"
                    )
                    correction = (
                        f"{correction_code}: "
                        + (
                            "a proposed OPINION block was rejected; correct it or omit it, and "
                            "do not repeat the invalid block; "
                            if rejected_opinion_attempt
                            else "non-inconclusive findings exist without a renderable opinion; "
                            "use existing findings and evidence references; "
                        )
                        + f"correction={retries + 1}/{MAX_OPINION_DRAFT_CORRECTIONS}"
                    )
                    corrected = replace(
                        latest_state,
                        version=latest_state.version + 1,
                        recent_actions=(*latest_state.recent_actions, correction)[-32:],
                    )
                    await self._save_state(latest_state, corrected)
                    await self._commit_current_uow()
                    state = corrected
                    continue
                pending_rule_tests, unavailable_rule_tests = _rule_test_finish_status(
                    context,
                    await self._invocations.list_for_investigation(investigation_id),
                )
                if pending_rule_tests:
                    required_markers = tuple(
                        f"rule_test_required:{candidate_id}:v{version}"
                        for candidate_id, version in pending_rule_tests
                    )
                    already_requested = all(
                        marker in latest_state.recent_actions for marker in required_markers
                    )
                    if already_requested:
                        stopped = latest_state.conclude(StopReason.SUBMISSION_NEEDS_REVIEW)
                        await self._save_state(latest_state, stopped)
                        await self._commit_current_uow()
                        return stopped
                    current_envelope = await self._envelopes.get(investigation_id)
                    remaining_turns = (
                        current_envelope.budget.remaining.get(BudgetDimension.BRIDGE_TURNS)
                        if current_envelope is not None
                        else budget.remaining.get(BudgetDimension.BRIDGE_TURNS)
                    )
                    if remaining_turns == 0:
                        stopped = latest_state.conclude(StopReason.SUBMISSION_NEEDS_REVIEW)
                        await self._save_state(latest_state, stopped)
                        await self._commit_current_uow()
                        return stopped
                    missing_markers = tuple(
                        marker
                        for marker in required_markers
                        if marker not in latest_state.recent_actions
                    )
                    correction = (
                        "rule_test_required: run rule.test with each exact candidate_id "
                        "and version "
                        "before finish: "
                        + ", ".join(
                            f"{candidate_id}@v{version}"
                            for candidate_id, version in pending_rule_tests
                        )
                        + "; if rule.test is unavailable or refuses, record that limitation."
                    )
                    corrected = replace(
                        latest_state,
                        version=latest_state.version + 1,
                        recent_actions=(
                            *latest_state.recent_actions,
                            *missing_markers,
                            correction,
                        )[-32:],
                    )
                    await self._save_state(latest_state, corrected)
                    await self._commit_current_uow()
                    state = corrected
                    continue
                if unavailable_rule_tests:
                    limitation_markers = tuple(
                        f"rule_test_unavailable:{candidate_id}:v{version}: "
                        "candidate remains untested"
                        for candidate_id, version in unavailable_rule_tests
                    )
                    new_limitations = tuple(
                        marker
                        for marker in limitation_markers
                        if marker not in latest_state.recent_actions
                    )
                    if new_limitations:
                        recorded = replace(
                            latest_state,
                            version=latest_state.version + 1,
                            recent_actions=(
                                *latest_state.recent_actions,
                                *new_limitations,
                            )[-32:],
                        )
                        await self._save_state(latest_state, recorded)
                        await self._commit_current_uow()
                pending_samples = _pending_sample_analysis(
                    context,
                    await self._invocations.list_for_investigation(investigation_id),
                )
                analysis_tools = tuple(
                    item.name
                    for item in context.available_tools
                    if item.name == "sample.analyze" or item.name.startswith("ghidra.")
                )
                if pending_samples and analysis_tools:
                    current_envelope = await self._envelopes.get(investigation_id)
                    remaining_turns = (
                        current_envelope.budget.remaining.get(BudgetDimension.BRIDGE_TURNS)
                        if current_envelope is not None
                        else budget.remaining.get(BudgetDimension.BRIDGE_TURNS)
                    )
                    explicit_reason = _sample_blocked_reason(decision.reasoning_summary)
                    sample_invocations = tuple(
                        item
                        for item in await self._invocations.list_for_investigation(investigation_id)
                        if item.tool_name == "sample.analyze"
                        or item.tool_name.startswith("ghidra.")
                    )
                    engine_unavailable = explicit_reason == "engine_unavailable" and all(
                        all(
                            (
                                latest := _latest_applicable_analysis_invocation(
                                    sample_invocations,
                                    association_id=association_id,
                                    sample_sha256=sample_sha256,
                                    tool_name=tool_name,
                                )
                            )
                            is not None
                            and latest.status is ToolInvocationStatus.UNAVAILABLE
                            for tool_name in analysis_tools
                        )
                        for association_id, sample_sha256 in pending_samples
                    )
                    if remaining_turns != 0 and not engine_unavailable:
                        fingerprint = hashlib.sha256(
                            "|".join(
                                f"{association_id}:{sample_sha256}"
                                for association_id, sample_sha256 in pending_samples
                            ).encode("utf-8")
                        ).hexdigest()[:16]
                        refusal_count = _sample_finish_refusal_count(
                            latest_state.recent_actions, fingerprint
                        )
                        if refusal_count < MAX_SAMPLE_ANALYSIS_FINISH_REFUSALS:
                            association_ids = ", ".join(
                                association_id for association_id, _sha256 in pending_samples
                            )
                            next_analysis_step = (
                                "call sample.analyze with association_id and evidence_refs"
                                if "sample.analyze" in analysis_tools
                                else "call an available ghidra.* tool with the sample_sha256"
                            )
                            correction = (
                                "finish_gate: acquired samples remain unanalyzed; "
                                f"{next_analysis_step} for "
                                f"{association_ids}. Refusal "
                                f"{refusal_count + 1}/{MAX_SAMPLE_ANALYSIS_FINISH_REFUSALS}."
                            )
                            corrected = replace(
                                latest_state,
                                version=latest_state.version + 1,
                                recent_actions=(
                                    *latest_state.recent_actions,
                                    f"finish_gate_refused:{fingerprint}:{refusal_count + 1}",
                                    correction,
                                )[-32:],
                            )
                            await self._save_state(latest_state, corrected)
                            await self._commit_current_uow()
                            state = corrected
                            continue
                return await self.conclude(investigation_id, StopReason.AGENT_FINISHED)
            if next_action is AgentNextAction.REQUEST_PERMISSION:
                return await self.conclude(investigation_id, StopReason.PERMISSION_REQUIRED)
            if (
                latest_state.no_progress_streak >= self._no_progress_limit
                and next_action is not AgentNextAction.SWITCH_HYPOTHESIS
                and not self._has_unseen_operation(investigation_id, decision, seen_operation_keys)
            ):
                return await self.conclude(investigation_id, StopReason.NO_NEW_INFORMATION)
            state = latest_state
        # Bound one worker invocation; the investigation remains resumable and active.
        return state

    async def execute_tool_call(
        self,
        *,
        investigation: AnalystInvestigation,
        envelope: InvestigationEnvelope | None,
        budget: InvestigationBudget,
        state: InvestigationWorkingState,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        purpose: str,
        operation_key: str,
        actor_id: str | None = None,
        check_stop_before_dispatch: bool = True,
    ) -> tuple[ToolInvocation, ToolResult, tuple[UUID, ...], bool]:
        """Persist budget and RUNNING invocation before using the shared dispatcher."""
        registered = self._registry.get(tool_name, schema_version)
        definition = registered[0] if registered else None
        requested_cost = thaw_json(definition.cost_model) if definition is not None else {}
        existing: ToolInvocation | None = None
        advisor_preflight_refusal_expected = False
        if registered is not None:
            adapter = registered[1]
            existing_lookup = getattr(self._invocations, "get_by_operation_key", None)
            existing = await existing_lookup(operation_key) if callable(existing_lookup) else None
            estimate_cost = getattr(adapter, "reservation_cost", None)
            if existing is None and callable(estimate_cost):
                estimate_budget = _copy_investigation_budget(budget)
                estimate_envelope = (
                    replace(envelope, budget=estimate_budget) if envelope is not None else None
                )
                estimate_context = ToolExecutionContext(
                    investigation_id=investigation.id,
                    envelope=estimate_envelope,
                    budget=estimate_budget,
                    idempotency_key=operation_key,
                    available_capabilities=self._available_capabilities,
                )
                try:
                    dynamic_cost = await estimate_cost(arguments, estimate_context)
                except Exception as exc:
                    # Fail closed. The adapter's pre-transport reservation check
                    # will refuse any non-empty prompt without a measured cost.
                    dynamic_cost = {
                        BudgetDimension.CONTEXT_BYTES: 0,
                        BudgetDimension.CONTEXT_TOKENS: 0,
                    }
                    _logger.info(
                        "Tool dynamic cost estimation failed for %s (%s)",
                        tool_name,
                        type(exc).__name__,
                    )
                requested_cost.update(dynamic_cost)
                advisor_preflight_refusal_expected = (
                    tool_name == "advisor.consult"
                    and dynamic_cost.get(BudgetDimension.CONTEXT_BYTES, 0) == 0
                    and dynamic_cost.get(BudgetDimension.CONTEXT_TOKENS, 0) == 0
                )
        external_effect = definition is not None and definition.effect in {
            ToolEffect.EXTERNAL_READ,
            ToolEffect.ACQUIRE,
            ToolEffect.EXTERNAL_ACTIVE,
            ToolEffect.EXTERNAL_SUBMIT,
        }
        invocation_parameters: Mapping[str, Any] = arguments
        external_effect_evidence_refs: tuple[EvidenceReference, ...] = ()
        if tool_name in _VT_COORDINATED_READ_TOOLS:
            if existing is not None:
                invocation_parameters = existing.normalized_parameters
                external_effect_evidence_refs = _vt_source_refs_from_parameters(
                    invocation_parameters
                )
            else:
                if tool_name == "vt.download_sample":
                    # Resolve the target with the same policy gate as file_report.
                    # Caller supplied evidence_refs cannot authorize the hash.
                    external_effect_evidence_refs = await self._trusted_vt_source_references(
                        investigation=investigation,
                        tool_name="vt.file_report",
                        arguments={"file_hash": arguments.get("sha256")},
                        envelope=envelope,
                    )
                else:
                    external_effect_evidence_refs = await self._trusted_vt_source_references(
                        investigation=investigation,
                        tool_name=tool_name,
                        arguments=arguments,
                        envelope=envelope,
                    )
                invocation_parameters = {
                    **arguments,
                    _VT_SOURCE_REFS_PARAMETER: [
                        reference.to_json() for reference in external_effect_evidence_refs
                    ],
                }
        elif tool_name == "advisor.consult" and existing is not None:
            invocation_parameters = existing.normalized_parameters
        elif tool_name == "advisor.consult" and advisor_preflight_refusal_expected:
            # The local advisor preflight has already established that no prompt
            # can be submitted. Keep only non-free-text identity fields in the
            # durable invocation; the operation key still binds the full input.
            consultation_type = arguments.get("consultation_type")
            invocation_parameters = {
                **(
                    {"consultation_type": consultation_type}
                    if isinstance(consultation_type, str)
                    else {}
                ),
                _ADVISOR_PREFLIGHT_REFUSAL_PARAMETER: True,
            }
        invocation = ToolInvocation(
            investigation_id=investigation.id,
            operation_key=operation_key,
            tool_name=tool_name,
            schema_version=schema_version,
            normalized_parameters=invocation_parameters,
            purpose=purpose,
            reserved_budget=requested_cost,
        )
        invocation = await self._invocations.add_if_absent(invocation)
        if tool_name in _VT_COORDINATED_READ_TOOLS:
            invocation_parameters = invocation.normalized_parameters
            external_effect_evidence_refs = _vt_source_refs_from_parameters(invocation_parameters)
        admission_reservation: BudgetReservation | None = None
        if invocation.status is ToolInvocationStatus.PENDING:
            # A durable invocation is the source of truth for an operation's cost.
            # This also makes a replay safe if the registry's cost model changed
            # after the original request was journaled.
            cost = invocation.reserved_budget
            try:
                admission_reservation = budget.reserve(operation_key, cost)
            except BudgetExceeded:
                # The dispatcher repeats the same admission check and returns a
                # typed pre-dispatch refusal. No reservation is created here.
                pass
            if (
                admission_reservation is not None
                and admission_reservation.status is not BudgetReservationStatus.RESERVED
            ):
                reviewed = invocation.transition(ToolInvocationStatus.NEEDS_REVIEW)
                invocation = await self._invocations.update_if_version(
                    reviewed, expected_version=invocation.version
                )
                await self._commit_current_uow()
                return (
                    invocation,
                    ToolResult(
                        status=ToolInvocationStatus.NEEDS_REVIEW,
                        message=(
                            "The operation already has a terminal budget reservation; "
                            "reconcile it before dispatch."
                        ),
                        error_code="reservation_not_active",
                    ),
                    invocation.response_refs,
                    external_effect,
                )
            running = invocation.transition(ToolInvocationStatus.RUNNING)
            invocation = await self._invocations.update_if_version(
                running, expected_version=invocation.version
            )
        else:
            # A durable record wins over dispatcher memory. Never turn an existing
            # invocation into a second external call.
            if invocation.status is ToolInvocationStatus.RUNNING:
                reviewed = invocation.transition(ToolInvocationStatus.NEEDS_REVIEW)
                invocation = await self._invocations.update_if_version(
                    reviewed, expected_version=invocation.version
                )
                await self._commit_current_uow()
                return (
                    invocation,
                    ToolResult(
                        status=ToolInvocationStatus.NEEDS_REVIEW,
                        observation_refs=invocation.response_refs,
                        message="Prior tool attempt needs reconciliation; it was not redispatched.",
                        error_code="invocation_needs_review",
                    ),
                    invocation.response_refs,
                    external_effect,
                )
            return (
                invocation,
                ToolResult(
                    status=invocation.status,
                    observation_refs=invocation.response_refs,
                    message="Reused durable tool invocation; it was not redispatched.",
                ),
                invocation.response_refs,
                False,
            )

        cost = invocation.reserved_budget
        if envelope is not None:
            try:
                envelope = await self._save_envelope_version(envelope)
            except VersionConflictError:
                # Admission did not reach durable storage, so the adapter is
                # provably unreachable. Persist that pre-submission outcome rather
                # than leaving a RUNNING invocation that would block a safe retry.
                budget.release_confirmed_non_submission(operation_key)
                failed = invocation.transition(ToolInvocationStatus.FAILED)
                invocation = await self._invocations.update_if_version(
                    failed, expected_version=invocation.version
                )
                await self._commit_current_uow()
                failure = ToolResult(
                    status=ToolInvocationStatus.FAILED,
                    error_code="envelope_admission_conflict",
                    framework_pre_submission_proof=(
                        FrameworkPreSubmissionProof.ENVELOPE_ADMISSION_CONFLICT
                    ),
                    message=(
                        "The approved envelope changed before the reservation was persisted; "
                        "the tool was not dispatched."
                    ),
                    output={"reason": "envelope_admission_conflict"},
                )
                _log_terminal_tool_failure(invocation, failure)
                return invocation, failure, (), external_effect
            budget = envelope.budget
        persisted_reservation = next(
            (
                item
                for item in (envelope.budget.reservations if envelope is not None else ())
                if item.operation_key == operation_key
            ),
            None,
        )
        reservation_persisted = (
            persisted_reservation is not None
            and persisted_reservation.status is BudgetReservationStatus.RESERVED
            and dict(persisted_reservation.amounts) == dict(cost)
        )
        # The RUNNING journal and its reservation are committed before the effect.
        await self._commit_current_uow()

        if check_stop_before_dispatch:
            stopped = await self._requested_stop(investigation.id)
            if stopped is not None:
                budget.release(operation_key)
                cancelled = invocation.transition(ToolInvocationStatus.FAILED)
                invocation = await self._invocations.update_if_version(
                    cancelled, expected_version=invocation.version
                )
                if envelope is not None:
                    current_envelope = await self._envelopes.get(investigation.id)
                    if current_envelope is not None:
                        current_envelope.budget.release(operation_key)
                        await self._save_envelope_version(current_envelope)
                await self._commit_current_uow()
                failure = ToolResult(
                    status=ToolInvocationStatus.FAILED,
                    exception_class="OperatorInterruption",
                    error_code="operator_interrupted_before_tool_dispatch",
                    framework_pre_submission_proof=(
                        FrameworkPreSubmissionProof.OPERATOR_INTERRUPTED
                    ),
                )
                _log_terminal_tool_failure(invocation, failure)
                return invocation, failure, (), external_effect

        tool_budget = _copy_investigation_budget(budget)
        tool_envelope = replace(envelope, budget=tool_budget) if envelope is not None else None
        external_effect_authority: ExternalEffectAuthority | None = None
        if (
            (tool_name in _VT_COORDINATED_READ_TOOLS or tool_name == "advisor.consult")
            and envelope is not None
            and reservation_persisted
            and persisted_reservation is not None
            and self._budget_snapshots is not None
        ):
            authority_kind = (
                ExternalEffectAuthorityKind.OSINT_POLICY
                if envelope.authority_kind is EnvelopeAuthorityKind.OSINT_POLICY
                else ExternalEffectAuthorityKind.HUMAN_G2
            )
            reservation_amounts = {
                (
                    dimension.value if isinstance(dimension, BudgetDimension) else str(dimension)
                ): int(amount)
                for dimension, amount in persisted_reservation.amounts.items()
            }
            external_effect_authority = ExternalEffectAuthority(
                investigation_id=investigation.id,
                invocation_id=invocation.id,
                operation_key=operation_key,
                reservation_amounts=reservation_amounts,
                expected_envelope_version=envelope.version,
                approval_decision_id=envelope.approval_decision_id,
                invocation_arguments_sha256=hashlib.sha256(
                    canonical_json_bytes(
                        arguments
                        if tool_name == "advisor.consult"
                        else invocation.normalized_parameters
                    )
                ).hexdigest(),
                authority_kind=authority_kind,
                policy_event_id=envelope.inherited_policy_event_id,
                policy_version=(
                    str(envelope.inherited_policy_version)
                    if envelope.inherited_policy_version is not None
                    else None
                ),
                pinned_subject_id=(
                    investigation.subject_id
                    if authority_kind is ExternalEffectAuthorityKind.OSINT_POLICY
                    else None
                ),
            )
        tool_context = ToolExecutionContext(
            investigation_id=investigation.id,
            envelope=tool_envelope,
            budget=tool_budget,
            idempotency_key=operation_key,
            available_capabilities=self._available_capabilities,
            external_effect_authority=external_effect_authority,
            external_effect_evidence_refs=external_effect_evidence_refs,
            actor_id=actor_id or "agent:bridge-investigator",
            purpose=purpose,
        )

        async def check_current_admission(
            _context: ToolExecutionContext,
        ) -> FrameworkPreSubmissionProof | None:
            if not external_effect or envelope is None:
                return None
            current_envelope = await self._envelopes.get(investigation.id)
            if (
                current_envelope is None
                or current_envelope.approval_decision_id != envelope.approval_decision_id
                or _envelope_reduced_since(envelope, current_envelope, family=tool_name)
            ):
                return FrameworkPreSubmissionProof.ENVELOPE_REVOKED
            if reservation_persisted:
                current_reservation = next(
                    (
                        item
                        for item in current_envelope.budget.reservations
                        if item.operation_key == operation_key
                    ),
                    None,
                )
                if (
                    current_reservation is None
                    or current_reservation.status is not BudgetReservationStatus.RESERVED
                    or dict(current_reservation.amounts) != dict(cost)
                ):
                    return FrameworkPreSubmissionProof.RESERVATION_CHANGED
            if tool_name in _VT_COORDINATED_READ_TOOLS:
                current_investigation = await self._investigations.get(investigation.id)
                if current_investigation is None:
                    return FrameworkPreSubmissionProof.INVESTIGATION_MISSING
                if tool_name == "vt.download_sample":
                    raw_hash = arguments.get("sha256")
                    try:
                        target_hash = (
                            normalize_file_hash(raw_hash) if isinstance(raw_hash, str) else ""
                        )
                    except (TypeError, ValueError, VirusTotalInvalidInputError):
                        target_hash = ""
                    if (
                        len(target_hash) == 64
                        and sum(
                            reference.kind == "sample_hash"
                            for reference in external_effect_evidence_refs
                        )
                        != 1
                    ):
                        return FrameworkPreSubmissionProof.DISCLOSURE_REVOKED
                snapshot = await self._context_builder.article_snapshots.resolve(
                    current_investigation.synthesis_artifact_id
                )
                if not self.vt_external_disclosure_allowed(
                    snapshot, current_envelope, tool_name
                ) or await self._vt_source_disclosure_restricted(
                    current_investigation,
                    external_effect_evidence_refs,
                    tool_name,
                    envelope=current_envelope,
                ):
                    return FrameworkPreSubmissionProof.DISCLOSURE_REVOKED
            elif tool_name == "advisor.consult":
                current_investigation = await self._investigations.get(investigation.id)
                if current_investigation is None:
                    return FrameworkPreSubmissionProof.INVESTIGATION_MISSING
                snapshot = await self._context_builder.article_snapshots.resolve(
                    current_investigation.synthesis_artifact_id
                )
                if not self.bridge_disclosure_allowed(snapshot, current_envelope):
                    return FrameworkPreSubmissionProof.DISCLOSURE_REVOKED
            return None

        result = await self._dispatcher.dispatch(
            tool_context,
            tool_name=tool_name,
            schema_version=schema_version,
            arguments=arguments,
            purpose=purpose,
            operation_key=operation_key,
            reservation_amounts=cost,
            # Keep the pre-dispatch envelope, reservation and source checks.
            # The coordinator repeats the authoritative checks under its consume lock.
            pre_dispatch_check=check_current_admission,
        )
        advisor_admission_recheck = (
            tool_name == "advisor.consult"
            and result.output.get("failure_stage") == "post_preparation_admission"
        )
        if (
            tool_name == "advisor.consult"
            and not advisor_admission_recheck
            and result.framework_pre_submission_proof
            is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
        ):
            output = dict(result.output)
            if output.get("advisor_status") == "needs_review":
                output["advisor_status"] = "refused"
            result = replace(
                result,
                status=ToolInvocationStatus.FAILED,
                output=output,
            )
        framework_proves_non_execution = (
            _confirmed_external_non_execution(result) and not advisor_admission_recheck
        )
        unproven_external_failure = (
            external_effect
            and result.status
            in {
                ToolInvocationStatus.FAILED,
                ToolInvocationStatus.UNAVAILABLE,
                ToolInvocationStatus.POLICY_DENIED,
                ToolInvocationStatus.NEEDS_REVIEW,
            }
            and not framework_proves_non_execution
        )
        ambiguous_adapter_response = (
            unproven_external_failure and result.framework_pre_submission_proof is None
        )
        trusted_vt_passive_read = (
            tool_name in _VT_PASSIVE_READ_TOOLS
            and definition is not None
            and definition.effect is ToolEffect.EXTERNAL_READ
            and registered is not None
            and is_trusted_vt_passive_read_adapter(tool_name, registered[1])
        )
        if unproven_external_failure and result.status is not ToolInvocationStatus.NEEDS_REVIEW:
            result = replace(
                result,
                status=ToolInvocationStatus.NEEDS_REVIEW,
                message=(
                    (
                        "The passive VirusTotal read returned without a confirmed outcome; "
                        "a fresh invocation can retry under current admission checks. "
                        if trusted_vt_passive_read
                        else "The external operation returned without a confirmed outcome; "
                        "reconcile it before resuming. "
                    )
                    + result.message
                )[:2_000],
            )
        uncertain_external = unproven_external_failure
        retryable_vt_read_uncertainty = uncertain_external and trusted_vt_passive_read
        if framework_proves_non_execution:
            budget.release_confirmed_non_submission(operation_key)
        elif not uncertain_external and result.status in {
            ToolInvocationStatus.UNAVAILABLE,
            ToolInvocationStatus.POLICY_DENIED,
        }:
            budget.release(operation_key)
        try:
            if ambiguous_adapter_response:
                recorded_refs = await self._valid_observation_refs(
                    investigation.id, result.observation_refs
                )
                persisted_observations = (
                    ()
                    if recorded_refs
                    else await self._persist_ambiguous_result_observation(invocation)
                )
            else:
                persisted_observations = await self._persist_result_observations(invocation, result)
        except Exception as exc:
            valid_refs = await self._valid_observation_refs(
                investigation.id, result.observation_refs
            )
            if (
                tool_name == "advisor.consult"
                and framework_proves_non_execution
                and result.framework_pre_submission_proof
                is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
            ):
                failed = invocation.transition(
                    ToolInvocationStatus.FAILED,
                    response_refs=valid_refs,
                    consumed_budget={},
                )
                invocation = await self._invocations.update_if_version(
                    failed, expected_version=invocation.version
                )
                await self._complete_tool_reservation(
                    investigation_id=investigation.id,
                    operation_key=operation_key,
                    result=result,
                    reserved_amounts=invocation.reserved_budget,
                    uncertain=False,
                    external_effect=True,
                )
                await self._commit_current_uow()
                _log_terminal_tool_failure(
                    invocation,
                    result,
                    exception_class=type(exc).__name__,
                    error_code="tool_observation_persistence_failed",
                )
                return invocation, result, valid_refs, False
            reviewed = invocation.transition(
                ToolInvocationStatus.NEEDS_REVIEW,
                response_refs=valid_refs,
                consumed_budget=(
                    dict(result.budget_consumed) if retryable_vt_read_uncertainty else {}
                ),
            )
            invocation = await self._invocations.update_if_version(
                reviewed, expected_version=invocation.version
            )
            if retryable_vt_read_uncertainty:
                await self._complete_tool_reservation(
                    investigation_id=investigation.id,
                    operation_key=operation_key,
                    result=result,
                    reserved_amounts=invocation.reserved_budget,
                    uncertain=True,
                    external_effect=True,
                    retryable_external_read=True,
                )
            await self._commit_current_uow()
            _log_terminal_tool_failure(
                invocation,
                result,
                exception_class=type(exc).__name__,
                error_code="tool_observation_persistence_failed",
            )
            return invocation, result, valid_refs, True
        result_refs = list(result.observation_refs)
        result_refs.extend(item.id for item in persisted_observations)
        valid_refs = await self._valid_observation_refs(
            investigation.id, tuple(dict.fromkeys(result_refs))
        )
        if uncertain_external:
            coordinator_transition = await self._invocations.get(invocation.id)
            if (
                coordinator_transition is not None
                and coordinator_transition.status is ToolInvocationStatus.NEEDS_REVIEW
            ):
                # The coordinator durably marks the invocation when it records
                # an ambiguous post-handoff result. Continue from that version
                # so the framework diagnostic can attach without a stale CAS.
                invocation = coordinator_transition
        terminal = invocation.transition(
            result.status,
            response_refs=valid_refs,
            consumed_budget=(
                {}
                if (
                    (uncertain_external and not retryable_vt_read_uncertainty)
                    or _confirmed_external_non_execution(result)
                )
                else _actual_tool_consumption(tool_context.budget, operation_key, result)
            ),
        )
        invocation = await self._invocations.update_if_version(
            terminal, expected_version=invocation.version
        )
        await self._complete_tool_reservation(
            investigation_id=investigation.id,
            operation_key=operation_key,
            result=result,
            reserved_amounts=invocation.reserved_budget,
            uncertain=uncertain_external,
            external_effect=external_effect,
            retryable_external_read=retryable_vt_read_uncertainty,
        )
        await self._commit_current_uow()
        _log_terminal_tool_failure(invocation, result)
        return invocation, result, valid_refs, uncertain_external

    async def _verified_local_download_reuse(
        self,
        investigation: AnalystInvestigation,
        references: tuple[EvidenceReference, ...],
    ) -> bool:
        """Prove a local quarantine hit before exempting it from egress disclosure."""
        hash_references = tuple(
            reference for reference in references if reference.kind == "sample_hash"
        )
        if (
            len(hash_references) != 1
            or self._samples is None
            or self._artifact_store is None
            or self._evidence_summaries is None
        ):
            return False
        requested_hash = hash_references[0].id
        if len(requested_hash) != 64:
            return False
        try:
            if normalize_file_hash(requested_hash) != requested_hash:
                return False
        except (TypeError, ValueError, VirusTotalInvalidInputError):
            return False
        samples = await self._samples.list_by_subject_and_sha256(
            investigation.subject_id, requested_hash
        )
        if len(samples) != 1:
            return False
        sample = samples[0]
        if (
            sample.external_llm_allowed
            or sample.expected_hash not in {None, requested_hash}
            or sample.state not in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
        ):
            return False
        descriptor = await self._evidence_summaries.blob_metadata(sample.blob_id)
        if descriptor is None:
            return False
        size = descriptor.get("byte_size")
        if (
            descriptor.get("sha256") != requested_hash
            or descriptor.get("logical_bucket") != SAMPLES_QUARANTINE_BUCKET
            or isinstance(size, bool)
            or not isinstance(size, int)
            or size < 0
        ):
            return False
        try:
            content = await self._artifact_store.read_bytes(sample.blob_id, max_bytes=size)
        except Exception:
            return False
        return len(content) == size and hashlib.sha256(content).hexdigest() == requested_hash

    async def _vt_source_disclosure_restricted(
        self,
        investigation: AnalystInvestigation,
        references: tuple[EvidenceReference, ...],
        family: str,
        *,
        envelope: InvestigationEnvelope | None = None,
    ) -> bool:
        sample_hashes = {
            reference.id for reference in references if reference.kind == "sample_hash"
        }
        observation_hashes: set[str] = set()
        hashes = set(sample_hashes)
        document_ids = {
            reference.id for reference in references if reference.kind == "source_document"
        }
        for reference in references:
            if reference.kind != "evidence_observation":
                continue
            try:
                observation_id = UUID(reference.id)
            except ValueError:
                return True
            observation = await self._observations.get(observation_id)
            if (
                observation is None
                or observation.investigation_id != investigation.id
                or observation.source_version != reference.version
                or observation.sha256 != reference.sha256
            ):
                return True
            object_type = observation.object_type.casefold()
            if object_type in {"sample", "file", "sample_observation"} or (
                sample_hashes and object_type == "sha256"
            ):
                try:
                    sample_hash = normalize_file_hash(observation.object_id)
                except (TypeError, ValueError):
                    return True
                if len(sample_hash) != 64:
                    return True
                observation_hashes.add(sample_hash)
                hashes.add(sample_hash)
            elif object_type in {"source_document", "document"}:
                document_ids.add(observation.object_id)
        sample_associations = self._sample_associations
        sample_repository = self._samples
        if hashes:
            allow_unobserved_hash = await self._osint_open_vt_hash_reads_allowed(
                investigation, envelope
            )
            if not allow_unobserved_hash and (
                sample_associations is None or sample_repository is None
            ):
                return True
            associations = (
                await sample_associations.list_for_investigation(investigation.id)
                if sample_associations is not None
                else ()
            )
            associated_hashes = {association.sample_sha256 for association in associations}
            allow_unassociated_search_observations = (
                family == "vt.intelligence_search" and allow_unobserved_hash
            )
            if any(
                reference.kind == "sample_hash" for reference in references
            ) and not observation_hashes.issubset(sample_hashes):
                return True
            ungrounded_hashes = sample_hashes - (associated_hashes | observation_hashes)
            if ungrounded_hashes and not allow_unobserved_hash:
                return True
            if (
                observation_hashes - (associated_hashes | sample_hashes)
                and not allow_unassociated_search_observations
            ):
                return True
        for sample_hash in hashes:
            if sample_repository is None:
                if (
                    not allow_unobserved_hash
                    or sample_hash in observation_hashes
                    or sample_hash in associated_hashes
                ):
                    return True
                continue
            samples = await sample_repository.list_by_subject_and_sha256(
                investigation.subject_id, sample_hash
            )
            if any(
                sample.tlp is TLP.RED
                or external_effect_source_do_not_submit_blocks(family, sample.do_not_submit)
                or bool((sample.license_restriction or "").strip())
                for sample in samples
            ):
                return True
        if document_ids:
            source_documents = self._source_documents
            if source_documents is None:
                return True
            for raw_document_id in document_ids:
                try:
                    document_id = UUID(raw_document_id)
                except ValueError:
                    return True
                document = await source_documents.get(document_id)
                if (
                    document is None
                    or document.subject_id != investigation.subject_id
                    or document.tlp is TLP.RED
                    or external_effect_source_do_not_submit_blocks(family, document.do_not_submit)
                    or bool((document.license_restriction or "").strip())
                ):
                    return True
        return False

    async def _trusted_vt_source_references(
        self,
        *,
        investigation: AnalystInvestigation,
        tool_name: str,
        arguments: Mapping[str, Any],
        envelope: InvestigationEnvelope | None = None,
    ) -> tuple[EvidenceReference, ...]:
        """Resolve hash refs from active OSINT authority or local evidence, never caller claims."""
        hash_value: Any = None
        if tool_name in {"vt.file_report", "vt.behaviour", "vt.behaviour_pcap"}:
            hash_value = arguments.get("file_hash")
        elif tool_name == "vt.relationships" and arguments.get("object_type") == "file":
            hash_value = arguments.get("object_id")
        if hash_value is not None:
            try:
                normalized_hash = normalize_file_hash(str(hash_value))
            except (TypeError, ValueError, VirusTotalInvalidInputError):
                return ()
            if len(normalized_hash) != 64:
                return ()
            hash_reference = EvidenceReference(
                kind="sample_hash",
                id=normalized_hash,
                version="sample-hash.v1",
                sha256=normalized_hash,
            )
            associations = (
                await self._sample_associations.list_for_investigation(investigation.id)
                if self._sample_associations is not None
                else ()
            )
            if any(item.sample_sha256 == normalized_hash for item in associations):
                return (hash_reference,)
            observations = await self._observations.list_for_investigation(investigation.id)
            matching_observations = tuple(
                item
                for item in observations
                if item.object_type.casefold() in {"sample", "file", "sample_observation", "sha256"}
                and item.object_id == normalized_hash
            )
            if matching_observations:
                return (
                    hash_reference,
                    *(
                        EvidenceReference(
                            kind="evidence_observation",
                            id=str(item.id),
                            version=item.source_version,
                            sha256=item.sha256,
                        )
                        for item in sorted(matching_observations, key=lambda value: str(value.id))
                    ),
                )
            if await self._osint_open_vt_hash_reads_allowed(investigation, envelope):
                return (hash_reference,)
            return ()

        if tool_name == "vt.intelligence_search":
            raw_refs = arguments.get("evidence_refs")
            if not isinstance(raw_refs, (list, tuple)) or not raw_refs:
                return ()
            resolved_observations: dict[UUID, EvidenceObservation] = {}
            for raw_ref in raw_refs:
                try:
                    reference_id = UUID(str(raw_ref))
                except (TypeError, ValueError, AttributeError):
                    return ()
                observation = await self._observations.get(reference_id)
                if observation is not None and observation.investigation_id == investigation.id:
                    resolved_observations[observation.id] = observation
                    continue
                finding = await self._findings.get(reference_id)
                if finding is None or finding.investigation_id != investigation.id:
                    return ()
                for observation_id in finding.observation_refs:
                    cited = await self._observations.get(observation_id)
                    if cited is None or cited.investigation_id != investigation.id:
                        return ()
                    resolved_observations[cited.id] = cited
            return tuple(
                EvidenceReference(
                    kind="evidence_observation",
                    id=str(item.id),
                    version=item.source_version,
                    sha256=item.sha256,
                )
                for item in sorted(resolved_observations.values(), key=lambda value: str(value.id))
            )

        if tool_name not in {"vt.object_report", "vt.relationships"}:
            return ()
        raw_kind = str(arguments.get("object_type", ""))
        raw_object_id = arguments.get("object_id")
        object_type = {
            "ip_addresses": VirusTotalObjectType.IP_ADDRESS,
            "ip_address": VirusTotalObjectType.IP_ADDRESS,
            "domains": VirusTotalObjectType.DOMAIN,
            "domain": VirusTotalObjectType.DOMAIN,
            "urls": VirusTotalObjectType.URL,
            "url": VirusTotalObjectType.URL,
        }.get(raw_kind)
        if object_type is None or not isinstance(raw_object_id, str):
            return ()
        try:
            normalized_object_id = _normalize_object_id(object_type, raw_object_id)
            snapshot = await self._context_builder.article_snapshots.resolve(
                investigation.synthesis_artifact_id
            )
        except Exception:
            return ()
        expected_indicator = {
            VirusTotalObjectType.IP_ADDRESS: "ipv4",
            VirusTotalObjectType.DOMAIN: "domain",
            VirusTotalObjectType.URL: "url",
        }[object_type]
        document_refs: dict[str, EvidenceReference] = {}
        for indicator in snapshot.confirmed_iocs:
            if indicator.indicator_type != expected_indicator:
                continue
            try:
                candidate = _normalize_object_id(object_type, indicator.value)
            except (TypeError, ValueError):
                continue
            if candidate != normalized_object_id:
                continue
            version = f"{snapshot.synthesis_artifact_id}@{snapshot.synthesis_version or 1}"
            for document_id in indicator.source_document_ids:
                reference = EvidenceReference(
                    kind="source_document",
                    id=str(document_id),
                    version=version,
                    sha256=snapshot.synthesis_sha256,
                )
                document_refs[reference.id] = reference
        return tuple(document_refs[key] for key in sorted(document_refs))

    async def _osint_open_vt_hash_reads_allowed(
        self,
        investigation: AnalystInvestigation,
        envelope: InvestigationEnvelope | None,
    ) -> bool:
        if (
            envelope is None
            or envelope.investigation_id != investigation.id
            or envelope.authority_kind is not EnvelopeAuthorityKind.OSINT_POLICY
            or envelope.inherited_policy_id is None
            or envelope.inherited_policy_version is None
            or envelope.inherited_policy_event_id is None
            or "vt" not in envelope.allowed_providers
            or self._effective_policy_for is None
        ):
            return False
        try:
            effective = await self._effective_policy_for(investigation.id)
        except Exception:
            return False
        if effective is None or not effective.authorized:
            return False
        inheritance = effective.inheritance
        policy = effective.policy
        return bool(
            inheritance.investigation_id == investigation.id
            and inheritance.subject_id == investigation.subject_id
            and inheritance.policy_id == envelope.inherited_policy_id
            and inheritance.policy_version == envelope.inherited_policy_version
            and inheritance.policy_event_id == envelope.inherited_policy_event_id
            and policy.policy_id == envelope.inherited_policy_id
            and policy.version == envelope.inherited_policy_version
            and policy.event_id == envelope.inherited_policy_event_id
            and policy.contains_subject(investigation.subject_id)
            and "vt" in policy.capabilities.allowed_providers
        )

    async def _bridge_context_evidence_references(
        self,
        *,
        investigation_id: UUID,
        context: InvestigationContextV1,
        snapshot: ArticleSnapshot,
    ) -> tuple[EvidenceReference, ...]:
        """Resolve canonical sources transmitted in a Bridge context."""
        observation_ids = _bridge_context_observation_ids(context)
        references: dict[tuple[str, str], EvidenceReference] = {}
        for observation_id in sorted(observation_ids, key=str):
            observation = await self._observations.get(observation_id)
            if observation is None or observation.investigation_id != investigation_id:
                raise ExternalEffectValidationError("Bridge context observation is unresolved")
            reference = EvidenceReference(
                kind="evidence_observation",
                id=str(observation.id),
                version=observation.source_version,
                sha256=observation.sha256,
            )
            references[(reference.kind, reference.id)] = reference

        source_document_ids = _bridge_context_source_document_ids(context)
        if source_document_ids:
            if self._source_documents is None:
                raise ExternalEffectValidationError(
                    "Bridge context source documents are unavailable"
                )
            document_version = f"{snapshot.synthesis_artifact_id}@{snapshot.synthesis_version or 1}"
            for document_id in sorted(source_document_ids, key=str):
                document = await self._source_documents.get(document_id)
                if document is None or document.subject_id != snapshot.subject_id:
                    raise ExternalEffectValidationError(
                        "Bridge context source document is unresolved"
                    )
                reference = EvidenceReference(
                    kind="source_document",
                    id=str(document.id),
                    version=document_version,
                    sha256=snapshot.synthesis_sha256,
                )
                references[(reference.kind, reference.id)] = reference

        sample_deltas = context.investigation.sample_deltas
        association_ids: dict[UUID, str] = {}
        for item in sample_deltas:
            raw_association_id = item.get("association_id")
            raw_sha256 = item.get("sha256")
            if raw_association_id is None and raw_sha256 is None:
                continue
            try:
                association_id = UUID(str(raw_association_id))
            except (TypeError, ValueError, AttributeError) as exc:
                raise ExternalEffectValidationError(
                    "Bridge context sample association is invalid"
                ) from exc
            if not isinstance(raw_sha256, str) or not re.fullmatch(r"[0-9a-f]{64}", raw_sha256):
                raise ExternalEffectValidationError("Bridge context sample hash is invalid")
            association_ids[association_id] = raw_sha256

        if association_ids:
            if self._sample_associations is None:
                raise ExternalEffectValidationError(
                    "Bridge context sample associations are unavailable"
                )
            for association_id, expected_sha256 in sorted(
                association_ids.items(), key=lambda item: str(item[0])
            ):
                association = await self._sample_associations.get(association_id)
                if (
                    association is None
                    or association.investigation_id != investigation_id
                    or association.sample_sha256 != expected_sha256
                ):
                    raise ExternalEffectValidationError(
                        "Bridge context sample association is unresolved"
                    )
                reference = EvidenceReference(
                    kind="sample_association_sample",
                    id=str(association.id),
                    version=str(association.version),
                    sha256=association.sample_sha256,
                )
                references[(reference.kind, reference.id)] = reference
        return tuple(references[key] for key in sorted(references))

    def _tool_operation_key(
        self,
        *,
        investigation_id: UUID,
        tool_name: str,
        schema_version: str,
        normalized_parameters: Mapping[str, Any],
        purpose: str,
    ) -> str:
        registered = self._registry.get(tool_name, schema_version)
        cache_identity = None
        if registered is not None and registered[0].effect in {
            ToolEffect.LOCAL_READ,
            ToolEffect.LOCAL_COMPUTE,
        }:
            # Version local deterministic results with their implementation proof.
            # External provider identities stay stable so deployment cannot reissue them.
            cache_identity = tool_operation_cache_identity(registered[1])
        return tool_operation_key(
            investigation_id=investigation_id,
            tool_name=tool_name,
            schema_version=schema_version,
            normalized_parameters=normalized_parameters,
            purpose=purpose,
            cache_identity=cache_identity,
        )

    def _has_unseen_operation(
        self,
        investigation_id: UUID,
        decision: AgentTurnDecisionV1,
        seen_operation_keys: set[str],
    ) -> bool:
        return any(
            self._tool_operation_key(
                investigation_id=investigation_id,
                tool_name=call.tool_name,
                schema_version=call.schema_version,
                normalized_parameters=call.arguments,
                purpose=call.purpose,
            )
            not in seen_operation_keys
            for call in decision.proposed_tool_calls
        )

    async def _process_turn(
        self,
        *,
        investigation: AnalystInvestigation,
        envelope: InvestigationEnvelope | None,
        budget: InvestigationBudget,
        state: InvestigationWorkingState,
        decision: AgentTurnDecisionV1,
        context: InvestigationContextV1,
        context_sha256: str,
        user_request_ids: tuple[UUID, ...],
    ) -> tuple[InvestigationWorkingState, StopReason | None]:
        observation_ids: list[UUID] = []
        finding_ids: list[UUID] = []
        action_summaries: list[str] = []
        progress = False
        turn_envelope = envelope
        for proposed_index, proposed in enumerate(decision.proposed_tool_calls):
            stopped = await self._requested_stop(investigation.id)
            if stopped is not None:
                return stopped, stopped.stop_reason
            prior_invocations = await self._invocations.list_for_investigation(investigation.id)
            prior_observations = await self._observations.list_for_investigation(investigation.id)
            blocked_capabilities = await provider_authorization_blocked_capabilities(
                investigation_id=investigation.id,
                invocations=prior_invocations,
                observations=prior_observations,
            )
            blocked_capabilities = tuple(
                sorted(
                    set(blocked_capabilities).union(
                        provider_download_blocked_capabilities(
                            investigation_id=investigation.id,
                            invocations=prior_invocations,
                            observations=prior_observations,
                        )
                    )
                )
            )
            blocked_providers = {item.split(":", 1)[0] for item in blocked_capabilities}
            blocked_download_capabilities = {
                item.split(":", 1)[0]
                for item in blocked_capabilities
                if item.startswith("vt.file_download:")
            }
            provider = proposed.tool_name.split(".", 1)[0].lower()
            definition_adapter = self._registry.get(proposed.tool_name, proposed.schema_version)
            definition = definition_adapter[0] if definition_adapter else None
            if provider in blocked_providers or (
                definition is not None
                and definition.capability_key in blocked_download_capabilities
            ):
                unavailable_reason = (
                    "The VirusTotal download capability is blocked after repeated provider refusal."
                    if definition is not None
                    and definition.capability_key in blocked_download_capabilities
                    else f"{provider}: provider authorization refused; capability is blocked."
                )
                action_summaries.append(
                    _action_summary(
                        proposed.tool_name,
                        ToolInvocationStatus.UNAVAILABLE,
                        unavailable_reason,
                    )
                )
                continue
            base_operation_key = self._tool_operation_key(
                investigation_id=investigation.id,
                tool_name=proposed.tool_name,
                schema_version=proposed.schema_version,
                normalized_parameters=proposed.arguments,
                purpose=proposed.purpose,
            )
            deduplicate_read = definition is not None and definition.effect in {
                ToolEffect.LOCAL_READ,
                ToolEffect.EXTERNAL_READ,
            }
            call_scope = hashlib.sha256(
                f"{state.turn_index}:{context_sha256}".encode()
            ).hexdigest()[:20]
            operation_key = (
                base_operation_key
                if deduplicate_read
                else f"{base_operation_key}:turn:{call_scope}"
            )
            all_matching_invocations = tuple(
                item
                for item in await self._invocations.list_for_investigation(investigation.id)
                if item.tool_name == proposed.tool_name
                and item.schema_version == proposed.schema_version
                and (
                    _operation_key_base(item.operation_key) == base_operation_key
                    if proposed.tool_name == "advisor.consult"
                    else _tool_call_arguments_match(item.normalized_parameters, proposed.arguments)
                )
            )
            matching_invocations = tuple(
                item
                for item in all_matching_invocations
                if _operation_key_matches(item.operation_key, operation_key)
            )
            invocation = matching_invocations[-1] if matching_invocations else None
            external_effect = definition is not None and definition.effect in {
                ToolEffect.EXTERNAL_READ,
                ToolEffect.ACQUIRE,
                ToolEffect.EXTERNAL_ACTIVE,
                ToolEffect.EXTERNAL_SUBMIT,
            }
            if external_effect:
                unresolved = tuple(
                    item
                    for item in await self._unresolved_tool_invocations(investigation.id)
                    if item.status is ToolInvocationStatus.NEEDS_REVIEW
                    or not _operation_key_matches(item.operation_key, operation_key)
                )
                if unresolved:
                    unresolved_ids = ", ".join(str(item.id) for item in unresolved[:8])
                    action_summaries.append(
                        _action_summary(
                            proposed.tool_name,
                            ToolInvocationStatus.NEEDS_REVIEW,
                            "An earlier ambiguous tool invocation must be reconciled before "
                            f"another external operation: {unresolved_ids}",
                        )
                    )
                    return state, StopReason.SUBMISSION_NEEDS_REVIEW
            definitive_failures = sum(
                item.status is ToolInvocationStatus.FAILED
                and (
                    not item.response_refs
                    or item.normalized_parameters.get(_ADVISOR_PREFLIGHT_REFUSAL_PARAMETER) is True
                )
                for item in all_matching_invocations
            )
            if external_effect and definitive_failures >= MAX_IDENTICAL_DEFINITE_TOOL_FAILURES:
                action_summaries.append(
                    _action_summary(
                        proposed.tool_name,
                        ToolInvocationStatus.FAILED,
                        "Identical arguments have already failed definitively twice without a "
                        "provider result. Correct the argument types or stop retrying this input.",
                    )
                )
                continue
            if (
                invocation is not None
                and invocation.status is ToolInvocationStatus.FAILED
                and (not external_effect or invocation.response_refs)
            ):
                failed_attempts = sum(
                    item.status is ToolInvocationStatus.FAILED for item in matching_invocations
                )
                retry_parent_key = base_operation_key if deduplicate_read else operation_key
                operation_key = f"{retry_parent_key}:retry:{failed_attempts}"
                invocation = await self._invocations.get_by_operation_key(operation_key)
            if invocation is not None and invocation.status not in {
                ToolInvocationStatus.POLICY_DENIED,
                ToolInvocationStatus.FAILED,
            }:
                replay, refs, stop = await self._replay_invocation(invocation)
                observation_ids.extend(refs)
                if set(refs).difference(state.last_seen_evidence_ids):
                    progress = True
                action_summaries.append(
                    _action_summary(invocation.tool_name, replay.status, replay.message)
                )
                if stop is not None:
                    return state, stop
                continue

            current_envelope = envelope
            if external_effect:
                current_envelope = await self._envelopes.get(investigation.id)
                if _envelope_reduced_since(
                    turn_envelope, current_envelope, family=proposed.tool_name
                ):
                    denied_key = (
                        f"{operation_key}:g2-denied:"
                        f"{current_envelope.version if current_envelope is not None else 'revoked'}"
                    )
                    denied = await self._invocations.get_by_operation_key(denied_key)
                    if denied is None:
                        denied = ToolInvocation(
                            investigation_id=investigation.id,
                            operation_key=denied_key,
                            tool_name=proposed.tool_name,
                            schema_version=proposed.schema_version,
                            normalized_parameters=proposed.arguments,
                            purpose=proposed.purpose,
                        )
                        denied = await self._invocations.add_if_absent(denied)
                        if denied.status is ToolInvocationStatus.PENDING:
                            terminal = denied.transition(ToolInvocationStatus.POLICY_DENIED)
                            denied = await self._invocations.update_if_version(
                                terminal, expected_version=denied.version
                            )
                    alternatives = self._alternative_tools(
                        investigation_id=investigation.id,
                        envelope=current_envelope,
                        budget=(
                            current_envelope.budget if current_envelope is not None else budget
                        ),
                        exclude_name=proposed.tool_name,
                    )
                    message = (
                        "G2 authorization changed during this turn; the external operation was "
                        "not started."
                    )
                    if alternatives:
                        message += " Alternatives: " + "; ".join(alternatives[:8])
                    action_summaries.append(
                        _action_summary(
                            proposed.tool_name, ToolInvocationStatus.POLICY_DENIED, message
                        )
                    )
                    await self._commit_current_uow()
                    await self._record_policy_reduction(
                        investigation_id=investigation.id,
                        attempted_operation=proposed.tool_name,
                        approved_envelope=turn_envelope,
                        current_envelope=current_envelope,
                        budget=budget,
                    )
                    return state, StopReason.PERMISSION_REQUIRED
                envelope = current_envelope
                if envelope is not None:
                    budget = envelope.budget

                if (
                    invocation is not None
                    and current_envelope is not None
                    and invocation.status is ToolInvocationStatus.POLICY_DENIED
                    and _tool_allowed_by_envelope(definition, current_envelope)
                ):
                    # A previously denied operation had no external effect. A new G2
                    # version gets a fresh idempotency identity so explicit approval
                    # can retry it without rewriting its denial record.
                    operation_key = f"{operation_key}:g2:{current_envelope.version}"
                    invocation = await self._invocations.get_by_operation_key(operation_key)
                elif invocation is not None:
                    replay, refs, stop = await self._replay_invocation(invocation)
                    observation_ids.extend(refs)
                    action_summaries.append(
                        _action_summary(invocation.tool_name, replay.status, replay.message)
                    )
                    if stop is not None:
                        return state, stop
                    continue
            elif invocation is not None:
                replay, refs, stop = await self._replay_invocation(invocation)
                observation_ids.extend(refs)
                action_summaries.append(
                    _action_summary(invocation.tool_name, replay.status, replay.message)
                )
                if stop is not None:
                    return state, stop
                continue

            if invocation is not None:
                replay, refs, stop = await self._replay_invocation(invocation)
                observation_ids.extend(refs)
                action_summaries.append(
                    _action_summary(invocation.tool_name, replay.status, replay.message)
                )
                if stop is not None:
                    return state, stop
                continue

            invocation, result, valid_refs, uncertain_external = await self.execute_tool_call(
                investigation=investigation,
                envelope=envelope,
                budget=budget,
                state=state,
                tool_name=proposed.tool_name,
                schema_version=proposed.schema_version,
                arguments=proposed.arguments,
                purpose=proposed.purpose,
                operation_key=operation_key,
            )
            observation_ids.extend(valid_refs)
            if set(valid_refs).difference(state.last_seen_evidence_ids):
                progress = True
            if invocation.status is ToolInvocationStatus.NEEDS_REVIEW and uncertain_external:
                return state, StopReason.SUBMISSION_NEEDS_REVIEW
            stopped = await self._requested_stop(investigation.id)
            if stopped is not None:
                return stopped, stopped.stop_reason
            latest_envelope = await self._envelopes.get(investigation.id)
            if external_effect and _envelope_reduced_since(
                turn_envelope, latest_envelope, family=proposed.tool_name
            ):
                await self._record_g2_blocked_calls(
                    investigation_id=investigation.id,
                    proposed_calls=decision.proposed_tool_calls[proposed_index + 1 :],
                    current_envelope=latest_envelope,
                )
                await self._record_policy_reduction(
                    investigation_id=investigation.id,
                    attempted_operation=proposed.tool_name,
                    approved_envelope=turn_envelope,
                    current_envelope=latest_envelope,
                    budget=budget,
                )
                return state, StopReason.PERMISSION_REQUIRED
            # Completing a tool reservation persists a newer envelope version.
            # Keep the next tool's reservation CAS and budget view on that durable
            # checkpoint, while preserving the original turn envelope for G2 checks.
            envelope = latest_envelope
            if envelope is not None:
                budget = envelope.budget
            outcome_message = result.message
            if result.suggested_alternatives:
                outcome_message = f"{outcome_message} Alternatives: " + "; ".join(
                    result.suggested_alternatives[:8]
                )
            if result.output.get("request_permission", False):
                outcome_message = f"{outcome_message} request_permission: true."
            action_summaries.append(
                _action_summary(invocation.tool_name, result.status, outcome_message)
            )
            if result.status is ToolInvocationStatus.NEEDS_REVIEW:
                return state, (
                    StopReason.PERMISSION_REQUIRED
                    if _confirmed_external_non_execution(result)
                    else StopReason.SUBMISSION_NEEDS_REVIEW
                )
            if invocation.tool_name == "advisor.consult" and (
                result.error_code == "advisor_secret_field_remaining"
                or result.output.get("failure_stage")
                in {"secret_preflight", "sample_disclosure_restricted"}
            ):
                return state, StopReason.SUBMISSION_NEEDS_REVIEW
            # An ordinary policy denial is local to the operation; G2 reductions above
            # end this turn before any other proposed external operation is started.

        stopped = await self._requested_stop(investigation.id)
        if stopped is not None:
            return stopped, stopped.stop_reason
        new_findings, deduplicated_findings = await self._persist_findings_with_count(
            investigation_id=investigation.id,
            proposals=decision.finding_proposals,
            decision_trace=decision.reasoning_summary,
        )
        finding_ids.extend(item.id for item in new_findings)
        if any(
            item.id not in state.last_seen_finding_ids
            and item.assessment is not FindingAssessment.INCONCLUSIVE
            for item in new_findings
        ):
            progress = True
        stopped = await self._requested_stop(investigation.id)
        if stopped is not None:
            return stopped, stopped.stop_reason
        valid_anchors = frozenset(
            item.anchor
            for item in (context.article.publication_anchors if context.article is not None else ())
        )
        opinion_written, opinion_rejections = await self._apply_opinion_delta(
            investigation_id=investigation.id,
            state=state,
            delta=decision.opinion_delta,
            valid_anchors=valid_anchors,
        )
        action_summaries.extend(opinion_rejections)
        progress = progress or opinion_written
        stopped = await self._requested_stop(investigation.id)
        if stopped is not None:
            return stopped, stopped.stop_reason
        hypotheses = self._update_hypotheses(
            state.hypotheses,
            decision=decision,
            findings=new_findings,
        )
        decision_sha256 = hashlib.sha256(decision.canonical_bytes()).hexdigest()
        if any(item.startswith("opinion_validation_rejected:") for item in opinion_rejections):
            action_summaries.append(f"opinion_attempt_rejected:{context_sha256}")
        acknowledged_request_ids = tuple(
            request_id
            for request_id in decision.addressed_request_ids
            if request_id in set(user_request_ids)
        )
        turn_state = replace(
            state,
            recent_actions=tuple(
                action
                for action in state.recent_actions
                if not _user_request_was_in_context(action, acknowledged_request_ids)
            ),
        )
        updated = turn_state.with_turn(
            context_sha256=context_sha256,
            decision_sha256=decision_sha256,
            progress=progress,
            evidence_ids=tuple(
                dict.fromkeys(
                    (
                        *state.last_seen_evidence_ids,
                        *(item.evidence_id for item in context.investigation.new_evidence),
                    )
                )
            ),
            finding_ids=tuple(
                dict.fromkeys(
                    (
                        *state.last_seen_finding_ids,
                        *(item.finding_id for item in context.investigation.findings),
                    )
                )
            ),
            action_summaries=tuple(action_summaries),
            hypotheses=hypotheses,
        )
        if deduplicated_findings:
            updated = replace(
                updated,
                deduplicated_findings=(state.deduplicated_findings + deduplicated_findings),
            )
        pending_request_actions = tuple(
            action
            for action in turn_state.recent_actions
            if action.startswith(USER_REQUEST_ACTION_PREFIX)
        )
        pending_request_ids = {
            str(request_id)
            for action in pending_request_actions
            if (request_id := _user_request_id(action)) is not None
        }
        pending_request_markers = tuple(
            action
            for action in state.recent_actions
            if action.startswith("api:user-request:")
            and action.rsplit(":", 1)[-1] in pending_request_ids
        )
        protected_actions = (*pending_request_markers, *pending_request_actions)
        protected_set = set(protected_actions)
        history_actions = tuple(
            action for action in updated.recent_actions if action not in protected_set
        )
        history_limit = max(0, 32 - len(protected_actions))
        if history_limit:
            history_actions = history_actions[-history_limit:]
        else:
            history_actions = ()
        updated = replace(updated, recent_actions=(*history_actions, *protected_actions))
        await self._save_state(state, updated)
        # Tools such as hit acquisition consume the aggregate's LoopBudget in their
        # own unit of work; apply only this turn's mutation on the current row so
        # a stale in-memory copy never overwrites (or is rejected by) their write.
        current = await self._require_investigation(investigation.id)
        current.record_turn(progress=progress)
        await self._investigations.save(current)
        await self._commit_current_uow()
        return updated, None

    async def reconcile_invocation(
        self,
        investigation_id: UUID,
        invocation_id: UUID,
        *,
        outcome: InvocationReconciliationOutcome,
        actor: str,
        reason: str,
        response_refs: tuple[UUID, ...] = (),
        expected_version: int | None = None,
        idempotency_key: str | None = None,
    ) -> ToolInvocation:
        if not actor.strip() or not reason.strip():
            raise ValueError("Invocation reconciliation requires an actor and reason")
        if not isinstance(outcome, InvocationReconciliationOutcome):
            outcome = InvocationReconciliationOutcome(outcome)
        invocation = await self._invocations.get(invocation_id)
        if invocation is None or invocation.investigation_id != investigation_id:
            raise LookupError("Tool invocation does not belong to the investigation")
        refs = await self._valid_observation_refs(investigation_id, tuple(response_refs))
        if len(refs) != len(set(response_refs)):
            raise ValueError("Reconciliation response references must exist in this investigation")
        if outcome is not InvocationReconciliationOutcome.EXECUTED_WITH_RESULT and refs:
            raise ValueError("Only executed_with_result may attach response references")
        if self._audit_decisions is None:
            raise RuntimeError("Invocation reconciliation audit repository is unavailable")

        details = {
            "outcome": outcome.value,
            "invocation_id": str(invocation.id),
            "operation_key": invocation.operation_key,
            "response_refs": [str(item) for item in refs],
        }
        request_identity = {
            **details,
            "investigation_id": str(investigation_id),
            "actor": actor.strip(),
            "reason": reason.strip(),
            "expected_version": expected_version,
        }
        request_hash = hashlib.sha256(canonical_json_bytes(request_identity)).hexdigest()
        idem_hash = (
            hashlib.sha256(idempotency_key.encode("utf-8")).hexdigest()[:16]
            if idempotency_key
            else request_hash[:16]
        )
        correlation_id = f"reconcile:{invocation_id}:{idem_hash}"
        decision = AnalystDecision(
            id=uuid5(
                _INVOCATION_RECONCILIATION_NAMESPACE,
                f"{correlation_id}:{request_hash}",
            ),
            investigation_id=investigation_id,
            decision_type=(
                AnalystDecisionType.PIVOT_APPROVE
                if outcome is InvocationReconciliationOutcome.EXECUTED_WITH_RESULT
                else AnalystDecisionType.PIVOT_REJECT
            ),
            target_type=AnalystDecisionTargetType.TOOL,
            target_id=invocation.id,
            actor_id=actor.strip(),
            reason=reason.strip(),
            correlation_id=correlation_id,
            details=details,
        )
        prior_decisions = await self._audit_decisions.list_for_investigation(investigation_id)
        prior = next(
            (item for item in prior_decisions if item.correlation_id == correlation_id), None
        )
        if prior is not None:
            if not _same_reconciliation_decision(prior, decision):
                raise ValueError("Idempotency-Key already records another reconciliation")
            expected_status = (
                ToolInvocationStatus.SUCCEEDED
                if outcome is InvocationReconciliationOutcome.EXECUTED_WITH_RESULT
                else ToolInvocationStatus.FAILED
            )
            if invocation.status is not expected_status or invocation.response_refs != refs:
                raise VersionConflictError("Reconciled invocation has since changed")
            return invocation

        if invocation.status is not ToolInvocationStatus.NEEDS_REVIEW:
            raise VersionConflictError("Only a needs_review invocation can be reconciled")
        if expected_version is not None and invocation.version != expected_version:
            raise VersionConflictError("Tool invocation version changed")

        envelope = await self._envelopes.get(investigation_id)
        consumed: dict[BudgetDimension, int] = {}
        if envelope is not None:
            reservation = next(
                (
                    item
                    for item in envelope.budget.reservations
                    if item.operation_key == invocation.operation_key
                ),
                None,
            )
            if reservation is None and invocation.reserved_budget:
                raise ValueError("Ambiguous invocation has no durable budget reservation")
            if reservation is not None:
                if outcome in {
                    InvocationReconciliationOutcome.EXECUTED_WITH_RESULT,
                    InvocationReconciliationOutcome.ABANDON,
                }:
                    envelope.budget.commit(invocation.operation_key)
                    consumed = dict(invocation.reserved_budget)
                else:
                    envelope.budget.release(invocation.operation_key)

        status = (
            ToolInvocationStatus.SUCCEEDED
            if outcome is InvocationReconciliationOutcome.EXECUTED_WITH_RESULT
            else ToolInvocationStatus.FAILED
        )
        resolved = replace(
            invocation,
            status=status,
            response_refs=refs,
            consumed_budget=consumed,
            updated_at=datetime.now(UTC),
            version=invocation.version + 1,
        )
        await self._invocations.update_if_version(resolved, expected_version=invocation.version)
        if envelope is not None:
            await self._save_envelope_version(envelope)
        await self._audit_decisions.append(decision)
        await self._commit_current_uow()
        if resolved.status is ToolInvocationStatus.FAILED:
            _log_terminal_tool_failure(
                resolved,
                ToolResult(
                    status=ToolInvocationStatus.FAILED,
                    exception_class="OperatorReconciliation",
                    error_code=(
                        "operator_declared_not_executed"
                        if outcome is InvocationReconciliationOutcome.NOT_EXECUTED
                        else "operator_abandoned_invocation"
                    ),
                ),
            )
        return resolved

    def _alternative_tools(
        self,
        *,
        investigation_id: UUID,
        envelope: InvestigationEnvelope | None,
        budget: InvestigationBudget,
        exclude_name: str,
    ) -> tuple[str, ...]:
        context = ToolExecutionContext(
            investigation_id=investigation_id,
            envelope=envelope,
            budget=budget,
            idempotency_key="g2-revocation-alternatives",
            available_capabilities=self._available_capabilities,
        )
        alternatives = []
        for definition in self._registry.list_available(context):
            if definition.name == exclude_name:
                continue
            if definition.effect in {ToolEffect.LOCAL_READ, ToolEffect.LOCAL_COMPUTE} or (
                envelope is not None and _tool_allowed_by_envelope(definition, envelope)
            ):
                alternatives.append(definition.name)
        return tuple(sorted(set(alternatives))[:8])

    async def _replay_invocation(
        self, invocation: ToolInvocation
    ) -> tuple[ToolResult, tuple[UUID, ...], StopReason | None]:
        status = invocation.status
        if status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.EMPTY}:
            reference = (
                f" See observation {invocation.response_refs[0]}."
                if invocation.response_refs
                else " No observation reference was recorded."
            )
            return (
                ToolResult(
                    status=status,
                    observation_refs=invocation.response_refs,
                    budget_consumed=invocation.consumed_budget,
                    message="Already executed; reuse the persisted result." + reference,
                ),
                invocation.response_refs,
                None,
            )
        if status is ToolInvocationStatus.UNAVAILABLE:
            reference = (
                f" See observation {invocation.response_refs[0]}."
                if invocation.response_refs
                else " No observation reference was recorded."
            )
            return (
                ToolResult(
                    status=status,
                    observation_refs=invocation.response_refs,
                    budget_consumed=invocation.consumed_budget,
                    message="Already executed and provider unavailable; do not repeat." + reference,
                ),
                invocation.response_refs,
                None,
            )
        if status is ToolInvocationStatus.RUNNING:
            reviewed = invocation.transition(ToolInvocationStatus.NEEDS_REVIEW)
            await self._invocations.update_if_version(reviewed, expected_version=invocation.version)
            await self._commit_current_uow()
            _log_terminal_tool_failure(
                reviewed,
                ToolResult(
                    status=ToolInvocationStatus.NEEDS_REVIEW,
                    exception_class="InvocationReplayRecovery",
                    error_code="running_invocation_requires_reconciliation",
                ),
            )
            return (
                ToolResult(
                    status=ToolInvocationStatus.NEEDS_REVIEW,
                    message="Prior tool attempt is ambiguous.",
                ),
                (),
                StopReason.SUBMISSION_NEEDS_REVIEW,
            )
        if status is ToolInvocationStatus.NEEDS_REVIEW and invocation.response_refs:
            return (
                ToolResult(
                    status=status,
                    observation_refs=invocation.response_refs,
                    message="Prior tool result requires explicit reconciliation before reuse.",
                ),
                invocation.response_refs,
                StopReason.SUBMISSION_NEEDS_REVIEW,
            )
        if status in {ToolInvocationStatus.PENDING, ToolInvocationStatus.NEEDS_REVIEW}:
            return (
                ToolResult(
                    status=ToolInvocationStatus.NEEDS_REVIEW,
                    message="Prior tool attempt needs review.",
                ),
                (),
                StopReason.SUBMISSION_NEEDS_REVIEW,
            )
        return (
            ToolResult(
                status=status,
                observation_refs=invocation.response_refs,
                message="Reused persisted tool outcome.",
            ),
            invocation.response_refs,
            None,
        )

    async def _persist_result_observations(
        self, invocation: ToolInvocation, result: ToolResult
    ) -> tuple[EvidenceObservation, ...]:
        registered = self._registry.get(invocation.tool_name, invocation.schema_version)
        definition = registered[0] if registered is not None else None
        external_effect = definition is not None and definition.effect in {
            ToolEffect.EXTERNAL_READ,
            ToolEffect.ACQUIRE,
            ToolEffect.EXTERNAL_ACTIVE,
            ToolEffect.EXTERNAL_SUBMIT,
        }
        if (
            result.framework_pre_submission_proof is FrameworkPreSubmissionProof.TOOL_UNAVAILABLE
            and not result.output
        ):
            # A registry-level capability miss is fully represented by the terminal
            # UNAVAILABLE invocation and has no tool result payload to archive.
            return ()
        has_framework_proof = external_effect and result.framework_pre_submission_proof is not None
        if (
            result.error_code == "invalid_tool_arguments"
            or result.output.get("error_code") == "invalid_tool_arguments"
        ):
            # Local schema rejection is a correction signal, not evidence about
            # the external subject or the provider's response. Keep it local
            # even when the framework also carries typed pre-submission proof.
            return ()
        provider_auth_refusal = (
            result.status is ToolInvocationStatus.UNAVAILABLE
            and result.output.get("reason") == "provider_authorization_refused"
        )
        provider_download_refusal = result.status is ToolInvocationStatus.UNAVAILABLE and (
            result.output.get("reason")
            in {
                "provider_download_route_not_found",
                "provider_download_authorization_refused",
            }
        )
        if (not result.output and not has_framework_proof) or (
            result.observation_refs
            and invocation.tool_name not in {"vt.download_sample", "sample.analyze"}
            and not provider_auth_refusal
            and not provider_download_refusal
            and not has_framework_proof
        ):
            return ()
        if self._observation_writer is None:
            raise RuntimeError("No observation archive is configured for tool output")
        return await self._observation_writer.archive(invocation, result)

    async def _persist_ambiguous_result_observation(
        self, invocation: ToolInvocation
    ) -> tuple[EvidenceObservation, ...]:
        """Record only framework status when an external result has no valid refs."""
        safe_result = ToolResult(
            status=ToolInvocationStatus.NEEDS_REVIEW,
            output={
                "status": ToolInvocationStatus.NEEDS_REVIEW.value,
                "outcome": "external_effect_unconfirmed",
                "adapter_output_archived": False,
            },
            message="The external result is ambiguous; adapter output was not archived.",
        )
        return await self._persist_result_observations(invocation, safe_result)

    async def _valid_observation_refs(
        self, investigation_id: UUID, refs: tuple[UUID, ...]
    ) -> tuple[UUID, ...]:
        valid = []
        for ref in refs:
            observation = await self._observations.get(ref)
            if observation is not None and observation.investigation_id == investigation_id:
                valid.append(ref)
        return tuple(dict.fromkeys(valid))

    async def _persist_findings(
        self,
        *,
        investigation_id: UUID,
        proposals: Sequence[Any],
        decision_trace: str,
    ) -> tuple[InvestigationFinding, ...]:
        findings, _ = await self._persist_findings_with_count(
            investigation_id=investigation_id,
            proposals=proposals,
            decision_trace=decision_trace,
        )
        return findings

    async def _persist_findings_with_count(
        self,
        *,
        investigation_id: UUID,
        proposals: Sequence[Any],
        decision_trace: str,
    ) -> tuple[tuple[InvestigationFinding, ...], int]:
        snapshot = await self._context_builder.article_snapshots.resolve(
            (await self._require_investigation(investigation_id)).synthesis_artifact_id
        )
        known_claims = {item.id for item in snapshot.article.claims}
        novelty_service = ArticleNoveltyService(
            observations=self._observations,
            investigations=self._investigations,
            article_snapshots=self._context_builder.article_snapshots,
        )
        persisted = []
        deduplicated = 0
        existing_findings = list(await self._findings.list_for_investigation(investigation_id))
        for proposal in proposals:
            # Hypothesis links are transient annotations for the working state,
            # not part of the finding's persistent evidence identity.
            finding_id = uuid5(
                _FINDING_NAMESPACE,
                f"{investigation_id}:{hashlib.sha256(proposal.persistent_identity_bytes()).hexdigest()}",
            )
            existing = await self._findings.get(finding_id)
            if existing is not None:
                persisted.append(existing)
                deduplicated += 1
                continue
            raw_refs = tuple(proposal.observation_refs)
            observation_refs = await self._valid_observation_refs(investigation_id, raw_refs)
            evidence_items = []
            for ref in observation_refs:
                item = await self._observations.get(ref)
                if item is not None:
                    evidence_items.append(item)
            evidence_observations = tuple(evidence_items)
            invalid_observations = len(observation_refs) != len(raw_refs)
            claim_refs = tuple(ref for ref in proposal.claim_refs if ref in known_claims)
            invalid_claims = len(claim_refs) != len(proposal.claim_refs)
            assessment = proposal.assessment
            limitations = list(proposal.limitations)
            subject_ref = _normalize_subject_ref(proposal.subject_ref)
            if (
                assessment is FindingAssessment.INCONCLUSIVE
                and "Backend downgraded supported proposals without observation references"
                in decision_trace
                and proposal.statement[:120] in decision_trace
            ):
                limitations.append(
                    "Backend downgraded supported proposal because it cited no existing "
                    "observation."
                )
            if assessment is FindingAssessment.SUPPORTED and (
                not observation_refs or invalid_observations or invalid_claims
            ):
                assessment = FindingAssessment.INCONCLUSIVE
                limitations.append(
                    "Backend downgraded supported finding because a cited observation or article "
                    "claim does not exist."
                )
            elif invalid_observations or invalid_claims:
                limitations.append(
                    "Backend removed nonexistent observation or article-claim citations."
                )
            if assessment is FindingAssessment.SUPPORTED:
                assessment, validated_limits = constrain_supported_assessment(
                    kind=proposal.kind,
                    assessment=assessment,
                    subject_ref=subject_ref,
                    claim_refs=claim_refs,
                    observations=evidence_observations,
                    limitations=tuple(limitations),
                )
                limitations = list(validated_limits)

            novelty = proposal.novelty
            if proposal.kind is FindingKind.NEW_SAMPLE and not _is_sample_hash(subject_ref):
                novelty = NoveltyClass.NOT_CONFIRMED
                assessment = FindingAssessment.INCONCLUSIVE
                limitations.append(
                    "Un finding NEW_SAMPLE sans subject_ref hash valide ne permet pas d'identifier "
                    "l'objet visé."
                )
            elif proposal.kind is FindingKind.NEW_SAMPLE or (
                proposal.novelty is NoveltyClass.NEW_TO_ARTICLE
            ):
                candidate = _novelty_candidate(
                    investigation_id=investigation_id,
                    kind=proposal.kind,
                    statement=proposal.statement,
                    subject_ref=subject_ref,
                    claim_refs=claim_refs,
                    assessment=assessment,
                    observation_refs=observation_refs,
                )
                if candidate is None:
                    novelty = NoveltyClass.NOT_CONFIRMED
                    limitations.append(
                        "La nouveauté Bridge a été déclassée : aucun objet ou claim valide "
                        "ne permet "
                        "la comparaison au snapshot."
                    )
                else:
                    classification = await novelty_service.classify(
                        investigation_id=investigation_id,
                        candidate=candidate,
                    )
                    novelty = classification.novelty
                    limitations.extend(classification.limitations)
                    if proposal.novelty is not novelty:
                        limitations.append(
                            "Nouveauté recalculée côté serveur depuis le snapshot de l'article : "
                            f"{classification.reason.summary}"
                        )
                    if (
                        proposal.novelty is NoveltyClass.NEW_TO_ARTICLE
                        and classification.reason.matched_article_identifiers
                    ):
                        limitations.append(
                            "La nouveauté proposée par Bridge a été déclassée car l'objet figure "
                            "déjà dans le snapshot : "
                            + ", ".join(classification.reason.matched_article_identifiers[:8])
                        )
            if novelty is NoveltyClass.NEW_TO_ARTICLE and not any(
                "absence mondiale" in item.casefold() for item in limitations
            ):
                limitations.append(
                    "Non documenté dans cet article ni ce corpus : ne démontre pas "
                    "l'absence mondiale de l'échantillon."
                )
            finding = InvestigationFinding(
                investigation_id=investigation_id,
                kind=proposal.kind,
                assessment=assessment,
                statement=proposal.statement,
                observation_refs=observation_refs,
                claim_refs=claim_refs,
                novelty=novelty,
                scope=proposal.scope,
                limitations=tuple(limitations),
                id=finding_id,
            )
            semantic_duplicate = next(
                (
                    item
                    for item in existing_findings
                    if _semantic_finding_identity(item) == _semantic_finding_identity(finding)
                ),
                None,
            )
            if semantic_duplicate is not None:
                persisted.append(semantic_duplicate)
                deduplicated += 1
                continue
            saved = await self._findings.append_version(finding, expected_previous_id=None)
            persisted.append(saved)
            existing_findings.append(saved)
        return tuple(persisted), deduplicated

    async def _apply_opinion_delta(
        self,
        *,
        investigation_id: UUID,
        state: InvestigationWorkingState,
        delta: Any | None,
        valid_anchors: frozenset[str] = frozenset(),
    ) -> tuple[bool, tuple[str, ...]]:
        if (
            delta is None
            or state.base_publication_artifact_id is None
            or state.base_publication_sha256 is None
        ):
            return False, ()
        opinion_id = investigation_opinion_id(investigation_id)
        current = await self._opinions.get(opinion_id)
        findings = tuple(await self._findings.list_for_investigation(investigation_id))
        observations = tuple(await self._observations.list_for_investigation(investigation_id))
        finding_by_id = {item.id: item for item in findings}
        observation_ids = {item.id for item in observations}
        blocks_by_type = {item.block_type: item for item in current.blocks} if current else {}
        unresolved = list(current.unresolved_points if current else ())
        rejections: list[str] = []
        notes_by_anchor = {item.anchor: item for item in (current.observations if current else ())}
        for proposed in delta.blocks:
            valid_findings = tuple(ref for ref in proposed.finding_refs if ref in finding_by_id)
            valid_observations = tuple(
                ref for ref in proposed.evidence_refs if ref in observation_ids
            )
            invalid_refs = len(valid_findings) != len(proposed.finding_refs) or len(
                valid_observations
            ) != len(proposed.evidence_refs)
            block = OpinionBlock(
                block_type=proposed.block_type,
                text=proposed.text,
                finding_refs=valid_findings,
                evidence_refs=valid_observations,
                qualification=proposed.qualification,
                certainty=proposed.certainty,
            )
            trial = AnalystOpinionV1(
                id=opinion_id,
                version=(current.version + 1) if current else 1,
                investigation_id=investigation_id,
                synthesis_artifact_id=(
                    await self._require_investigation(investigation_id)
                ).synthesis_artifact_id,
                synthesis_sha256=(
                    await self._context_builder.article_snapshots.resolve(
                        (await self._require_investigation(investigation_id)).synthesis_artifact_id
                    )
                ).synthesis_sha256,
                base_publication_artifact_id=state.base_publication_artifact_id,
                base_publication_sha256=state.base_publication_sha256,
                blocks=tuple((*blocks_by_type.values(), block))
                if block.block_type not in blocks_by_type
                else tuple(
                    block if old.block_type is block.block_type else old
                    for old in blocks_by_type.values()
                ),
                author_id=self._author_id,
                status=AnalystOpinionStatus.DRAFT,
                unresolved_points=tuple(unresolved),
            )
            violations = verify_opinion_citations(trial, findings, observations)
            block_violations = tuple(
                item for item in violations if item.block_type is block.block_type
            )
            if invalid_refs or block_violations:
                unresolved.append(
                    f"Opinion block {block.block_type.value} withheld: citation validation failed."
                )
                rejections.append(
                    "opinion_validation_rejected: cite existing finding and evidence IDs that "
                    "satisfy the requested qualification."
                )
                continue
            blocks_by_type[block.block_type] = block
        for proposed in getattr(delta, "notes", ()):
            if proposed.anchor not in valid_anchors:
                rejections.append(
                    "note_validation_rejected: use an anchor listed in article.publication_anchors."
                )
                continue
            valid_findings = tuple(ref for ref in proposed.finding_refs if ref in finding_by_id)
            valid_evidence = tuple(ref for ref in proposed.evidence_refs if ref in observation_ids)
            if len(valid_findings) != len(proposed.finding_refs) or len(valid_evidence) != len(
                proposed.evidence_refs
            ):
                rejections.append(
                    "note_validation_rejected: cite only existing finding and observation IDs."
                )
                continue
            existing_note = notes_by_anchor.get(proposed.anchor)
            if existing_note is not None and _normalize_opinion_note_text(
                existing_note.text
            ) == _normalize_opinion_note_text(proposed.text):
                continue
            if existing_note is None and len(notes_by_anchor) >= 24:
                rejections.append(
                    "note_validation_rejected: the opinion already has the 24-note limit."
                )
                continue
            note_id = (
                existing_note.id
                if existing_note is not None
                else uuid5(
                    _OPINION_NAMESPACE,
                    f"{investigation_id}:note:{proposed.anchor}",
                )
            )
            note = AnalystObservationV1(
                id=note_id,
                anchor=proposed.anchor,
                text=proposed.text,
                state=proposed.state,
                relation=proposed.relation,
                finding_refs=valid_findings,
                evidence_refs=valid_evidence,
            )
            trial_notes_by_anchor = {**notes_by_anchor, proposed.anchor: note}
            trial_blocks = opinion_blocks_with_observations(
                tuple(
                    item
                    for item in blocks_by_type.values()
                    if item.block_type.value != "analyst_observations"
                ),
                tuple(trial_notes_by_anchor.values()),
            )
            trial = AnalystOpinionV1(
                id=opinion_id,
                version=(current.version + 1) if current else 1,
                investigation_id=investigation_id,
                synthesis_artifact_id=(
                    await self._require_investigation(investigation_id)
                ).synthesis_artifact_id,
                synthesis_sha256=(
                    await self._context_builder.article_snapshots.resolve(
                        (await self._require_investigation(investigation_id)).synthesis_artifact_id
                    )
                ).synthesis_sha256,
                base_publication_artifact_id=state.base_publication_artifact_id,
                base_publication_sha256=state.base_publication_sha256,
                blocks=trial_blocks,
                author_id=self._author_id,
                status=AnalystOpinionStatus.DRAFT,
                unresolved_points=tuple(unresolved),
            )
            note_violations = tuple(
                violation
                for violation in verify_opinion_citations(trial, findings, observations)
                if violation.reference_id == note.id
                or violation.reference_id in {*note.finding_refs, *note.evidence_refs}
            )
            if note_violations:
                rejections.append(
                    "note_validation_rejected: the cited evidence does not support this note state."
                )
                continue
            notes_by_anchor[proposed.anchor] = note
        unresolved.extend(delta.unresolved_points)
        blocks = opinion_blocks_with_observations(
            tuple(
                item
                for item in blocks_by_type.values()
                if item.block_type.value != "analyst_observations"
            ),
            tuple(notes_by_anchor.values()),
        )
        if (
            current is not None
            and blocks == current.blocks
            and tuple(unresolved) == current.unresolved_points
        ):
            return False, tuple(rejections)
        investigation = await self._require_investigation(investigation_id)
        snapshot = await self._context_builder.article_snapshots.resolve(
            investigation.synthesis_artifact_id
        )
        expected_version = current.version if current else 0
        opinion = AnalystOpinionV1(
            id=opinion_id,
            version=expected_version + 1,
            investigation_id=investigation_id,
            synthesis_artifact_id=investigation.synthesis_artifact_id,
            synthesis_sha256=snapshot.synthesis_sha256,
            base_publication_artifact_id=state.base_publication_artifact_id,
            base_publication_sha256=state.base_publication_sha256,
            blocks=blocks,
            author_id=self._author_id,
            status=AnalystOpinionStatus.DRAFT,
            unresolved_points=tuple(dict.fromkeys(unresolved)),
        )
        if verify_opinion_citations(opinion, findings, observations):
            rejections.append(
                "opinion_validation_rejected: correct citations to existing supported findings "
                "and observations."
            )
            return False, tuple(rejections)
        await self._opinions.append_version(opinion, expected_version=expected_version)
        return True, tuple(rejections)

    @staticmethod
    def _update_hypotheses(
        hypotheses: tuple[Hypothesis, ...],
        *,
        decision: AgentTurnDecisionV1,
        findings: tuple[InvestigationFinding, ...],
    ) -> tuple[Hypothesis, ...]:
        if not hypotheses:
            return hypotheses
        # Proposal links are sufficient to target their hypotheses. Legacy
        # decisions without either kind of link keep their prior no-op behavior.
        target_ids = set(decision.hypothesis_refs)
        target_ids.update(
            reference
            for proposal in decision.finding_proposals
            for reference in proposal.hypothesis_refs
        )
        if not target_ids:
            return hypotheses
        # A turn may propose unrelated findings. Never attribute the last
        # finding to every hypothesis mentioned in the decision.
        linked: dict[str, list[InvestigationFinding]] = {}
        for proposal, finding in zip(decision.finding_proposals, findings, strict=True):
            if (
                finding.assessment not in {FindingAssessment.SUPPORTED, FindingAssessment.REFUTED}
                or not finding.observation_refs
            ):
                continue
            proposal_hypothesis_ids = tuple(dict.fromkeys(proposal.hypothesis_refs))
            if len(proposal_hypothesis_ids) == 1:
                linked.setdefault(proposal_hypothesis_ids[0], []).append(finding)

        updates = []
        for hypothesis in hypotheses:
            if hypothesis.id not in target_ids:
                updates.append(hypothesis)
                continue
            relevant = tuple(linked.get(hypothesis.id, ()))
            assessments = {item.assessment for item in relevant}
            status = hypothesis.status
            if len(assessments) > 1:
                # Contradictory observations do not justify a categorical verdict.
                status = HypothesisStatus.OPEN
            elif FindingAssessment.SUPPORTED in assessments:
                status = HypothesisStatus.SUPPORTED
            elif FindingAssessment.REFUTED in assessments:
                status = HypothesisStatus.REFUTED
            elif decision.next_action is AgentNextAction.SWITCH_HYPOTHESIS:
                if not decision.finding_proposals:
                    status = HypothesisStatus.ABANDONED
            updates.append(
                Hypothesis(
                    id=hypothesis.id,
                    statement=hypothesis.statement,
                    status=status,
                    evidence_refs=tuple(
                        dict.fromkeys(
                            (
                                *hypothesis.evidence_refs,
                                *(ref for item in relevant for ref in item.observation_refs),
                            )
                        )
                    ),
                    finding_refs=tuple(
                        dict.fromkeys((*hypothesis.finding_refs, *(item.id for item in relevant)))
                    ),
                )
            )
        return tuple(updates)

    async def _bridge_stop(self, turn: BridgeTurnResult) -> StopReason | None:
        if turn.status is TurnSubmissionStatus.SUCCEEDED:
            return None
        if turn.status is TurnSubmissionStatus.NEEDS_REVIEW:
            return StopReason.SUBMISSION_NEEDS_REVIEW
        if turn.status is TurnSubmissionStatus.INVALID_OUTPUT:
            return StopReason.SUBMISSION_NEEDS_REVIEW
        if turn.status is TurnSubmissionStatus.PRE_SUBMISSION_RETRY_EXHAUSTED:
            return StopReason.SUBMISSION_NEEDS_REVIEW
        return StopReason.MODEL_UNAVAILABLE

    async def _record_bridge_rejection(
        self, investigation_id: UUID, idempotency_key: str, detail: str
    ) -> None:
        current = await self._require_state(investigation_id)
        action = f"{_INVALID_BRIDGE_ACTION_PREFIX}{idempotency_key[:64]}:{detail.strip()[:256]}"
        if action in current.recent_actions:
            return
        updated = replace(
            current,
            version=current.version + 1,
            recent_actions=(*current.recent_actions, action)[-32:],
        )
        await self._save_state(current, updated)
        await self._commit_current_uow()

    @staticmethod
    def _external_allowed(
        snapshot: ArticleSnapshot, envelope: InvestigationEnvelope | None
    ) -> bool:
        if not InvestigationService._external_tlp_allowed(snapshot, envelope):
            return False
        assert envelope is not None
        return bool(snapshot.external_llm_allowed and envelope.disclosure.external_llm_allowed)

    @staticmethod
    def _external_tlp_allowed(
        snapshot: ArticleSnapshot, envelope: InvestigationEnvelope | None
    ) -> bool:
        # Both Bridge and external provider operations need a materialized G2
        # envelope with a source TLP no higher than its approved TLP bound.
        if envelope is None or not isinstance(snapshot.tlp, str):
            return False
        raw_tlp = snapshot.tlp.strip().casefold()
        if raw_tlp.startswith("tlp:"):
            raw_tlp = raw_tlp[4:].strip()
        try:
            source_tlp = TLPLevel(raw_tlp)
        except ValueError:
            return False
        envelope_tlp = envelope.disclosure.tlp
        if envelope_tlp is not None and not isinstance(envelope_tlp, TLPLevel):
            return False
        tlp_rank = {
            TLPLevel.WHITE: 0,
            TLPLevel.CLEAR: 1,
            TLPLevel.GREEN: 2,
            TLPLevel.AMBER: 3,
            TLPLevel.RED: 4,
        }
        if envelope_tlp is not None and tlp_rank[envelope_tlp] < tlp_rank[source_tlp]:
            return False
        return source_tlp is not TLPLevel.RED and envelope_tlp is not TLPLevel.RED

    @staticmethod
    def _external_policy_block_action(
        snapshot: ArticleSnapshot,
        envelope: InvestigationEnvelope | None,
        turn_index: int,
    ) -> str:
        tlp = (snapshot.tlp or "").strip().lower().replace("tlp:", "")
        details = [
            f"turn={turn_index}",
            f"snapshot_external_allowed={int(snapshot.external_llm_allowed)}",
            f"snapshot_tlp_red={int(tlp == 'red')}",
        ]
        if envelope is None:
            details.append("envelope=absent")
        else:
            envelope_tlp_red = (
                envelope.disclosure.tlp is not None and envelope.disclosure.tlp.value == "red"
            )
            details.extend(
                (
                    f"envelope_external_allowed={int(envelope.disclosure.external_llm_allowed)}",
                    f"envelope_tlp_red={int(envelope_tlp_red)}",
                )
            )
        return "bridge_submission_blocked:external_llm_policy:" + ":".join(details)

    @staticmethod
    def _unapproved_budget(investigation: AnalystInvestigation) -> InvestigationBudget:
        return InvestigationBudget(
            limits={
                BudgetDimension.VT_UNITS: 0,
                BudgetDimension.SHODAN_CREDITS: 0,
                BudgetDimension.DOWNLOADS: 0,
            }
        )

    async def _save_envelope_version(
        self, envelope: InvestigationEnvelope
    ) -> InvestigationEnvelope:
        updated = replace(envelope, version=envelope.version + 1)
        saved = await self._envelopes.save_if_version(updated, expected_version=envelope.version)
        await self._save_budget_snapshot(saved)
        return saved

    async def _save_budget_snapshot(self, envelope: InvestigationEnvelope) -> None:
        """Mirror the envelope's canonical budget into normalized reservation rows."""
        repository = self._budget_snapshots
        if repository is None:
            return
        try:
            # The envelope CAS is canonical; retry snapshot CAS conflicts so a
            # concurrent mirror update cannot strand a released reservation.
            for _ in range(3):
                current = await repository.get(envelope.investigation_id)
                if current is not None and current.budget == envelope.budget:
                    return
                expected_version = current.version if current is not None else None
                snapshot = InvestigationBudgetSnapshot(
                    investigation_id=envelope.investigation_id,
                    version=current.version + 1 if current is not None else 1,
                    budget=envelope.budget,
                )
                try:
                    await repository.save_if_version(
                        snapshot,
                        expected_version=expected_version,
                    )
                    return
                except VersionConflictError:
                    continue
            raise VersionConflictError("Investigation budget snapshot changed repeatedly")
        except asyncio.CancelledError:
            await self._rollback_budget_snapshot_pair()
            raise
        except Exception as exc:
            await self._rollback_budget_snapshot_pair()
            raise _BudgetSnapshotPersistenceError("Envelope budget mirror failed.") from exc

    async def _rollback_budget_snapshot_pair(self) -> None:
        if self._rollback_uow is not None:
            await asyncio.shield(self._rollback_uow())

    async def _commit_current_uow(self) -> None:
        if self._commit_uow is not None:
            await self._commit_uow()

    async def _requested_stop(self, investigation_id: UUID) -> InvestigationWorkingState | None:
        """Reload the durable stop decision at every externally visible boundary."""
        state = await self._require_state(investigation_id)
        return state if state.stop_reason is not None else None

    async def _record_policy_reduction(
        self,
        *,
        investigation_id: UUID,
        attempted_operation: str,
        approved_envelope: InvestigationEnvelope | None,
        current_envelope: InvestigationEnvelope | None,
        budget: InvestigationBudget,
    ) -> None:
        state = await self._require_state(investigation_id)
        if state.stop_reason is not None:
            return
        alternatives = self._alternative_tools(
            investigation_id=investigation_id,
            envelope=current_envelope,
            budget=current_envelope.budget if current_envelope is not None else budget,
            exclude_name=attempted_operation,
        )
        detail = (
            f"g2_reduced_after:{attempted_operation}: "
            f"approved_version={approved_envelope.version if approved_envelope else 0}: "
            f"current_version={current_envelope.version if current_envelope else 0}"
        )
        if alternatives:
            detail += "; alternatives: " + ", ".join(alternatives)
        updated = replace(
            state,
            version=state.version + 1,
            recent_actions=(*state.recent_actions, detail)[-32:],
        )
        await self._save_state(state, updated)
        await self._commit_current_uow()

    async def _record_g2_blocked_calls(
        self,
        *,
        investigation_id: UUID,
        proposed_calls: Sequence[Any],
        current_envelope: InvestigationEnvelope | None,
    ) -> None:
        version = current_envelope.version if current_envelope is not None else "revoked"
        for proposed in proposed_calls:
            adapter = self._registry.get(proposed.tool_name, proposed.schema_version)
            definition = adapter[0] if adapter is not None else None
            if definition is None or definition.effect not in {
                ToolEffect.EXTERNAL_READ,
                ToolEffect.ACQUIRE,
                ToolEffect.EXTERNAL_ACTIVE,
                ToolEffect.EXTERNAL_SUBMIT,
            }:
                continue
            base_key = tool_operation_key(
                investigation_id=investigation_id,
                tool_name=proposed.tool_name,
                schema_version=proposed.schema_version,
                normalized_parameters=proposed.arguments,
                purpose=proposed.purpose,
            )
            denied_key = f"{base_key}:g2-denied:{version}"
            invocation = await self._invocations.get_by_operation_key(denied_key)
            if invocation is None:
                invocation = await self._invocations.add_if_absent(
                    ToolInvocation(
                        investigation_id=investigation_id,
                        operation_key=denied_key,
                        tool_name=proposed.tool_name,
                        schema_version=proposed.schema_version,
                        normalized_parameters=proposed.arguments,
                        purpose=proposed.purpose,
                    )
                )
            if invocation.status is ToolInvocationStatus.PENDING:
                denied = invocation.transition(ToolInvocationStatus.POLICY_DENIED)
                await self._invocations.update_if_version(
                    denied, expected_version=invocation.version
                )
        await self._commit_current_uow()

    async def _bridge_submission_allowed(
        self,
        investigation_id: UUID,
        approved_envelope: InvestigationEnvelope | None,
    ) -> bool:
        if await self._requested_stop(investigation_id) is not None:
            return False
        current_envelope = await self._envelopes.get(investigation_id)
        if _envelope_reduced_since(approved_envelope, current_envelope, family="bridge.turn"):
            return False
        if (
            approved_envelope is None
            or approved_envelope.authority_kind is not EnvelopeAuthorityKind.OSINT_POLICY
        ):
            return True
        resolver = self._effective_policy_for
        if resolver is None:
            return False
        try:
            effective_policy = await resolver(investigation_id)
            if effective_policy is None or not effective_policy.authorized:
                return False
            investigation = await self._investigations.get(investigation_id)
            return bool(
                investigation is not None
                and effective_policy.inheritance.subject_id == investigation.subject_id
            )
        except Exception:
            # Missing policy state, lineage mismatch, or resolver failure must
            # refuse before the Bridge transport emits a request.
            return False

    async def _complete_bridge_reservation(
        self,
        investigation_id: UUID,
        operation_key: str,
        budget: InvestigationBudget,
        *,
        blocked: bool,
        confirmed_non_submission: bool = False,
    ) -> None:
        if await self._envelopes.get(investigation_id) is None:
            if confirmed_non_submission:
                budget.release_confirmed_non_submission(operation_key)
            elif blocked:
                budget.release(operation_key)
            else:
                budget.commit(operation_key)
            return
        for _ in range(3):
            envelope = await self._envelopes.get(investigation_id)
            if envelope is None:
                return
            reservation = next(
                (
                    item
                    for item in envelope.budget.reservations
                    if item.operation_key == operation_key
                ),
                None,
            )
            if confirmed_non_submission and reservation is not None:
                envelope.budget.release_confirmed_non_submission(operation_key)
            elif reservation is None or reservation.status is BudgetReservationStatus.RESERVED:
                if blocked:
                    envelope.budget.release(operation_key)
                elif reservation is not None:
                    envelope.budget.commit(operation_key)
                if reservation is None and blocked:
                    return
                if reservation is None:
                    return
                try:
                    await self._save_envelope_version(envelope)
                    return
                except VersionConflictError:
                    continue
            return
        raise VersionConflictError("Bridge budget reservation changed repeatedly")

    async def _complete_tool_reservation(
        self,
        *,
        investigation_id: UUID,
        operation_key: str,
        result: ToolResult,
        reserved_amounts: Mapping[BudgetDimension, int],
        uncertain: bool,
        external_effect: bool = False,
        retryable_external_read: bool = False,
    ) -> None:
        for _ in range(3):
            current_envelope = await self._envelopes.get(investigation_id)
            if current_envelope is None:
                return
            # In-memory repositories may return the stored envelope object
            # itself. Release a private budget copy so a failed CAS cannot
            # mutate canonical state before it is durably saved.
            envelope = replace(
                current_envelope,
                budget=_copy_investigation_budget(current_envelope.budget),
            )
            reservation = next(
                (
                    item
                    for item in envelope.budget.reservations
                    if item.operation_key == operation_key
                ),
                None,
            )
            expected_reservation = BudgetReservation(
                operation_key=operation_key,
                amounts=reserved_amounts,
            )
            if reservation is None or (
                reservation.status is BudgetReservationStatus.RESERVED
                and reservation.amounts != expected_reservation.amounts
            ):
                # G2 revocation zeroes external limits while an already-dispatched
                # operation is in flight. Restore its persisted reservation before
                # recording the response; this does not grant permission for another
                # operation because the envelope's limits and provider scope stay revoked.
                envelope.budget.restore_reservation(expected_reservation)
                reservation = expected_reservation
            if reservation.status is not BudgetReservationStatus.RESERVED:
                await self._save_budget_snapshot(envelope)
                return
            if retryable_external_read:
                # Idempotent VT reads can be retried as a new invocation. Close
                # this reservation at the adapter's conservative attempted cost
                # so the unused page and time allowance does not remain locked.
                envelope.budget.commit(operation_key, dict(result.budget_consumed))
            elif uncertain:
                return
            elif (
                external_effect
                and result.status
                in {
                    ToolInvocationStatus.FAILED,
                    ToolInvocationStatus.UNAVAILABLE,
                    ToolInvocationStatus.POLICY_DENIED,
                    ToolInvocationStatus.NEEDS_REVIEW,
                }
                and not _confirmed_external_non_execution(result)
            ):
                return
            if _confirmed_external_non_execution(result):
                envelope.budget.release_confirmed_non_submission(operation_key)
                try:
                    await self._save_envelope_version(envelope)
                    return
                except VersionConflictError:
                    continue
            if result.status in {
                ToolInvocationStatus.SUCCEEDED,
                ToolInvocationStatus.EMPTY,
                ToolInvocationStatus.PARTIAL,
            }:
                consumed = dict(result.budget_consumed or reserved_amounts)
                envelope.budget.commit(operation_key, consumed)
            else:
                envelope.budget.release(operation_key)
            try:
                await self._save_envelope_version(envelope)
                return
            except VersionConflictError:
                continue
        raise VersionConflictError("Tool budget reservation changed repeatedly")

    async def _require_state(self, investigation_id: UUID) -> InvestigationWorkingState:
        state = await self._states.get(investigation_id)
        if state is None:
            raise ValueError(f"Investigation state {investigation_id} does not exist")
        return state

    async def _require_investigation(self, investigation_id: UUID) -> AnalystInvestigation:
        investigation = await self._investigations.get(investigation_id)
        if investigation is None:
            raise ValueError(f"Analyst investigation {investigation_id} does not exist")
        return investigation

    async def _save_state(
        self, current: InvestigationWorkingState, updated: InvestigationWorkingState
    ) -> None:
        base = current
        candidate = updated
        for attempt in range(MAX_STATE_VERSION_CONFLICT_RETRIES):
            try:
                await self._states.save_if_version(candidate, expected_version=base.version)
                return
            except InvestigationStateVersionConflict:
                if attempt + 1 >= MAX_STATE_VERSION_CONFLICT_RETRIES:
                    raise
                latest = await self._require_state(base.investigation_id)
                candidate = _rebase_investigation_state_update(base, candidate, latest)
                base = latest


def _rebase_investigation_state_update(
    base: InvestigationWorkingState,
    candidate: InvestigationWorkingState,
    latest: InvestigationWorkingState,
) -> InvestigationWorkingState:
    """Apply a job's state delta over a concurrent operator-action append.

    Concurrent writes to other investigation state fields are not silently
    overwritten. Operator requests only append to ``recent_actions``; that
    history is merged so neither the request nor the job's own action is lost.
    """
    if base.investigation_id != candidate.investigation_id or (
        base.investigation_id != latest.investigation_id
    ):
        raise InvestigationStateVersionConflict("Investigation state identity changed")

    changed_fields = {
        item.name: getattr(candidate, item.name)
        for item in fields(InvestigationWorkingState)
        if item.name not in {"version", "recent_actions"}
        and getattr(candidate, item.name) != getattr(base, item.name)
    }
    conflicting_fields = tuple(
        name
        for name, value in changed_fields.items()
        if getattr(latest, name) != getattr(base, name) and getattr(latest, name) != value
    )
    if conflicting_fields:
        raise InvestigationStateVersionConflict(
            "Investigation state changed concurrently in fields: "
            + ", ".join(sorted(conflicting_fields))
        )

    recent_actions = _merge_recent_actions(
        base.recent_actions,
        candidate.recent_actions,
        latest.recent_actions,
    )
    return replace(
        latest,
        version=latest.version + 1,
        recent_actions=recent_actions,
        **changed_fields,
    )


def _merge_recent_actions(
    base: tuple[str, ...],
    candidate: tuple[str, ...],
    latest: tuple[str, ...],
) -> tuple[str, ...]:
    removed = set(base).difference(candidate)
    merged = [action for action in latest if action not in removed]
    for action in candidate:
        if action not in base and action not in merged:
            merged.append(action)

    protected = [
        action
        for action in merged
        if action.startswith(USER_REQUEST_ACTION_PREFIX)
        or action.startswith(_OPERATOR_REQUEST_MARKER_PREFIX)
    ]
    protected_set = set(protected)
    history = [action for action in merged if action not in protected_set]
    history_limit = max(0, _RECENT_ACTIONS_LIMIT - len(protected))
    if history_limit:
        history = history[-history_limit:]
    else:
        history = []
    if len(protected) > _RECENT_ACTIONS_LIMIT:
        protected = protected[-_RECENT_ACTIONS_LIMIT:]
    return (*history, *protected)


def _user_request_was_in_context(action: str, request_ids: tuple[UUID, ...]) -> bool:
    request_id = _user_request_id(action)
    return request_id in request_ids if request_id is not None else False


def _user_request_id(action: str) -> UUID | None:
    if not action.startswith(USER_REQUEST_ACTION_PREFIX):
        return None
    try:
        payload = json.loads(action[len(USER_REQUEST_ACTION_PREFIX) :])
        return UUID(str(payload.get("request_id", "")))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return None


def _same_reconciliation_decision(actual: AnalystDecision, expected: AnalystDecision) -> bool:
    return (
        actual.id == expected.id
        and actual.investigation_id == expected.investigation_id
        and actual.decision_type is expected.decision_type
        and actual.target_type is expected.target_type
        and actual.target_id == expected.target_id
        and actual.actor_id == expected.actor_id
        and actual.reason == expected.reason
        and actual.correlation_id == expected.correlation_id
        and actual.details == expected.details
    )


def _operation_key_base(operation_key: str) -> str:
    """Remove attempt/turn suffixes while preserving the hashed operation identity."""
    return operation_key.partition(":")[0]


def _operation_key_matches(existing_key: str, current_key: str) -> bool:
    """Reuse only this exact operation version or one of its recorded retries."""
    return existing_key == current_key or existing_key.startswith(f"{current_key}:retry:")


def _envelope_reduced_since(
    previous: InvestigationEnvelope | None,
    current: InvestigationEnvelope | None,
    *,
    family: str | None = None,
) -> bool:
    if previous is None:
        return False
    if current is None:
        return True
    if not set(previous.allowed_providers).issubset(current.allowed_providers):
        return True
    if previous.acquire_hits_allowed and not current.acquire_hits_allowed:
        return True
    for dimension, previous_limit in previous.budget.limits.items():
        current_limit = current.budget.limits.get(dimension)
        if previous_limit is None and current_limit is not None:
            return True
        if (
            previous_limit is not None
            and current_limit is not None
            and current_limit < previous_limit
        ):
            return True
    if (
        previous.disclosure.do_not_submit is False
        and current.disclosure.do_not_submit
        and not (family or "").startswith(("bridge.", "advisor.", "vt."))
    ):
        return True
    if previous.disclosure.external_llm_allowed and not current.disclosure.external_llm_allowed:
        return True
    rank = {"white": 1, "clear": 1, "green": 2, "amber": 3, "red": 4}
    previous_tlp = previous.disclosure.tlp.value if previous.disclosure.tlp else None
    current_tlp = current.disclosure.tlp.value if current.disclosure.tlp else None
    return bool(
        current_tlp is not None and rank.get(current_tlp, 0) > rank.get(previous_tlp or "white", 0)
    )


def _tool_allowed_by_envelope(definition: Any, envelope: InvestigationEnvelope | None) -> bool:
    if envelope is None or definition.effect is ToolEffect.EXTERNAL_ACTIVE:
        return False
    if definition.effect in {
        ToolEffect.EXTERNAL_READ,
        ToolEffect.ACQUIRE,
        ToolEffect.EXTERNAL_SUBMIT,
    }:
        provider = definition.capability_key.partition(".")[0]
        if (
            provider not in envelope.allowed_providers
            or (envelope.disclosure.tlp is not None and envelope.disclosure.tlp.value == "red")
            or (
                envelope.disclosure.do_not_submit
                and provider not in {"bridge", "advisor", "vt", "virustotal"}
            )
        ):
            return False
        if definition.effect is ToolEffect.ACQUIRE and not envelope.acquire_hits_allowed:
            return False
    return True


def _actual_tool_consumption(
    budget: InvestigationBudget,
    operation_key: str,
    result: ToolResult,
) -> dict[BudgetDimension, int]:
    if result.budget_consumed:
        return dict(result.budget_consumed)
    reservation = next(
        (item for item in budget.reservations if item.operation_key == operation_key), None
    )
    if reservation is not None and reservation.status is BudgetReservationStatus.COMMITTED:
        return dict(reservation.committed_amounts)
    return {}


def _novelty_candidate(
    *,
    investigation_id: UUID,
    kind: FindingKind,
    statement: str,
    subject_ref: str | None,
    claim_refs: tuple[str, ...],
    assessment: FindingAssessment,
    observation_refs: tuple[UUID, ...],
) -> NoveltyCandidate | None:
    if kind is FindingKind.NEW_SAMPLE:
        if not subject_ref or not subject_ref.strip():
            return None
        candidate_kind = NoveltyCandidateKind.SAMPLE
        value = subject_ref.strip()
        object_type = "sample"
        object_id = subject_ref.strip()
    elif kind is FindingKind.CLAIM_VERIFICATION:
        if not claim_refs:
            return None
        candidate_kind = NoveltyCandidateKind.CLAIM
        value = statement
        object_type = "claim"
        object_id = claim_refs[0]
    elif subject_ref and subject_ref.strip():
        candidate_kind = NoveltyCandidateKind.IOC
        value = subject_ref.strip()
        object_type = kind.value
        object_id = subject_ref.strip()
    else:
        candidate_kind = NoveltyCandidateKind.BEHAVIOUR
        value = statement
        object_type = kind.value
        object_id = statement[:256]
    return NoveltyCandidate(
        investigation_id=investigation_id,
        kind=candidate_kind,
        value=value,
        object_type=object_type,
        object_id=object_id,
        details={"statement": statement},
        claim_id=claim_refs[0] if kind is FindingKind.CLAIM_VERIFICATION and claim_refs else None,
        claim_assessment=assessment if kind is FindingKind.CLAIM_VERIFICATION else None,
        observation_refs=observation_refs,
    )


def _normalize_subject_ref(subject_ref: str | None) -> str | None:
    if subject_ref is None:
        return None
    normalized = subject_ref.strip()
    if _is_sample_hash(normalized):
        return normalized.lower()
    return normalized


def _normalize_opinion_note_text(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def _pending_sample_analysis(
    context: InvestigationContextV1,
    invocations: Sequence[ToolInvocation],
) -> tuple[tuple[str, str], ...]:
    completed = {
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.PARTIAL,
        ToolInvocationStatus.EMPTY,
    }
    pending: list[tuple[str, str]] = []
    for sample in context.investigation.sample_deltas:
        if not isinstance(sample, Mapping) or sample.get("analysis_status") == "analyzed":
            continue
        association_id = _safe_uuid_value(sample.get("association_id"))
        sample_sha256 = sample.get("sha256")
        if (
            association_id is None
            or not isinstance(sample_sha256, str)
            or not re.fullmatch(r"[0-9a-f]{64}", sample_sha256)
        ):
            continue
        analyzed = any(
            item.status in completed
            and bool(item.response_refs)
            and (
                (
                    item.tool_name == "sample.analyze"
                    and _invocation_association_id(item) == association_id
                )
                or (
                    item.tool_name.startswith("ghidra.")
                    and _invocation_sample_sha256(item) == sample_sha256
                )
            )
            for item in invocations
        )
        if not analyzed:
            pending.append((association_id, sample_sha256))
    return tuple(sorted(set(pending)))


def _invocation_association_id(invocation: ToolInvocation) -> str | None:
    parameters = invocation.normalized_parameters
    if not isinstance(parameters, Mapping):
        return None
    return _safe_uuid_value(parameters.get("association_id"))


def _invocation_sample_sha256(invocation: ToolInvocation) -> str | None:
    parameters = invocation.normalized_parameters
    if not isinstance(parameters, Mapping):
        return None
    value = parameters.get("sample_sha256") or parameters.get("sha256")
    if isinstance(value, str) and re.fullmatch(r"[0-9a-f]{64}", value):
        return value
    return None


def _latest_applicable_analysis_invocation(
    invocations: Sequence[ToolInvocation],
    *,
    association_id: str,
    sample_sha256: str,
    tool_name: str,
) -> ToolInvocation | None:
    matching = tuple(
        item
        for item in invocations
        if item.tool_name == tool_name
        and (
            _invocation_association_id(item) == association_id
            if tool_name == "sample.analyze"
            else _invocation_sample_sha256(item) == sample_sha256
        )
    )
    return max(matching, key=lambda item: (item.created_at, item.id.hex), default=None)


def _safe_uuid_value(value: Any) -> str | None:
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError):
        return None


def _sample_blocked_reason(reasoning_summary: str) -> str | None:
    match = re.search(
        r"(?im)^\s*BLOCKED_REASON:\s*(engine_unavailable|budget_exhausted)\s*$",
        reasoning_summary,
    )
    return match.group(1).casefold() if match else None


def _sample_finish_refusal_count(actions: Sequence[str], fingerprint: str) -> int:
    prefix = f"finish_gate_refused:{fingerprint}:"
    for action in reversed(actions):
        if not action.startswith("finish_gate_refused:"):
            continue
        if not action.startswith(prefix):
            return 0
        try:
            return min(MAX_SAMPLE_ANALYSIS_FINISH_REFUSALS, max(0, int(action[len(prefix) :])))
        except ValueError:
            return 0
    return 0


def _rule_test_finish_status(
    context: InvestigationContextV1,
    invocations: Sequence[ToolInvocation],
) -> tuple[tuple[tuple[str, int], ...], tuple[tuple[str, int], ...]]:
    candidates: set[tuple[str, int]] = set()
    for item in context.investigation.rule_deltas:
        if not isinstance(item, Mapping) or item.get("kind") != "rule_candidate":
            continue
        if item.get("validation_status") != "untested":
            continue
        version = item.get("version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 1:
            continue
        try:
            candidate_id = str(UUID(str(item.get("candidate_id"))))
        except (TypeError, ValueError):
            continue
        candidates.add((candidate_id, version))

    if not candidates:
        return (), ()
    rule_test_available = any(item.name == "rule.test" for item in context.available_tools)
    pending: list[tuple[str, int]] = []
    unavailable: list[tuple[str, int]] = []
    completed_statuses = {
        ToolInvocationStatus.SUCCEEDED,
        ToolInvocationStatus.PARTIAL,
        ToolInvocationStatus.EMPTY,
    }
    for candidate_id, version in sorted(candidates):
        matching = tuple(
            invocation
            for invocation in invocations
            if invocation.tool_name == "rule.test"
            and invocation.normalized_parameters.get("candidate_id") == candidate_id
            and invocation.normalized_parameters.get("version") == version
        )
        if any(item.status in completed_statuses for item in matching):
            continue
        if not rule_test_available or any(
            item.status is ToolInvocationStatus.UNAVAILABLE for item in matching
        ):
            unavailable.append((candidate_id, version))
            continue
        pending.append((candidate_id, version))
    return tuple(pending), tuple(unavailable)


def _semantic_finding_identity(
    finding: InvestigationFinding,
) -> tuple[FindingKind, FindingAssessment, str, frozenset[str], frozenset[UUID]]:
    statement = " ".join(unicodedata.normalize("NFC", finding.statement).split())
    return (
        finding.kind,
        finding.assessment,
        statement,
        frozenset(finding.claim_refs),
        frozenset(finding.observation_refs),
    )


def _is_sample_hash(value: str | None) -> bool:
    if value is None or len(value) not in {32, 40, 64}:
        return False
    return all(character in "0123456789abcdefABCDEF" for character in value)


def _agenda_from_snapshot(snapshot: ArticleSnapshot) -> InvestigationAgenda:
    candidates = list(snapshot.article.open_questions[:6])
    candidates.extend(claim.statement for claim in snapshot.article.claims if len(candidates) < 6)
    if len(candidates) < 2:
        candidates.extend(
            (
                "Quels éléments de la chaîne d'infection sont confirmés par les preuves citées ?",
                "Quelles affirmations importantes restent à vérifier ou à nuancer ?",
            )
        )
    unique = list(dict.fromkeys(item.strip() for item in candidates if item.strip()))[:6]
    while len(unique) < 2:
        unique.append(f"Question d'investigation complémentaire {len(unique) + 1}")
    items = tuple(
        AgendaItem(question=question[:2_000], potential=1.0 - index * 0.05, rank=index + 1)
        for index, question in enumerate(unique)
    )
    return InvestigationAgenda(items=items)


def _reservation_status(
    budget: InvestigationBudget, operation_key: str
) -> BudgetReservationStatus | None:
    return next(
        (item.status for item in budget.reservations if item.operation_key == operation_key),
        None,
    )


def bridge_budget_operation_key(
    *, investigation_id: UUID, turn_index: int, context_sha256: str, attempt: int = 0
) -> str:
    key = f"bridge:{investigation_id}:{turn_index}:{context_sha256}"
    return f"{key}:attempt={attempt}" if attempt else key


def _vt_source_refs_from_parameters(
    parameters: Mapping[str, Any],
) -> tuple[EvidenceReference, ...]:
    raw = parameters.get(_VT_SOURCE_REFS_PARAMETER)
    if not isinstance(raw, (list, tuple)) or len(raw) > 16:
        return ()

    references: list[EvidenceReference] = []
    for item in raw:
        if not isinstance(item, Mapping):
            return ()
        try:
            references.append(
                EvidenceReference(
                    kind=str(item["kind"]),
                    id=str(item["id"]),
                    version=str(item["version"]),
                    sha256=str(item["sha256"]),
                )
            )
        except (KeyError, TypeError, ValueError):
            return ()
    return tuple(references)


def _tool_call_arguments_match(
    normalized_parameters: Mapping[str, Any], arguments: Mapping[str, Any]
) -> bool:
    stored_arguments = thaw_json(normalized_parameters)
    if not isinstance(stored_arguments, dict):
        return False
    call_arguments = thaw_json(arguments)
    if not isinstance(call_arguments, dict):
        return False
    stored_arguments.pop(_VT_SOURCE_REFS_PARAMETER, None)
    return stored_arguments == call_arguments


def _bridge_context_observation_ids(context: InvestigationContextV1) -> set[UUID]:
    """Collect only typed observation IDs serialized into a Bridge context."""
    payload = context.model_dump(mode="python", by_alias=True)
    observation_ids: set[UUID] = set()
    scalar_keys = {"evidence_id", "observation_id"}
    collection_keys = {"evidence_refs", "observation_refs", "observation_ids"}

    def add(value: object) -> None:
        if isinstance(value, UUID):
            observation_ids.add(value)
            return
        if isinstance(value, str):
            try:
                observation_ids.add(UUID(value))
            except ValueError as exc:
                raise ExternalEffectValidationError(
                    "Bridge context observation reference is invalid"
                ) from exc
            return
        raise ExternalEffectValidationError("Bridge context observation reference is invalid")

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key in scalar_keys or key.endswith("_observation_id"):
                    add(child)
                elif key in collection_keys or key.endswith("_observation_ids"):
                    if not isinstance(child, Sequence) or isinstance(child, str | bytes):
                        raise ExternalEffectValidationError(
                            "Bridge context observation references are invalid"
                        )
                    for item in child:
                        add(item)
                else:
                    visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for child in value:
                visit(child)

    visit(payload)
    return observation_ids


def _bridge_context_source_document_ids(context: InvestigationContextV1) -> set[UUID]:
    """Collect source document IDs present in typed provenance fields."""
    payload = context.model_dump(mode="python", by_alias=True)
    document_ids: set[UUID] = set()

    def add(value: object, *, reference_text: bool = False) -> None:
        if reference_text and isinstance(value, str):
            value = value.split(":", 1)[0]
        if isinstance(value, UUID):
            document_ids.add(value)
        elif isinstance(value, str):
            try:
                document_ids.add(UUID(value))
            except ValueError:
                if not reference_text:
                    raise ExternalEffectValidationError(
                        "Bridge context source document reference is invalid"
                    ) from None

    def visit(value: object) -> None:
        if isinstance(value, Mapping):
            for key, child in value.items():
                if key == "source_document_ids":
                    if not isinstance(child, Sequence) or isinstance(child, str | bytes):
                        raise ExternalEffectValidationError(
                            "Bridge context source document references are invalid"
                        )
                    for item in child:
                        add(item)
                elif key == "source_refs":
                    if not isinstance(child, Sequence) or isinstance(child, str | bytes):
                        raise ExternalEffectValidationError(
                            "Bridge context source references are invalid"
                        )
                    for item in child:
                        add(item, reference_text=True)
                else:
                    visit(child)
        elif isinstance(value, Sequence) and not isinstance(value, str | bytes):
            for child in value:
                visit(child)

    visit(payload)
    return document_ids


def _copy_investigation_budget(budget: InvestigationBudget) -> InvestigationBudget:
    copied = InvestigationBudget(limits=dict(budget.limits))
    for reservation in budget.reservations:
        copied.restore_reservation(reservation)
    return copied


def _confirmed_external_non_execution(result: ToolResult) -> bool:
    """Accept only framework-attested, typed proof; adapter output is untrusted."""
    return result.framework_pre_submission_proof is not None


def _action_summary(tool_name: str, status: ToolInvocationStatus, message: str) -> str:
    safe_tool_name = sanitize_evidence_text(tool_name, max_chars=128)
    safe_message = sanitize_evidence_text(
        " ".join(message.replace("\x00", "").split()), max_chars=300
    )
    return f"{safe_tool_name}: {status.value}{': ' + safe_message if safe_message else ''}"


def _log_terminal_tool_failure(
    invocation: ToolInvocation,
    result: ToolResult,
    *,
    exception_class: str | None = None,
    error_code: str | None = None,
) -> None:
    if invocation.status not in {
        ToolInvocationStatus.NEEDS_REVIEW,
        ToolInvocationStatus.FAILED,
    }:
        return
    raw_code = (
        error_code
        or result.error_code
        or result.output.get("error_code")
        or (
            "tool_execution_failed"
            if invocation.status is ToolInvocationStatus.FAILED
            else "tool_outcome_needs_review"
        )
    )
    raw_exception = exception_class or result.exception_class or "not_reported"
    safe_code = _SAFE_LOG_TOKEN.sub("_", str(raw_code))[:96] or "unspecified"
    safe_exception = _SAFE_LOG_TOKEN.sub("_", str(raw_exception))[:96] or "unknown"
    safe_tool = _SAFE_LOG_TOKEN.sub("_", invocation.tool_name)[:128] or "unknown"
    # Tool messages are static, provider-free strings; keep a bounded one-line copy
    # so a needs_review outcome without an error code stays diagnosable from logs.
    safe_reason = " ".join(str(result.message or "").replace("\x00", "").split())[:200]
    _logger.warning(
        "investigation tool invocation finished in a failure state"
        + (f": {safe_reason}" if safe_reason else ""),
        extra={
            "event": "investigation_tool_invocation_terminal_failure",
            "tool_name": safe_tool,
            "exception_class": safe_exception,
            "error_code": safe_code,
            "investigation_id": str(invocation.investigation_id),
            "invocation_id": str(invocation.id),
            "status": invocation.status.value,
        },
    )


__all__ = [
    "InvestigationService",
    "InvocationReconciliationOutcome",
    "ProductionArtifactContextSnapshotRepository",
    "ProductionArtifactObservationWriter",
    "ToolObservationWriter",
]
