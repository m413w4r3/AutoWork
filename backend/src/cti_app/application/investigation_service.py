"""Agent-directed investigation orchestration over the A1 contracts and ports."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import replace
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
from cti_app.application.investigation_contracts import (
    AgentTurnDecisionV1,
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
    InvestigationEnvelopeRepository,
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
)
from cti_app.application.persistence import (
    AnalystDecisionRepository,
    AnalystInvestigationRepository,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import (
    USER_REQUEST_ACTION_PREFIX,
    ArticleSnapshot,
    ResearchContextBuilder,
)
from cti_app.domain.analyst_opinion import (
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlock,
    verify_opinion_citations,
)
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    AgendaItem,
    AgentNextAction,
    BudgetDimension,
    BudgetExceeded,
    BudgetReservationStatus,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    NoveltyClass,
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
)
from cti_app.domain.production import (
    AnalystInvestigation,
    AnalystInvestigationStatus,
    LoopBudget,
    ProductionArtifact,
    ProductionRun,
)

_FINDING_NAMESPACE = UUID("3e2a06e9-660e-44a7-9a9c-6db60a16ff55")
_OPINION_NAMESPACE = UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d")
_INVOCATION_RECONCILIATION_NAMESPACE = UUID("ac8d1f15-c6ca-4808-97db-d3b63d34f851")
_INVALID_BRIDGE_ACTION_PREFIX = "bridge_invalid_output:"


class InvocationReconciliationOutcome(StrEnum):
    EXECUTED_WITH_RESULT = "executed_with_result"
    NOT_EXECUTED = "not_executed"
    ABANDON = "abandon"


class ToolObservationWriter(Protocol):
    async def archive(
        self, invocation: ToolInvocation, result: ToolResult
    ) -> tuple[EvidenceObservation, ...]: ...


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
        if result.observation_refs:
            return ()
        safe_result = _sanitize_json(thaw_json(result.output))
        if not safe_result:
            return ()
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
            object_type="tool_call",
            object_id=invocation.operation_key,
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
        observation_writer: ToolObservationWriter | None = None,
        available_capabilities: frozenset[str] = frozenset(),
        author_id: str = "bridge-investigator",
        no_progress_limit: int = 2,
        max_turns_per_run: int = 64,
        commit_uow: Callable[[], Awaitable[None]] | None = None,
        audit_decisions: AnalystDecisionRepository | None = None,
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
        self._observation_writer = observation_writer
        self._available_capabilities = frozenset(available_capabilities)
        self._author_id = author_id
        self._no_progress_limit = no_progress_limit
        self._max_turns_per_run = max_turns_per_run
        self._commit_uow = commit_uow
        owner = getattr(commit_uow, "__self__", None)
        self._audit_decisions = audit_decisions or getattr(owner, "analyst_decisions", None)

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
            return existing_state
        if envelope is not None and envelope.investigation_id != investigation.id:
            raise ValueError("Investigation envelope belongs to another investigation")
        existing_envelope = await self._envelopes.get(investigation.id)
        if existing_envelope is None and envelope is not None:
            envelope = self._with_turn_budget(envelope, investigation.budget)
            await self._envelopes.save_if_version(envelope, expected_version=None)
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
        )
        await self._states.save_if_version(state, expected_version=None)
        if investigation.status is AnalystInvestigationStatus.QUEUED:
            investigation.start()
            await self._investigations.save(investigation)
        return state

    async def resume(self, investigation_id: UUID) -> InvestigationWorkingState:
        state = await self._require_state(investigation_id)
        investigation = await self._require_investigation(investigation_id)
        unresolved = tuple(
            item
            for item in await self._invocations.list_for_investigation(investigation_id)
            if item.status is ToolInvocationStatus.NEEDS_REVIEW and not item.response_refs
        )
        if unresolved:
            raise ValueError(
                "Ambiguous tool invocations must be reconciled before resuming: "
                + ", ".join(str(item.id) for item in unresolved[:8])
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
        }:
            investigation.status = AnalystInvestigationStatus.RUNNING
            investigation.finished_at = None
            investigation._bump()
            await self._investigations.save(investigation)
        return state

    async def interrupt(self, investigation_id: UUID) -> InvestigationWorkingState:
        return await self.conclude(investigation_id, StopReason.USER_INTERRUPT)

    async def conclude(
        self, investigation_id: UUID, reason: StopReason
    ) -> InvestigationWorkingState:
        state = await self._require_state(investigation_id)
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
        if state.stop_reason is not None:
            return state
        if investigation.status is AnalystInvestigationStatus.QUEUED:
            investigation.start()
            await self._investigations.save(investigation)
        if investigation.status is not AnalystInvestigationStatus.RUNNING:
            return state

        for _ in range(self._max_turns_per_run):
            state = await self._require_state(investigation_id)
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
            allowed = self._external_allowed(snapshot, envelope)
            if not allowed:
                # Context construction is the local read path; no model transport runs.
                return await self.conclude(investigation_id, StopReason.MODEL_UNAVAILABLE)
            context_bytes = context.canonical_bytes()
            context_size_bytes = len(context_bytes)
            context_sha256 = hashlib.sha256(context_bytes).hexdigest()
            user_request_ids = tuple(
                item.request_id for item in context.investigation.user_requests
            )
            if state.pending_context_snapshot_id is None:
                context_snapshot_id = await self._context_snapshots.store(
                    context_bytes.decode("utf-8")
                )
                pending = state.with_pending_context(context_snapshot_id, context_sha256)
                await self._save_state(state, pending)
                state = pending
            bridge_key = f"bridge:{investigation_id}:{state.turn_index}:{context_sha256}"
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
                budget.commit(bridge_key)
            except BudgetExceeded:
                return await self.conclude(investigation_id, StopReason.BUDGET_EXHAUSTED)
            if envelope is not None and reservation_before is not BudgetReservationStatus.COMMITTED:
                envelope = await self._save_envelope_version(envelope)
                # Acquisition tools use their own UoW. Commit this approved
                # budget snapshot before the Bridge/adapter can read it there.
                await self._commit_current_uow()
            turn = await self._bridge.turn(
                investigation_id=investigation_id,
                subject_id=investigation.subject_id,
                turn_index=state.turn_index,
                context=context,
                external_llm_allowed=allowed,
                on_invalid_output=lambda key, detail: self._record_bridge_rejection(
                    investigation_id, key, detail
                ),
            )
            state = await self._require_state(investigation_id)
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
                item.operation_key
                for item in await self._invocations.list_for_investigation(investigation_id)
            }
            outcome = await self._process_turn(
                investigation=investigation,
                envelope=envelope,
                budget=budget,
                state=state,
                decision=decision,
                context_sha256=turn.context_sha256,
                user_request_ids=user_request_ids,
            )
            latest_state = outcome[0]
            investigation = await self._require_investigation(investigation_id)
            next_action = decision.next_action
            if outcome[1] is not None:
                return await self.conclude(investigation_id, outcome[1])
            if next_action is AgentNextAction.FINISH:
                return await self.conclude(investigation_id, StopReason.AGENT_FINISHED)
            if next_action is AgentNextAction.REQUEST_PERMISSION:
                return await self.conclude(investigation_id, StopReason.PERMISSION_REQUIRED)
            if (
                latest_state.no_progress_streak >= self._no_progress_limit
                and next_action is not AgentNextAction.SWITCH_HYPOTHESIS
                and not _proposes_unseen_operation(investigation_id, decision, seen_operation_keys)
            ):
                return await self.conclude(investigation_id, StopReason.NO_NEW_INFORMATION)
            state = latest_state
        # Bound one worker invocation; the investigation remains resumable and active.
        return state

    async def _process_turn(
        self,
        *,
        investigation: AnalystInvestigation,
        envelope: InvestigationEnvelope | None,
        budget: InvestigationBudget,
        state: InvestigationWorkingState,
        decision: AgentTurnDecisionV1,
        context_sha256: str,
        user_request_ids: tuple[UUID, ...],
    ) -> tuple[InvestigationWorkingState, StopReason | None]:
        observation_ids: list[UUID] = []
        finding_ids: list[UUID] = []
        action_summaries: list[str] = []
        progress = False
        turn_envelope = envelope
        policy_changed_stop = False
        for proposed in decision.proposed_tool_calls:
            definition_adapter = self._registry.get(proposed.tool_name, proposed.schema_version)
            definition = definition_adapter[0] if definition_adapter else None
            base_operation_key = tool_operation_key(
                investigation_id=investigation.id,
                tool_name=proposed.tool_name,
                schema_version=proposed.schema_version,
                normalized_parameters=proposed.arguments,
                purpose=proposed.purpose,
            )
            operation_key = base_operation_key
            invocation = await self._invocations.get_by_operation_key(base_operation_key)
            external_effect = definition is not None and definition.effect in {
                ToolEffect.EXTERNAL_READ,
                ToolEffect.ACQUIRE,
                ToolEffect.EXTERNAL_ACTIVE,
            }
            if (
                invocation is not None
                and invocation.status is not ToolInvocationStatus.POLICY_DENIED
            ):
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
                if _envelope_reduced_since(turn_envelope, current_envelope):
                    policy_changed_stop = True
                    denied_key = (
                        f"{base_operation_key}:g2-denied:"
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
                    break
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
                    operation_key = f"{base_operation_key}:g2:{current_envelope.version}"
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

            cost = thaw_json(definition.cost_model) if definition else {}
            try:
                reservation_before = _reservation_status(budget, operation_key)
                budget.reserve(operation_key, cost)
            except BudgetExceeded:
                # The dispatcher repeats the preflight and returns a typed result.
                reservation_before = BudgetReservationStatus.RELEASED
            invocation = ToolInvocation(
                investigation_id=investigation.id,
                operation_key=operation_key,
                tool_name=proposed.tool_name,
                schema_version=proposed.schema_version,
                normalized_parameters=proposed.arguments,
                purpose=proposed.purpose,
                reserved_budget=cost,
            )
            invocation = await self._invocations.add_if_absent(invocation)
            if invocation.status is ToolInvocationStatus.PENDING:
                running = invocation.transition(ToolInvocationStatus.RUNNING)
                invocation = await self._invocations.update_if_version(
                    running, expected_version=invocation.version
                )
            elif invocation.status in {
                ToolInvocationStatus.SUCCEEDED,
                ToolInvocationStatus.EMPTY,
            }:
                observation_ids.extend(invocation.response_refs)
                if set(invocation.response_refs).difference(state.last_seen_evidence_ids):
                    progress = True
                action_summaries.append(
                    _action_summary(invocation.tool_name, invocation.status, "replayed")
                )
                continue
            else:
                return state, StopReason.SUBMISSION_NEEDS_REVIEW

            if envelope is not None:
                envelope = await self._save_envelope_version(envelope)
                budget = envelope.budget
            # The RUNNING row and its budget reservation must survive a worker
            # crash after the remote side has accepted the operation.
            await self._commit_current_uow()

            tool_context = ToolExecutionContext(
                investigation_id=investigation.id,
                envelope=envelope,
                budget=budget,
                idempotency_key=operation_key,
                available_capabilities=self._available_capabilities,
            )
            result = await self._dispatcher.dispatch(
                tool_context,
                tool_name=proposed.tool_name,
                schema_version=proposed.schema_version,
                arguments=proposed.arguments,
                purpose=proposed.purpose,
                operation_key=operation_key,
            )
            if result.status in {
                ToolInvocationStatus.UNAVAILABLE,
                ToolInvocationStatus.POLICY_DENIED,
                ToolInvocationStatus.NEEDS_REVIEW,
                ToolInvocationStatus.FAILED,
            }:
                budget.release(operation_key)
            try:
                persisted_observations = await self._persist_result_observations(invocation, result)
            except Exception:
                valid_refs = await self._valid_observation_refs(
                    investigation.id, result.observation_refs
                )
                reviewed = invocation.transition(
                    ToolInvocationStatus.NEEDS_REVIEW,
                    response_refs=valid_refs,
                    consumed_budget=_actual_tool_consumption(budget, operation_key, result),
                )
                await self._invocations.update_if_version(
                    reviewed, expected_version=invocation.version
                )
                if envelope is not None and _reservation_status(budget, operation_key) != (
                    reservation_before
                ):
                    envelope = await self._save_envelope_version(envelope)
                await self._commit_current_uow()
                return state, StopReason.SUBMISSION_NEEDS_REVIEW
            result_refs = list(result.observation_refs)
            result_refs.extend(item.id for item in persisted_observations)
            valid_refs = await self._valid_observation_refs(
                investigation.id, tuple(dict.fromkeys(result_refs))
            )
            observation_ids.extend(valid_refs)
            if set(valid_refs).difference(state.last_seen_evidence_ids):
                progress = True
            terminal = invocation.transition(
                result.status,
                response_refs=valid_refs,
                consumed_budget=_actual_tool_consumption(budget, operation_key, result),
            )
            invocation = await self._invocations.update_if_version(
                terminal, expected_version=invocation.version
            )
            if envelope is not None and _reservation_status(budget, operation_key) != (
                reservation_before
            ):
                envelope = await self._save_envelope_version(envelope)
            await self._commit_current_uow()
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
            if result.status is ToolInvocationStatus.NEEDS_REVIEW and not result.output.get(
                "request_permission", False
            ):
                return state, StopReason.PERMISSION_REQUIRED
            # An ordinary policy denial is local to the operation; G2 reductions above
            # end this turn before any other proposed external operation is started.

        new_findings = await self._persist_findings(
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
        opinion_written = await self._apply_opinion_delta(
            investigation_id=investigation.id,
            state=state,
            delta=decision.opinion_delta,
        )
        progress = progress or opinion_written
        all_observations = await self._observations.list_for_investigation(investigation.id)
        all_findings = await self._findings.list_for_investigation(investigation.id)
        hypotheses = self._update_hypotheses(
            state.hypotheses,
            decision=decision,
            findings=new_findings,
        )
        decision_sha256 = hashlib.sha256(decision.canonical_bytes()).hexdigest()
        turn_state = replace(
            state,
            recent_actions=tuple(
                action
                for action in state.recent_actions
                if not _user_request_was_in_context(action, user_request_ids)
            ),
        )
        updated = turn_state.with_turn(
            context_sha256=context_sha256,
            decision_sha256=decision_sha256,
            progress=progress,
            evidence_ids=tuple(item.id for item in all_observations),
            finding_ids=tuple(item.id for item in all_findings),
            action_summaries=tuple(action_summaries),
            hypotheses=hypotheses,
        )
        await self._save_state(state, updated)
        # Tools such as hit acquisition consume the aggregate's LoopBudget in their
        # own unit of work; apply only this turn's mutation on the current row so
        # a stale in-memory copy never overwrites (or is rejected by) their write.
        current = await self._require_investigation(investigation.id)
        current.record_turn(progress=progress)
        await self._investigations.save(current)
        await self._commit_current_uow()
        return (
            updated,
            StopReason.PERMISSION_REQUIRED if policy_changed_stop else None,
        )

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
            return (
                ToolResult(
                    status=status,
                    observation_refs=invocation.response_refs,
                    budget_consumed=invocation.consumed_budget,
                    message="Reused persisted tool invocation.",
                ),
                invocation.response_refs,
                None,
            )
        if status is ToolInvocationStatus.RUNNING:
            reviewed = invocation.transition(ToolInvocationStatus.NEEDS_REVIEW)
            await self._invocations.update_if_version(reviewed, expected_version=invocation.version)
            await self._commit_current_uow()
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
                    output={"request_permission": True},
                    message="Prior tool result is awaiting an explicit permission decision.",
                ),
                invocation.response_refs,
                None,
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
        if not result.output or result.observation_refs:
            return ()
        if self._observation_writer is None:
            raise RuntimeError("No observation archive is configured for tool output")
        return await self._observation_writer.archive(invocation, result)

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
        for proposal in proposals:
            # Hypothesis links are transient annotations for the working state,
            # not part of the finding's persistent evidence identity.
            proposal_payload = proposal.model_dump(mode="json", exclude={"hypothesis_refs"})
            finding_id = uuid5(
                _FINDING_NAMESPACE,
                f"{investigation_id}:{hashlib.sha256(canonical_json_bytes(proposal_payload)).hexdigest()}",
            )
            existing = await self._findings.get(finding_id)
            if existing is not None:
                persisted.append(existing)
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
            persisted.append(
                await self._findings.append_version(finding, expected_previous_id=None)
            )
        return tuple(persisted)

    async def _apply_opinion_delta(
        self,
        *,
        investigation_id: UUID,
        state: InvestigationWorkingState,
        delta: Any | None,
    ) -> bool:
        if (
            delta is None
            or state.base_publication_artifact_id is None
            or state.base_publication_sha256 is None
        ):
            return False
        opinion_id = uuid5(_OPINION_NAMESPACE, str(investigation_id))
        current = await self._opinions.get(opinion_id)
        findings = tuple(await self._findings.list_for_investigation(investigation_id))
        observations = tuple(await self._observations.list_for_investigation(investigation_id))
        finding_by_id = {item.id: item for item in findings}
        observation_ids = {item.id for item in observations}
        blocks_by_type = {item.block_type: item for item in current.blocks} if current else {}
        unresolved = list(current.unresolved_points if current else ())
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
                continue
            blocks_by_type[block.block_type] = block
        unresolved.extend(delta.unresolved_points)
        blocks = tuple(blocks_by_type.values())
        if (
            current is not None
            and blocks == current.blocks
            and tuple(unresolved) == current.unresolved_points
        ):
            return False
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
            return False
        await self._opinions.append_version(opinion, expected_version=expected_version)
        return True

    @staticmethod
    def _update_hypotheses(
        hypotheses: tuple[Hypothesis, ...],
        *,
        decision: AgentTurnDecisionV1,
        findings: tuple[InvestigationFinding, ...],
    ) -> tuple[Hypothesis, ...]:
        if not hypotheses or not decision.hypothesis_refs:
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
        target_ids = set(decision.hypothesis_refs)
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
                status = HypothesisStatus.OPEN
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
        tlp = (snapshot.tlp or "").strip().lower().replace("tlp:", "")
        if envelope is None:
            return bool(
                snapshot.external_llm_allowed and not snapshot.do_not_submit and tlp != "red"
            )
        return bool(
            snapshot.external_llm_allowed
            and envelope.disclosure.external_llm_allowed
            and not snapshot.do_not_submit
            and not envelope.disclosure.do_not_submit
            and tlp != "red"
            and (envelope.disclosure.tlp is None or envelope.disclosure.tlp.value != "red")
        )

    @staticmethod
    def _unapproved_budget(investigation: AnalystInvestigation) -> InvestigationBudget:
        return InvestigationBudget(
            limits={
                BudgetDimension.BRIDGE_TURNS: investigation.budget.max_cycles,
                BudgetDimension.VT_UNITS: 0,
                BudgetDimension.SHODAN_CREDITS: 0,
                BudgetDimension.DOWNLOADS: 0,
            }
        )

    async def _save_envelope_version(
        self, envelope: InvestigationEnvelope
    ) -> InvestigationEnvelope:
        updated = replace(envelope, version=envelope.version + 1)
        return await self._envelopes.save_if_version(updated, expected_version=envelope.version)

    async def _commit_current_uow(self) -> None:
        if self._commit_uow is not None:
            await self._commit_uow()

    @staticmethod
    def _with_turn_budget(
        envelope: InvestigationEnvelope, legacy_budget: LoopBudget
    ) -> InvestigationEnvelope:
        limits = dict(envelope.budget.limits)
        limits.setdefault(BudgetDimension.BRIDGE_TURNS, legacy_budget.max_cycles)
        if limits == dict(envelope.budget.limits):
            return envelope
        return replace(
            envelope, budget=InvestigationBudget(limits=limits), version=envelope.version
        )

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
        try:
            await self._states.save_if_version(updated, expected_version=current.version)
        except InvestigationStateVersionConflict:
            raise


def _user_request_was_in_context(action: str, request_ids: tuple[UUID, ...]) -> bool:
    if not action.startswith(USER_REQUEST_ACTION_PREFIX):
        return False
    try:
        payload = json.loads(action[len(USER_REQUEST_ACTION_PREFIX) :])
        request_id = UUID(str(payload.get("request_id", "")))
    except (AttributeError, TypeError, ValueError, json.JSONDecodeError):
        return False
    return request_id in request_ids


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


def _proposes_unseen_operation(
    investigation_id: UUID,
    decision: AgentTurnDecisionV1,
    seen_operation_keys: set[str],
) -> bool:
    return any(
        tool_operation_key(
            investigation_id=investigation_id,
            tool_name=call.tool_name,
            schema_version=call.schema_version,
            normalized_parameters=call.arguments,
            purpose=call.purpose,
        )
        not in seen_operation_keys
        for call in decision.proposed_tool_calls
    )


def _envelope_reduced_since(
    previous: InvestigationEnvelope | None,
    current: InvestigationEnvelope | None,
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
    if previous.disclosure.do_not_submit is False and current.disclosure.do_not_submit:
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
    if definition.effect in {ToolEffect.EXTERNAL_READ, ToolEffect.ACQUIRE}:
        provider = definition.capability_key.partition(".")[0]
        if (
            provider not in envelope.allowed_providers
            or (envelope.disclosure.tlp is not None and envelope.disclosure.tlp.value == "red")
            or envelope.disclosure.do_not_submit
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


def _action_summary(tool_name: str, status: ToolInvocationStatus, message: str) -> str:
    safe_tool_name = sanitize_evidence_text(tool_name, max_chars=128)
    safe_message = sanitize_evidence_text(
        " ".join(message.replace("\x00", "").split()), max_chars=300
    )
    return f"{safe_tool_name}: {status.value}{': ' + safe_message if safe_message else ''}"


__all__ = [
    "InvestigationService",
    "InvocationReconciliationOutcome",
    "ProductionArtifactContextSnapshotRepository",
    "ProductionArtifactObservationWriter",
    "ToolObservationWriter",
]
