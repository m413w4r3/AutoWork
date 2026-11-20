"""Transactional registry and proposal effects for canonical editorial writes."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import Any, Protocol, cast
from uuid import UUID, uuid4

from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    registry_hash,
)
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalState,
)
from cti_app.domain.editorial_proposal_impact import EditorialProposalImpact
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialRevisionStatus,
    EditorialSourceStage,
    bootstrap_block_registry,
    reconcile_exact_block_registry,
    workspace_pin_hash,
)
from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage


class EditorialRevisionEffectsError(RuntimeError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class EditorialWorkspaceAdvancer(Protocol):
    def advance(
        self,
        *,
        base_registry: tuple[BlockRegistryEntry, ...],
        artifact_id: UUID,
        canonical_sha256: str,
        source_stage: str,
        canonical_content: Mapping[str, Any],
        lineage: tuple[Mapping[str, object], ...],
        retired_block_ids: tuple[UUID, ...],
    ) -> Any: ...


@dataclass(frozen=True, slots=True)
class EditorialRunGuard:
    production_run_id: UUID


@dataclass(frozen=True, slots=True)
class EditorialRevisionEffectsResult:
    snapshot: WorkspaceSnapshotRecord
    impact: EditorialProposalImpact | None = None


class EditorialRevisionEffects:
    """Apply the effects shared by adoption and direct legacy canonical writers."""

    def __init__(self, *, workspace_service: Any) -> None:
        self._workspace_service = cast(EditorialWorkspaceAdvancer, workspace_service)

    async def acquire_run_guard(self, uow: Any, production_run_id: UUID) -> EditorialRunGuard:
        await uow.editorial_proposals.lock_adoption_for_run(production_run_id)
        return EditorialRunGuard(production_run_id)

    async def lock_run_row(self, uow: Any, guard: EditorialRunGuard) -> Any:
        run = await uow.production_runs.get_for_update(guard.production_run_id)
        if run is None:
            raise EditorialRevisionEffectsError("editorial_run_missing")
        return run

    async def current_snapshot_in_uow(self, uow: Any, subject_id: UUID) -> WorkspaceSnapshotRecord:
        try:
            snapshot_record_in_uow = getattr(
                self._workspace_service, "snapshot_record_in_uow", None
            )
            if snapshot_record_in_uow is None:
                raise EditorialRevisionEffectsError("editorial_registry_conflict")
            return cast(WorkspaceSnapshotRecord, await snapshot_record_in_uow(uow, subject_id))
        except Exception as error:
            code = getattr(error, "code", None) or getattr(error, "reason_code", None)
            raise EditorialRevisionEffectsError(
                str(code or "editorial_registry_conflict")
            ) from None

    async def apply_after_write(
        self,
        uow: Any,
        *,
        run_guard: EditorialRunGuard,
        base_snapshot: WorkspaceSnapshotRecord,
        stage: ProductionArtifactStage,
        artifact: ProductionArtifact,
        canonical_sha256: str,
        canonical_content: Mapping[str, Any],
        lineage: Sequence[Mapping[str, object]] | None,
        retired_block_ids: Sequence[UUID] = (),
        actor_id: UUID,
        accepted_proposal: EditorialProposal | None = None,
    ) -> EditorialRevisionEffectsResult:
        if (
            run_guard.production_run_id != base_snapshot.production_run_id
            or artifact.production_run_id != base_snapshot.production_run_id
            or artifact.subject_id != base_snapshot.subject_id
            or artifact.stage is not stage
        ):
            raise EditorialRevisionEffectsError("editorial_registry_conflict")
        if stage not in {
            ProductionArtifactStage.SYNTHESIS,
            ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        }:
            raise EditorialRevisionEffectsError("editorial_revision_stage_unsupported")
        if accepted_proposal is not None and (
            accepted_proposal.production_run_id != run_guard.production_run_id
            or accepted_proposal.state is not EditorialProposalState.ACCEPTED
            or accepted_proposal.accepted_artifact_id != artifact.id
        ):
            raise EditorialRevisionEffectsError("editorial_proposal_state_conflict")

        effective_lineage = (
            tuple(lineage)
            if lineage is not None
            else _legacy_lineage(
                base_snapshot.block_registry,
                artifact_id=artifact.id,
                canonical_sha256=canonical_sha256,
                source_stage=stage.value,
                canonical_content=canonical_content,
            )
        )
        snapshot = await self._append_workspace_snapshot(
            uow,
            base_snapshot,
            stage,
            artifact,
            canonical_sha256=canonical_sha256,
            canonical_content=canonical_content,
            lineage=effective_lineage,
            retired_block_ids=tuple(retired_block_ids),
        )
        await stale_changed_pin_proposals(
            uow,
            production_run_id=run_guard.production_run_id,
            actor_id=actor_id,
            current_record=snapshot,
            base_proposal=accepted_proposal,
        )

        impact = None
        if accepted_proposal is not None:
            sequence = await uow.editorial_proposal_impacts.next_sequence_for_run(
                accepted_proposal.production_run_id
            )
            impact = EditorialProposalImpact(
                impact_id=uuid4(),
                subject_id=accepted_proposal.subject_id,
                production_run_id=accepted_proposal.production_run_id,
                proposal_id=accepted_proposal.id,
                accepted_artifact_id=artifact.id,
                accepted_snapshot_id=snapshot.snapshot_id,
                base_pin_hash=accepted_proposal.base_pin_hash,
                accepted_pin_hash=snapshot.workspace_pin_hash,
                sequence=sequence,
            )
            await uow.editorial_proposal_impacts.add(impact)
        return EditorialRevisionEffectsResult(snapshot=snapshot, impact=impact)

    async def _append_workspace_snapshot(
        self,
        uow: Any,
        base: WorkspaceSnapshotRecord,
        stage: ProductionArtifactStage,
        artifact: ProductionArtifact,
        *,
        canonical_sha256: str,
        canonical_content: Mapping[str, Any],
        lineage: tuple[Mapping[str, object], ...],
        retired_block_ids: tuple[UUID, ...],
    ) -> WorkspaceSnapshotRecord:
        source_stage = EditorialSourceStage(stage.value)
        base_stage_entries = tuple(
            entry for entry in base.block_registry if entry.source_stage is source_stage
        )
        try:
            advanced = self._workspace_service.advance(
                base_registry=base_stage_entries,
                artifact_id=artifact.id,
                canonical_sha256=canonical_sha256,
                source_stage=stage.value,
                canonical_content=canonical_content,
                lineage=lineage,
                retired_block_ids=retired_block_ids,
            )
        except Exception as error:
            code = getattr(error, "code", None) or getattr(error, "reason_code", None)
            raise EditorialRevisionEffectsError(
                str(code or "editorial_registry_conflict")
            ) from None

        unchanged_entries = tuple(
            entry for entry in base.block_registry if entry.source_stage is not source_stage
        )
        entries = (
            (*advanced.active, *unchanged_entries)
            if source_stage is EditorialSourceStage.SYNTHESIS
            else (*unchanged_entries, *advanced.active)
        )
        retired = tuple(
            sorted(set(base.retired_block_ids) | set(advanced.retired_block_ids), key=str)
        )
        prefix = "synthesis" if source_stage is EditorialSourceStage.SYNTHESIS else "enrichment"
        pins: dict[str, object] = {
            "subject_id": base.subject_id,
            "production_run_id": base.production_run_id,
            "synthesis_artifact_id": base.synthesis_artifact_id,
            "synthesis_version": base.synthesis_version,
            "synthesis_input_hash": base.synthesis_input_hash,
            "synthesis_canonical_sha256": base.synthesis_canonical_sha256,
            "enrichment_artifact_id": base.enrichment_artifact_id,
            "enrichment_version": base.enrichment_version,
            "enrichment_input_hash": base.enrichment_input_hash,
            "enrichment_canonical_sha256": base.enrichment_canonical_sha256,
            "publication_artifact_id": base.publication_artifact_id,
            "publication_version": base.publication_version,
            "publication_input_hash": base.publication_input_hash,
            "publication_canonical_sha256": base.publication_canonical_sha256,
            "pipeline_generation": base.pipeline_generation,
            "routing_decision_id": base.routing_decision_id,
            "routing_decision_hash": base.routing_decision_hash,
            "publication_kind": base.publication_kind,
        }
        pins[f"{prefix}_artifact_id"] = artifact.id
        pins[f"{prefix}_version"] = artifact.version
        pins[f"{prefix}_input_hash"] = artifact.input_hash
        pins[f"{prefix}_canonical_sha256"] = canonical_sha256
        next_record = replace(
            base,
            snapshot_id=uuid4(),
            synthesis_artifact_id=(
                artifact.id
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_artifact_id
            ),
            synthesis_version=(
                artifact.version
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_version
            ),
            synthesis_input_hash=(
                artifact.input_hash
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_input_hash
            ),
            synthesis_canonical_sha256=(
                canonical_sha256
                if source_stage is EditorialSourceStage.SYNTHESIS
                else base.synthesis_canonical_sha256
            ),
            enrichment_artifact_id=(
                artifact.id
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_artifact_id
            ),
            enrichment_version=(
                artifact.version
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_version
            ),
            enrichment_input_hash=(
                artifact.input_hash
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_input_hash
            ),
            enrichment_canonical_sha256=(
                canonical_sha256
                if source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
                else base.enrichment_canonical_sha256
            ),
            workspace_pin_hash=workspace_pin_hash(pins),
            registry_hash=registry_hash(entries),
            block_registry=tuple(entries),
            retired_block_ids=retired,
            parent_snapshot_id=base.snapshot_id,
        )
        stored = await uow.editorial_workspace_snapshots.append_if_absent(next_record)
        if (
            stored.workspace_pin_hash != next_record.workspace_pin_hash
            or stored.registry_hash != next_record.registry_hash
            or stored.block_registry != next_record.block_registry
        ):
            raise EditorialRevisionEffectsError("editorial_registry_conflict")
        return cast(WorkspaceSnapshotRecord, stored)


async def stale_changed_pin_proposals(
    uow: Any,
    *,
    production_run_id: UUID,
    actor_id: UUID,
    current_record: WorkspaceSnapshotRecord | None,
    base_proposal: EditorialProposal | None = None,
) -> None:
    """Persist stale transitions for proposals whose exact pins no longer match."""
    if base_proposal is not None and base_proposal.production_run_id != production_run_id:
        raise EditorialRevisionEffectsError("editorial_proposal_state_conflict")
    for proposal in await uow.editorial_proposals.list_proposed_for_run_for_update(
        production_run_id
    ):
        if base_proposal is not None and proposal.id == base_proposal.id:
            continue
        if current_record is not None:
            fences_match = proposal_fences_match_record(proposal, current_record)
            if base_proposal is None and fences_match:
                continue
            if (
                base_proposal is not None
                and proposal.variant_group_id != base_proposal.variant_group_id
                and fences_match
            ):
                continue
        event = (
            stale_after_acceptance_event(
                proposal,
                base_proposal,
                actor_id,
                current_record.workspace_pin_hash,
            )
            if (
                base_proposal is not None
                and base_proposal.state is EditorialProposalState.ACCEPTED
                and current_record is not None
            )
            else state_event(
                proposal,
                state=EditorialProposalState.STALE,
                actor_id=actor_id,
                idempotency_key=f"stale-{proposal.id.hex}-{proposal.state_version}",
                request_sha256=state_request_sha256(proposal, "stale"),
            )
        )
        stale, changed = await uow.editorial_proposals.transition_if_current(
            subject_id=proposal.subject_id,
            occurrence_id=proposal.occurrence_id,
            proposal_id=proposal.id,
            expected_state=EditorialProposalState.PROPOSED,
            expected_state_version=proposal.state_version,
            state=EditorialProposalState.STALE,
            event=event,
        )
        if stale is None or not changed:
            raise EditorialRevisionEffectsError("proposal_state_conflict")


def proposal_fences_match_record(
    proposal: EditorialProposal, record: WorkspaceSnapshotRecord
) -> bool:
    if (
        record.subject_id != proposal.subject_id
        or record.production_run_id != proposal.production_run_id
        or record.snapshot_id != proposal.snapshot_id
        or record.workspace_pin_hash != proposal.base_pin_hash
        or record.routing_decision_hash != proposal.base_routing_hash
    ):
        return False
    if record.synthesis_artifact_id == proposal.base_artifact_id:
        return (
            record.synthesis_version == proposal.base_artifact_version
            and record.synthesis_input_hash == proposal.base_input_hash
            and record.synthesis_canonical_sha256 == proposal.base_canonical_sha256
        )
    if record.enrichment_artifact_id == proposal.base_artifact_id:
        return (
            record.enrichment_version == proposal.base_artifact_version
            and record.enrichment_input_hash == proposal.base_input_hash
            and record.enrichment_canonical_sha256 == proposal.base_canonical_sha256
        )
    return False


def state_event(
    proposal: EditorialProposal,
    *,
    state: EditorialProposalState,
    actor_id: UUID,
    idempotency_key: str,
    request_sha256: str,
) -> EditorialProposalEvent:
    return EditorialProposalEvent(
        id=uuid4(),
        proposal_id=proposal.id,
        sequence=proposal.state_version + 1,
        kind=EditorialProposalEventKind.STATE_CHANGED,
        state=state,
        state_version=proposal.state_version + 1,
        actor_id=actor_id,
        idempotency_key=idempotency_key,
        request_sha256=request_sha256,
        patch_sha256=proposal.patch_sha256,
    )


def state_request_sha256(proposal: EditorialProposal, action: str) -> str:
    body = {
        "action": action,
        "proposal_id": str(proposal.id),
        "state_version": proposal.state_version,
        "patch_sha256": proposal.patch_sha256,
    }
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def stale_after_acceptance_event(
    proposal: EditorialProposal,
    accepted: EditorialProposal,
    actor_id: UUID,
    accepted_pin_hash: str,
) -> EditorialProposalEvent:
    body = {
        "action": "stale_after_acceptance",
        "proposal_id": str(proposal.id),
        "accepted_proposal_id": str(accepted.id),
        "accepted_pin_hash": accepted_pin_hash,
        "state_version": proposal.state_version,
    }
    digest = hashlib.sha256(
        json.dumps(body, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return state_event(
        proposal,
        state=EditorialProposalState.STALE,
        actor_id=actor_id,
        idempotency_key=f"accept-stale-{accepted.id.hex}-{proposal.id.hex}",
        request_sha256=digest,
    )


def _legacy_lineage(
    base_registry: Sequence[BlockRegistryEntry],
    *,
    artifact_id: UUID,
    canonical_sha256: str,
    source_stage: str,
    canonical_content: Mapping[str, Any],
) -> tuple[Mapping[str, object], ...]:
    """Derive explicit transitions from stable registry IDs and exact content matches."""
    stage_entries = tuple(item for item in base_registry if item.source_stage.value == source_stage)
    candidates = bootstrap_block_registry(
        artifact_id=artifact_id,
        canonical_sha256=canonical_sha256,
        source_stage=source_stage,
        canonical_content=canonical_content,
    )
    reconciled = reconcile_exact_block_registry(stage_entries, candidates)
    candidate_by_pointer = {item.structural_pointer: item for item in candidates}
    candidate_by_id = {item.block_id: item for item in candidates}
    exact_by_id = {
        item.block_id: item
        for item in reconciled
        if item.revision_status is EditorialRevisionStatus.READY
        and item.block_id in {base.block_id for base in stage_entries}
    }
    used_pointers: set[str] = set()
    result: list[Mapping[str, object]] = []
    for previous in stage_entries:
        exact = exact_by_id.get(previous.block_id)
        pointer_entry: BlockRegistryEntry | None = exact
        transition = "rewrite"
        if exact is not None and exact.structural_pointer != previous.structural_pointer:
            transition = "move"
        if pointer_entry is None:
            same_pointer = candidate_by_pointer.get(previous.structural_pointer)
            if same_pointer is not None and same_pointer.kind is previous.kind:
                duplicate_old = (
                    sum(
                        item.kind is previous.kind
                        and item.content_sha256 == previous.content_sha256
                        for item in stage_entries
                    )
                    > 1
                )
                duplicate_new = (
                    sum(
                        item.kind is same_pointer.kind
                        and item.content_sha256 == same_pointer.content_sha256
                        for item in candidates
                    )
                    > 1
                )
                if previous.content_sha256 == same_pointer.content_sha256 and (
                    duplicate_old or duplicate_new
                ):
                    raise EditorialRevisionEffectsError("editorial_identity_ambiguous")
                if same_pointer.structural_pointer not in used_pointers:
                    pointer_entry = same_pointer
        if pointer_entry is None:
            exact_matches = [
                item
                for item in candidates
                if item.kind is previous.kind
                and item.content_sha256 == previous.content_sha256
                and item.structural_pointer not in used_pointers
            ]
            if len(exact_matches) > 1:
                raise EditorialRevisionEffectsError("editorial_identity_ambiguous")
            if exact_matches:
                pointer_entry = exact_matches[0]
                transition = "move"
        if pointer_entry is None:
            result.append(
                {
                    "block_id": str(previous.block_id),
                    "transition": "delete",
                    "pointer": None,
                }
            )
            continue
        if pointer_entry.structural_pointer in used_pointers:
            raise EditorialRevisionEffectsError("editorial_identity_ambiguous")
        used_pointers.add(pointer_entry.structural_pointer)
        result.append(
            {
                "block_id": str(previous.block_id),
                "transition": transition,
                "pointer": pointer_entry.structural_pointer,
            }
        )
    result.extend(
        {
            "transition": "insert",
            "pointer": item.structural_pointer,
        }
        for item in candidates
        if item.structural_pointer not in used_pointers
    )
    if len(candidate_by_id) != len(candidates):
        raise EditorialRevisionEffectsError("editorial_registry_conflict")
    return tuple(result)
