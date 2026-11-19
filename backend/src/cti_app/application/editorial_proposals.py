"""Prepare immutable editorial proposal patches without writing canonical artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, replace
from io import BytesIO
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.blob_storage import BlobStore
from cti_app.application.editorial_enrichment_revision import (
    assert_requested_placement,
    targeted_action,
    validate_editorial_enrichment_patch,
    validate_targeted_action,
    validate_targeted_request,
)
from cti_app.application.editorial_synthesis.patch_validation import (
    TargetedSynthesisRevisionValidationError,
    assert_non_target_unchanged,
    parse_wire_patch,
)
from cti_app.application.editorial_synthesis.targets import (
    resolve_target,
)
from cti_app.application.editorial_workspace.registry import (
    WorkspaceOccurrenceResolution,
    read_pointer,
    resolve_occurrence,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalConflict,
    EditorialProposalEvent,
    EditorialProposalEventKind,
    EditorialProposalModelRun,
    EditorialProposalOrigin,
    EditorialProposalState,
    variant_group_identity,
)
from cti_app.domain.editorial_thread import EditorialThreadEventKind
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.domain.errors import BlobIntegrityError
from cti_app.domain.model_conversations import ConversationTurnStatus
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionMode,
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionRequest,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)

_PATCH_BUCKET = "editorial-proposal-patches"
_IDEMPOTENCY_KEY = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_MAX_PATCH_BYTES = 256 * 1024


class EditorialProposalApplicationError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class _ProposalPatchBlobMissing(Exception):
    """Signal that the locked blob address must be repopulated outside the UOW."""


@dataclass(frozen=True, slots=True)
class EditorialProposalPreparation:
    """Server-resolved input captured for the exact Bridge proposal turn."""

    subject_id: UUID
    production_run_id: UUID
    snapshot_id: UUID
    occurrence_id: UUID
    thread_id: UUID
    base_artifact_id: UUID
    base_artifact_version: int
    base_input_hash: str
    base_canonical_sha256: str
    base_pin_hash: str
    routing_decision_hash: str | None
    target_id: str
    target: TargetedEditorialRevisionTarget
    operation: TargetedEditorialRevisionOperation
    before_hash: str
    base_content: Mapping[str, Any]
    raw_response: str
    model_run_id: UUID
    model_turn_id: UUID
    actor_id: UUID
    idempotency_key: str
    request_sha256: str
    evidence_refs: tuple[dict[str, object], ...] = ()
    instruction: str = ""
    requester: str = "bridge"
    enrichment_context: Mapping[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class EditorialProposalRead:
    id: UUID
    subject_id: UUID
    production_run_id: UUID
    occurrence_id: UUID
    thread_id: UUID | None
    state: str
    state_version: int
    operation: str
    base_artifact_id: UUID
    base_artifact_version: int
    base_canonical_sha256: str
    base_text: str | None
    patch_sha256: str | None
    patch: dict[str, object] | None
    diagnostics: dict[str, object]
    created_at: str


class EditorialProposalService:
    """Persist validated proposal state while keeping canonical artifacts read-only."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        blob_store: BlobStore | None = None,
        artifact_store: ProductionArtifactStore | None = None,
        workspace_service: Any | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._blob_store = blob_store
        self._artifact_store = artifact_store
        self._workspace_service = workspace_service

    async def prepare_bridge_result(
        self, preparation: EditorialProposalPreparation
    ) -> EditorialProposal:
        if not _IDEMPOTENCY_KEY.fullmatch(preparation.idempotency_key):
            raise EditorialProposalApplicationError("idempotency_key_invalid")
        preparation = await self._resolve_authoritative_preparation(preparation)
        patch_payload, state, kind, diagnostics = self._validate_result(preparation)
        encoded_patch = (
            ProductionArtifactStore.canonical_json_bytes(patch_payload)
            if patch_payload is not None
            else None
        )
        if encoded_patch is not None and len(encoded_patch) > _MAX_PATCH_BYTES:
            encoded_patch = None
            state = EditorialProposalState.NEEDS_REVIEW
            kind = EditorialProposalEventKind.NEEDS_REVIEW
            diagnostics = {"code": "proposal_patch_too_large"}

        descriptor: BlobDescriptor | None = None
        if encoded_patch is not None:
            descriptor = await self._upload_and_verify_patch(encoded_patch)

        try:
            while True:
                try:
                    stored = await self._persist_prepared_result(
                        preparation,
                        encoded_patch=encoded_patch,
                        descriptor=descriptor,
                        state=state,
                        kind=kind,
                        diagnostics=diagnostics,
                    )
                except _ProposalPatchBlobMissing:
                    if encoded_patch is None:
                        raise EditorialProposalApplicationError(
                            "proposal_patch_payload_missing"
                        ) from None
                    # Cleanup may have won the address lock after the initial read-back.
                    # Restore and verify outside the relational UOW, then retry under lock.
                    descriptor = await self._upload_and_verify_patch(encoded_patch)
                    continue
                return stored
        except Exception:
            if descriptor is not None:
                await self._best_effort_orphan_cleanup(descriptor)
            raise

    async def _resolve_authoritative_preparation(
        self, preparation: EditorialProposalPreparation
    ) -> EditorialProposalPreparation:
        if self._workspace_service is None:
            raise EditorialProposalApplicationError("proposal_workspace_unavailable")
        try:
            resolved = await self._workspace_service.resolve_revision_target(
                subject_id=preparation.subject_id,
                snapshot_id=preparation.snapshot_id,
                block_id=preparation.occurrence_id,
                operation=preparation.operation.value,
            )
        except EditorialWorkspaceError as error:
            code = (
                "proposal_base_stale"
                if error.code == "editorial_snapshot_stale"
                else "proposal_target_ambiguous"
                if error.code == "editorial_identity_ambiguous"
                else "proposal_target_mismatch"
            )
            raise EditorialProposalApplicationError(code) from None

        record = resolved.record
        if (
            record.snapshot_id != preparation.snapshot_id
            or record.subject_id != preparation.subject_id
            or record.production_run_id != preparation.production_run_id
            or record.workspace_pin_hash != preparation.base_pin_hash
            or record.routing_decision_hash != preparation.routing_decision_hash
            or record.synthesis_artifact_id != preparation.base_artifact_id
            or record.synthesis_version != preparation.base_artifact_version
            or record.synthesis_input_hash != preparation.base_input_hash
            or record.synthesis_canonical_sha256 != preparation.base_canonical_sha256
        ):
            raise EditorialProposalApplicationError("proposal_base_stale")

        supplied_base_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(dict(preparation.base_content))
        ).hexdigest()
        resolved_base_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(dict(resolved.canonical_content))
        ).hexdigest()
        if (
            supplied_base_sha256 != record.synthesis_canonical_sha256
            or resolved_base_sha256 != record.synthesis_canonical_sha256
        ):
            raise EditorialProposalApplicationError("proposal_base_stale")
        if (
            resolved.occurrence.entry is None
            or resolved.occurrence.binding_state != "valid"
            or resolved.occurrence.artifact_id != record.synthesis_artifact_id
            or resolved.occurrence.artifact_version != record.synthesis_version
            or resolved.occurrence.canonical_sha256 != record.synthesis_canonical_sha256
            or preparation.target != resolved.target
            or preparation.target_id != resolved.target_id
            or preparation.before_hash != resolved.before_hash
        ):
            raise EditorialProposalApplicationError("proposal_target_mismatch")
        return replace(
            preparation,
            target=resolved.target,
            target_id=resolved.target_id,
            before_hash=resolved.before_hash,
            base_content=resolved.canonical_content,
        )

    async def _upload_and_verify_patch(self, encoded_patch: bytes) -> BlobDescriptor:
        if self._blob_store is None:
            raise EditorialProposalApplicationError("proposal_patch_storage_unavailable")
        descriptor: BlobDescriptor | None = None
        try:
            for attempt in range(2):
                descriptor = await self._blob_store.put(
                    BytesIO(encoded_patch),
                    logical_bucket=_PATCH_BUCKET,
                    mime_type="application/vnd.autowork.editorial-patch+json",
                )
                try:
                    await self._verify_patch_blob(descriptor, encoded_patch)
                except FileNotFoundError:
                    if attempt == 0:
                        # A concurrent collector may delete the object between put and
                        # read-back. Re-upload once before opening any relational UOW.
                        continue
                    raise
                return descriptor
            raise EditorialProposalApplicationError("proposal_patch_upload_retry_exhausted")
        except Exception:
            if descriptor is not None:
                await self._best_effort_orphan_cleanup(descriptor)
            raise

    async def _verify_patch_blob(self, descriptor: BlobDescriptor, encoded_patch: bytes) -> None:
        if self._blob_store is None:
            raise EditorialProposalApplicationError("proposal_patch_storage_unavailable")
        stored_bytes = await self._blob_store.read(descriptor, max_bytes=max(descriptor.size, 1))
        digest = hashlib.sha256(stored_bytes).hexdigest()
        if (
            stored_bytes != encoded_patch
            or digest != descriptor.sha256
            or len(stored_bytes) != descriptor.size
        ):
            raise BlobIntegrityError("Editorial proposal patch failed read-back verification")

    async def _persist_prepared_result(
        self,
        preparation: EditorialProposalPreparation,
        *,
        encoded_patch: bytes | None,
        descriptor: BlobDescriptor | None,
        state: EditorialProposalState,
        kind: EditorialProposalEventKind,
        diagnostics: dict[str, object],
    ) -> EditorialProposal:
        async with self._uow_factory() as uow:
            if descriptor is not None:
                await uow.blobs.lock_address(descriptor.logical_bucket, descriptor.sha256)
                if encoded_patch is None or self._blob_store is None:
                    raise EditorialProposalApplicationError("proposal_patch_storage_unavailable")
                try:
                    await self._verify_patch_blob(descriptor, encoded_patch)
                except FileNotFoundError:
                    # The UOW exits without registering a relational reference. The caller
                    # uploads and verifies again only after this transaction releases lock.
                    raise _ProposalPatchBlobMissing from None

            await self._assert_exact_bridge_origin(uow, preparation)
            existing = await uow.editorial_proposals.get_by_idempotency_key(
                preparation.subject_id,
                preparation.actor_id,
                preparation.idempotency_key,
            )
            patch_sha256 = descriptor.sha256 if descriptor is not None else None
            if existing is not None:
                if not _replay_matches(existing, preparation, patch_sha256):
                    raise EditorialProposalConflict("proposal_idempotency_conflict")
                await self._append_thread_reference(uow, existing)
                await uow.commit()
                return existing

            blob_record: BlobRecord | None = None
            if descriptor is not None:
                blob_record = await uow.blobs.add_if_absent(BlobRecord(descriptor=descriptor))
                if (
                    blob_record.descriptor.size != descriptor.size
                    or blob_record.descriptor.mime_type != descriptor.mime_type
                ):
                    raise BlobIntegrityError("Editorial proposal blob metadata conflicts")

            proposal = EditorialProposal(
                id=uuid4(),
                subject_id=preparation.subject_id,
                production_run_id=preparation.production_run_id,
                origin=EditorialProposalOrigin.BRIDGE_DRAFT,
                snapshot_id=preparation.snapshot_id,
                occurrence_id=preparation.occurrence_id,
                base_artifact_id=preparation.base_artifact_id,
                base_artifact_version=preparation.base_artifact_version,
                base_input_hash=preparation.base_input_hash,
                base_canonical_sha256=preparation.base_canonical_sha256,
                base_routing_hash=preparation.routing_decision_hash,
                base_pin_hash=preparation.base_pin_hash,
                target_id=preparation.target_id,
                operation=preparation.operation.value,
                before_hash=preparation.before_hash,
                patch_blob_ref=blob_record.id if blob_record is not None else None,
                patch_sha256=patch_sha256,
                evidence_refs=preparation.evidence_refs,
                diagnostics=diagnostics,
                thread_id=preparation.thread_id,
                variant_group_id=variant_group_identity(
                    base_pin_hash=preparation.base_pin_hash,
                    occurrence_id=preparation.occurrence_id,
                    operation=preparation.operation.value,
                    thread_id=preparation.thread_id,
                ),
                model_run_id=preparation.model_run_id,
                model_turn_id=preparation.model_turn_id,
                state=state,
                state_version=1,
                actor_id=preparation.actor_id,
                idempotency_key=preparation.idempotency_key,
                request_sha256=preparation.request_sha256,
            )
            event = EditorialProposalEvent(
                id=uuid4(),
                proposal_id=proposal.id,
                sequence=1,
                kind=kind,
                state=state,
                state_version=1,
                actor_id=preparation.actor_id,
                idempotency_key=preparation.idempotency_key,
                request_sha256=preparation.request_sha256,
                patch_sha256=patch_sha256,
                diagnostics=diagnostics,
            )
            model_run = EditorialProposalModelRun(
                proposal_id=proposal.id,
                thread_id=preparation.thread_id,
                model_run_id=preparation.model_run_id,
                model_turn_id=preparation.model_turn_id,
            )
            stored, inserted = await uow.editorial_proposals.add_if_absent(
                proposal, event=event, model_run=model_run
            )
            if not inserted and not _replay_matches(stored, preparation, patch_sha256):
                raise EditorialProposalConflict("proposal_idempotency_conflict")
            await self._append_thread_reference(uow, stored)
            await uow.commit()
            return stored

    @staticmethod
    async def _append_thread_reference(uow: UnitOfWork, proposal: EditorialProposal) -> None:
        """Make the persisted proposal discoverable from its append-only thread timeline."""
        if proposal.thread_id is None or proposal.model_turn_id is None:
            raise EditorialProposalApplicationError("proposal_bridge_origin_invalid")
        await uow.editorial_threads.append_event(
            thread_id=proposal.thread_id,
            kind=EditorialThreadEventKind.PROPOSAL,
            actor_id=proposal.actor_id,
            idempotency_key=f"proposal:{proposal.id}",
            request_sha256=proposal.request_sha256,
            proposal_id=proposal.id,
            model_turn_id=proposal.model_turn_id,
        )

    async def inspect(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        *,
        workspace_service: Any,
    ) -> EditorialProposalRead:
        try:
            resolution = await workspace_service.resolve_occurrence(subject_id, occurrence_id)
        except Exception:
            raise EditorialProposalApplicationError("proposal_not_found") from None
        if (
            resolution.entry is None
            or resolution.binding_state != "valid"
            or resolution.record.subject_id != subject_id
        ):
            raise EditorialProposalApplicationError("proposal_not_found")

        base_artifact = None
        base_snapshot = None
        async with self._uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                subject_id, occurrence_id, proposal_id
            )
            if (
                proposal is None
                or proposal.production_run_id != resolution.record.production_run_id
            ):
                raise EditorialProposalApplicationError("proposal_not_found")
            blob = (
                await uow.blobs.get(proposal.patch_blob_ref)
                if proposal.patch_blob_ref is not None
                else None
            )
            if proposal.patch_blob_ref is not None and blob is None:
                raise EditorialProposalApplicationError("proposal_patch_unavailable")
            if self._artifact_store is not None:
                artifact_repository = getattr(uow, "production_artifacts", None)
                snapshot_repository = getattr(uow, "editorial_workspace_snapshots", None)
                if artifact_repository is not None and snapshot_repository is not None:
                    base_artifact = await artifact_repository.get(proposal.base_artifact_id)
                    base_snapshot = await snapshot_repository.get_by_id(proposal.snapshot_id)

        patch = None
        if blob is not None:
            if (
                blob.descriptor.logical_bucket != _PATCH_BUCKET
                or blob.descriptor.mime_type != "application/vnd.autowork.editorial-patch+json"
                or blob.descriptor.size > _MAX_PATCH_BYTES
            ):
                raise EditorialProposalApplicationError("proposal_patch_unavailable")
            try:
                if self._blob_store is not None:
                    raw = await self._blob_store.read(
                        blob.descriptor, max_bytes=blob.descriptor.size
                    )
                elif self._artifact_store is not None:
                    raw = await self._artifact_store.read_bytes(
                        blob.id, max_bytes=blob.descriptor.size
                    )
                else:
                    raise EditorialProposalApplicationError("proposal_patch_unavailable")
            except Exception:
                raise EditorialProposalApplicationError("proposal_patch_unavailable") from None
            digest = hashlib.sha256(raw).hexdigest()
            if (
                len(raw) != blob.descriptor.size
                or digest != proposal.patch_sha256
                or digest != blob.descriptor.sha256
            ):
                raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")
            try:
                decoded = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError):
                raise EditorialProposalApplicationError("proposal_patch_unavailable") from None
            if (
                not isinstance(decoded, dict)
                or decoded.get("schema_version") != "editorial-patch-v1"
                or decoded.get("operation") != proposal.operation
                or decoded.get("occurrence_id") != str(proposal.occurrence_id)
            ):
                raise EditorialProposalApplicationError("proposal_patch_unavailable")
            patch = decoded
        base_text = await self._read_base_text(proposal, base_artifact, base_snapshot)
        if proposal.state is EditorialProposalState.PROPOSED and not _proposal_pins_match(
            proposal, resolution
        ):
            proposal = await self._persist_stale_transition(proposal)
        return EditorialProposalRead(
            id=proposal.id,
            subject_id=proposal.subject_id,
            production_run_id=proposal.production_run_id,
            occurrence_id=proposal.occurrence_id,
            thread_id=proposal.thread_id,
            state=proposal.state.value,
            state_version=proposal.state_version,
            operation=proposal.operation,
            base_artifact_id=proposal.base_artifact_id,
            base_artifact_version=proposal.base_artifact_version,
            base_canonical_sha256=proposal.base_canonical_sha256,
            base_text=base_text,
            patch_sha256=proposal.patch_sha256,
            patch=patch,
            diagnostics=dict(proposal.diagnostics),
            created_at=proposal.created_at.isoformat(),
        )

    async def _read_base_text(
        self,
        proposal: EditorialProposal,
        artifact: Any | None,
        snapshot: Any | None,
    ) -> str | None:
        if self._artifact_store is None:
            return None
        if artifact is None or artifact.canonical_blob_id is None or snapshot is None:
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        try:
            canonical_content = await self._artifact_store.read_json(artifact.canonical_blob_id)
        except Exception:
            raise EditorialProposalApplicationError("proposal_base_unavailable") from None
        digest = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(canonical_content)
        ).hexdigest()
        if digest != proposal.base_canonical_sha256:
            raise EditorialProposalApplicationError("proposal_base_hash_mismatch")
        occurrence = resolve_occurrence(snapshot, proposal.occurrence_id)
        if (
            occurrence.entry is None
            or occurrence.binding_state != "valid"
            or occurrence.artifact_id != proposal.base_artifact_id
            or occurrence.artifact_version != proposal.base_artifact_version
            or occurrence.canonical_sha256 != proposal.base_canonical_sha256
        ):
            raise EditorialProposalApplicationError("proposal_base_unavailable")
        try:
            value = read_pointer(canonical_content, occurrence.entry.structural_pointer)
        except (KeyError, IndexError, TypeError, ValueError):
            raise EditorialProposalApplicationError("proposal_base_unavailable") from None
        if isinstance(value, str):
            return value
        if isinstance(value, Mapping):
            for key in ("text", "heading", "title"):
                text_value = value.get(key)
                if isinstance(text_value, str):
                    return text_value
        return None

    async def _persist_stale_transition(self, proposal: EditorialProposal) -> EditorialProposal:
        event = _stale_event(proposal)
        async with self._uow_factory() as uow:
            transitioned, changed = await uow.editorial_proposals.transition_if_current(
                subject_id=proposal.subject_id,
                occurrence_id=proposal.occurrence_id,
                proposal_id=proposal.id,
                expected_state=EditorialProposalState.PROPOSED,
                expected_state_version=proposal.state_version,
                state=EditorialProposalState.STALE,
                event=event,
            )
            if transitioned is None:
                raise EditorialProposalApplicationError("proposal_not_found")
            if changed:
                await uow.commit()
            return transitioned

    async def reject(
        self,
        subject_id: UUID,
        occurrence_id: UUID,
        proposal_id: UUID,
        *,
        actor_id: UUID,
        expected_state_version: int,
        patch_sha256: str | None,
        idempotency_key: str,
        workspace_service: Any,
    ) -> EditorialProposal:
        if not _IDEMPOTENCY_KEY.fullmatch(idempotency_key):
            raise EditorialProposalApplicationError("idempotency_key_invalid")
        if patch_sha256 is None or not _SHA256.fullmatch(patch_sha256):
            raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")
        if expected_state_version < 1:
            raise EditorialProposalApplicationError("proposal_state_conflict")

        request_sha256 = _decision_request_sha256(
            proposal_id=proposal_id,
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            expected_state_version=expected_state_version,
            patch_sha256=patch_sha256,
        )
        async with self._uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence(
                subject_id, occurrence_id, proposal_id
            )
            if proposal is None:
                raise EditorialProposalApplicationError("proposal_not_found")
            existing_event = await uow.editorial_proposals.get_event_by_idempotency(
                proposal_id, idempotency_key
            )
            if existing_event is not None:
                if _decision_event_matches(existing_event, actor_id, request_sha256):
                    return proposal
                raise EditorialProposalApplicationError("proposal_idempotency_conflict")

        inspected = await self.inspect(
            subject_id,
            occurrence_id,
            proposal_id,
            workspace_service=workspace_service,
        )
        if inspected.state != EditorialProposalState.PROPOSED.value:
            raise EditorialProposalApplicationError(
                "proposal_stale"
                if inspected.state == EditorialProposalState.STALE.value
                else "proposal_state_conflict"
            )
        if inspected.state_version != expected_state_version:
            raise EditorialProposalApplicationError("proposal_state_conflict")
        if inspected.patch_sha256 != patch_sha256:
            raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")

        stale = False
        async with self._uow_factory() as uow:
            proposal = await uow.editorial_proposals.get_for_occurrence_for_update(
                subject_id, occurrence_id, proposal_id
            )
            if proposal is None:
                raise EditorialProposalApplicationError("proposal_not_found")
            existing_event = await uow.editorial_proposals.get_event_by_idempotency(
                proposal_id, idempotency_key
            )
            if existing_event is not None:
                if _decision_event_matches(existing_event, actor_id, request_sha256):
                    return proposal
                raise EditorialProposalApplicationError("proposal_idempotency_conflict")
            if proposal.patch_sha256 != patch_sha256:
                raise EditorialProposalApplicationError("proposal_patch_hash_mismatch")
            if proposal.state is EditorialProposalState.STALE:
                stale = True
            elif (
                proposal.state is not EditorialProposalState.PROPOSED
                or proposal.state_version != expected_state_version
            ):
                raise EditorialProposalApplicationError("proposal_state_conflict")

            if not stale:
                run = await uow.production_runs.get_for_update(proposal.production_run_id)
                resolution = (
                    await workspace_service.resolve_occurrence_in_uow(
                        uow, subject_id, occurrence_id
                    )
                    if run is not None
                    else None
                )
                pins_match = (
                    resolution is not None
                    and resolution.entry is not None
                    and resolution.binding_state == "valid"
                    and _proposal_pins_match(proposal, resolution)
                )
                if not pins_match:
                    stale_event = _stale_event(proposal)
                    stored_stale, changed = await uow.editorial_proposals.transition_if_current(
                        subject_id=subject_id,
                        occurrence_id=occurrence_id,
                        proposal_id=proposal_id,
                        expected_state=EditorialProposalState.PROPOSED,
                        expected_state_version=proposal.state_version,
                        state=EditorialProposalState.STALE,
                        event=stale_event,
                    )
                    if stored_stale is None:
                        raise EditorialProposalApplicationError("proposal_not_found")
                    if changed:
                        await uow.commit()
                    stale = True

            if not stale:
                rejected = proposal.transition(EditorialProposalState.REJECTED)
                event = EditorialProposalEvent(
                    id=uuid4(),
                    proposal_id=proposal.id,
                    sequence=rejected.state_version,
                    kind=EditorialProposalEventKind.STATE_CHANGED,
                    state=EditorialProposalState.REJECTED,
                    state_version=rejected.state_version,
                    actor_id=actor_id,
                    idempotency_key=idempotency_key,
                    request_sha256=request_sha256,
                    patch_sha256=patch_sha256,
                )
                stored, changed = await uow.editorial_proposals.transition_if_current(
                    subject_id=subject_id,
                    occurrence_id=occurrence_id,
                    proposal_id=proposal_id,
                    expected_state=EditorialProposalState.PROPOSED,
                    expected_state_version=expected_state_version,
                    state=EditorialProposalState.REJECTED,
                    event=event,
                )
                if stored is None:
                    raise EditorialProposalApplicationError("proposal_not_found")
                if not changed:
                    replay = await uow.editorial_proposals.get_event_by_idempotency(
                        proposal_id, idempotency_key
                    )
                    if replay is not None and _decision_event_matches(
                        replay, actor_id, request_sha256
                    ):
                        return stored
                    raise EditorialProposalApplicationError("proposal_state_conflict")
                if stored.thread_id is not None:
                    await uow.editorial_threads.append_event(
                        thread_id=stored.thread_id,
                        kind=EditorialThreadEventKind.REJECTED,
                        actor_id=actor_id,
                        idempotency_key=f"proposal-rejection:{idempotency_key}",
                        request_sha256=request_sha256,
                        proposal_id=stored.id,
                    )
                await uow.commit()
                return stored

        if stale:
            raise EditorialProposalApplicationError("proposal_stale")
        raise EditorialProposalApplicationError("proposal_state_conflict")

    async def cleanup_orphan_blob(self, descriptor: BlobDescriptor) -> bool:
        """Delete only when neither a proposal nor any other row references the blob."""
        if self._blob_store is None:
            raise EditorialProposalApplicationError("proposal_patch_storage_unavailable")
        async with self._uow_factory() as uow:
            await uow.blobs.lock_address(descriptor.logical_bucket, descriptor.sha256)
            blob = await uow.blobs.get_by_address(descriptor.logical_bucket, descriptor.sha256)
            if blob is not None:
                generic_references = await uow.blobs.count_references(blob.id)
                proposal_references = await uow.editorial_proposals.count_patch_references(blob.id)
                if generic_references or proposal_references:
                    return False
                await uow.blobs.delete(blob.id)
            # Hold the same address lock through object deletion and commit. A preparer
            # waiting to register a reference re-reads the object after this critical section.
            await self._blob_store.delete(descriptor)
            await uow.commit()
        return True

    async def _best_effort_orphan_cleanup(self, descriptor: BlobDescriptor) -> None:
        if self._blob_store is None:
            return
        try:
            await self.cleanup_orphan_blob(descriptor)
        except Exception:
            # The original persistence or verification failure remains authoritative.
            return

    def _validate_result(
        self, preparation: EditorialProposalPreparation
    ) -> tuple[
        dict[str, object] | None,
        EditorialProposalState,
        EditorialProposalEventKind,
        dict[str, object],
    ]:
        try:
            patch = parse_wire_patch(preparation.raw_response)
        except TargetedSynthesisRevisionValidationError as error:
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                {"code": error.reason_code},
            )
        if patch.refusal is not None:
            return (
                None,
                EditorialProposalState.REFUSED,
                EditorialProposalEventKind.REFUSED,
                {"code": patch.refusal.code.value},
            )
        try:
            if (
                patch.action is not preparation.operation
                or patch.base_artifact_hash != preparation.base_canonical_sha256
                or patch.before_hash != preparation.before_hash
            ):
                raise EditorialProposalApplicationError("proposal_base_stale")
            if preparation.target.kind is TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT:
                return self._validate_enrichment_result(preparation, patch)
            resolved = resolve_target(
                preparation.base_content, preparation.target, preparation.operation
            )
            if patch.target != resolved.path or resolved.before_hash != preparation.before_hash:
                raise EditorialProposalApplicationError("proposal_target_ambiguous")
            if preparation.operation is not TargetedEditorialRevisionOperation.REWRITE_TITLE:
                raise EditorialProposalApplicationError("proposal_patch_requires_review")
            revised = dict(preparation.base_content)
            revised["title"] = patch.after
            assert_non_target_unchanged(
                preparation.base_content,
                revised,
                preparation.target,
                preparation.operation,
                resolved,
            )
            return (
                {
                    "schema_version": "editorial-patch-v1",
                    "operation": preparation.operation.value,
                    "target": preparation.target.to_json(),
                    "target_id": preparation.target_id,
                    "occurrence_id": str(preparation.occurrence_id),
                    "before_hash": resolved.before_hash,
                    "after": patch.after,
                    "rationale": patch.rationale,
                },
                EditorialProposalState.PROPOSED,
                EditorialProposalEventKind.PREPARED,
                {},
            )
        except EditorialProposalApplicationError as error:
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                {"code": error.code},
            )
        except (TypeError, ValueError) as error:
            code = getattr(error, "reason_code", "proposal_patch_invalid")
            return (
                None,
                EditorialProposalState.NEEDS_REVIEW,
                EditorialProposalEventKind.NEEDS_REVIEW,
                {"code": str(code)},
            )

    def _validate_enrichment_result(
        self, preparation: EditorialProposalPreparation, patch: Any
    ) -> tuple[
        dict[str, object] | None,
        EditorialProposalState,
        EditorialProposalEventKind,
        dict[str, object],
    ]:
        context = preparation.enrichment_context
        if context is None:
            raise EditorialProposalApplicationError("proposal_patch_requires_review")
        request = TargetedEditorialRevisionRequest(
            subject_id=preparation.subject_id,
            base_artifact_id=preparation.base_artifact_id,
            base_version=preparation.base_artifact_version,
            base_input_hash=preparation.base_input_hash,
            base_canonical_sha256=preparation.base_canonical_sha256,
            target=preparation.target,
            operation=preparation.operation,
            instruction=preparation.instruction,
            mode=TargetedEditorialRevisionMode.EXISTING_EVIDENCE_ONLY,
            requester=preparation.requester,
            before_hash=preparation.before_hash,
        )
        validate_targeted_request(request)
        if preparation.target.element_kind is None:
            raise EditorialProposalApplicationError("proposal_patch_invalid")
        validate_targeted_action(
            preparation.target.element_kind, targeted_action(preparation.operation)
        )
        raw = json.loads(patch.after)
        candidate = validate_editorial_enrichment_patch(
            raw,
            element_kind=preparation.target.element_kind,
            element_key=preparation.target.element_key or "new-element",
            evidence_pack=context["evidence_pack"],
            extraction=context["extraction"],
            synthesis=context["synthesis"],
            figure_catalog=tuple(context.get("figure_catalog", ())),
        )
        assert_requested_placement(
            candidate=candidate,
            element_kind=preparation.target.element_kind,
            targeted_request=request,
            synthesis=context["synthesis"],
        )
        return (
            {
                "schema_version": "editorial-patch-v1",
                "operation": preparation.operation.value,
                "target": preparation.target.to_json(),
                "target_id": preparation.target_id,
                "occurrence_id": str(preparation.occurrence_id),
                "before_hash": preparation.before_hash,
                "enrichment_patch": raw,
                "rationale": patch.rationale,
            },
            EditorialProposalState.PROPOSED,
            EditorialProposalEventKind.PREPARED,
            {},
        )

    async def _assert_exact_bridge_origin(
        self, uow: UnitOfWork, preparation: EditorialProposalPreparation
    ) -> None:
        await uow.production_runs.lock_creation_for_subject(preparation.subject_id)
        snapshot = await uow.editorial_workspace_snapshots.get_by_id(preparation.snapshot_id)
        latest_snapshot = await uow.editorial_workspace_snapshots.latest_for_run(
            preparation.production_run_id
        )
        if (
            snapshot is None
            or latest_snapshot is None
            or latest_snapshot.snapshot_id != preparation.snapshot_id
            or snapshot.subject_id != preparation.subject_id
            or snapshot.production_run_id != preparation.production_run_id
            or snapshot.workspace_pin_hash != preparation.base_pin_hash
            or snapshot.routing_decision_hash != preparation.routing_decision_hash
            or snapshot.synthesis_artifact_id != preparation.base_artifact_id
            or snapshot.synthesis_version != preparation.base_artifact_version
            or snapshot.synthesis_input_hash != preparation.base_input_hash
            or snapshot.synthesis_canonical_sha256 != preparation.base_canonical_sha256
        ):
            raise EditorialProposalApplicationError("proposal_base_stale")
        resolution = resolve_occurrence(snapshot, preparation.occurrence_id)
        if (
            resolution.entry is None
            or resolution.binding_state != "valid"
            or resolution.artifact_id != preparation.base_artifact_id
            or resolution.artifact_version != preparation.base_artifact_version
            or resolution.canonical_sha256 != preparation.base_canonical_sha256
        ):
            raise EditorialProposalApplicationError("proposal_base_stale")
        thread_record = await uow.editorial_threads.get(preparation.thread_id)
        if thread_record is None:
            raise EditorialProposalApplicationError("proposal_not_found")
        thread = thread_record.thread
        origin = thread.origin
        if (
            origin.subject_id != preparation.subject_id
            or origin.production_run_id != preparation.production_run_id
            or origin.occurrence_id != preparation.occurrence_id
        ):
            raise EditorialProposalApplicationError("proposal_not_found")
        turn = await uow.model_conversation_turns.get(preparation.model_turn_id)
        model_run = await uow.model_runs.get(preparation.model_run_id)
        if (
            turn is None
            or model_run is None
            or turn.model_run_id != preparation.model_run_id
            or turn.objective != "editorial_proposal"
            or turn.conversation_id != thread.model_conversation_id
            or model_run.id != preparation.model_run_id
            or turn.status is not ConversationTurnStatus.SUCCEEDED
            or model_run.status is not ModelRunStatus.SUCCEEDED
            or turn.output_sha256
            != hashlib.sha256(preparation.raw_response.encode("utf-8")).hexdigest()
        ):
            raise EditorialProposalApplicationError("proposal_model_run_mismatch")


def _replay_matches(
    existing: EditorialProposal,
    preparation: EditorialProposalPreparation,
    patch_sha256: str | None,
) -> bool:
    return (
        existing.request_sha256 == preparation.request_sha256
        and existing.patch_sha256 == patch_sha256
        and existing.production_run_id == preparation.production_run_id
        and existing.snapshot_id == preparation.snapshot_id
        and existing.occurrence_id == preparation.occurrence_id
        and existing.thread_id == preparation.thread_id
        and existing.base_artifact_id == preparation.base_artifact_id
        and existing.base_artifact_version == preparation.base_artifact_version
        and existing.base_input_hash == preparation.base_input_hash
        and existing.base_canonical_sha256 == preparation.base_canonical_sha256
        and existing.base_routing_hash == preparation.routing_decision_hash
        and existing.base_pin_hash == preparation.base_pin_hash
        and existing.target_id == preparation.target_id
        and existing.operation == preparation.operation.value
        and existing.before_hash == preparation.before_hash
        and existing.evidence_refs == preparation.evidence_refs
        and existing.model_run_id == preparation.model_run_id
        and existing.model_turn_id == preparation.model_turn_id
    )


def _proposal_pins_match(
    proposal: EditorialProposal, resolution: WorkspaceOccurrenceResolution
) -> bool:
    record = resolution.record
    return (
        record.workspace_pin_hash == proposal.base_pin_hash
        and record.routing_decision_hash == proposal.base_routing_hash
        and record.synthesis_artifact_id == proposal.base_artifact_id
        and record.synthesis_version == proposal.base_artifact_version
        and record.synthesis_input_hash == proposal.base_input_hash
        and record.synthesis_canonical_sha256 == proposal.base_canonical_sha256
        and resolution.artifact_id == proposal.base_artifact_id
        and resolution.artifact_version == proposal.base_artifact_version
        and resolution.canonical_sha256 == proposal.base_canonical_sha256
    )


def _stale_event(proposal: EditorialProposal) -> EditorialProposalEvent:
    return EditorialProposalEvent(
        id=uuid4(),
        proposal_id=proposal.id,
        sequence=proposal.state_version + 1,
        kind=EditorialProposalEventKind.STATE_CHANGED,
        state=EditorialProposalState.STALE,
        state_version=proposal.state_version + 1,
        actor_id=proposal.actor_id,
        idempotency_key=f"stale-{proposal.id.hex}-{proposal.state_version}",
        request_sha256=_state_request_sha256(
            action="stale",
            proposal_id=proposal.id,
            state_version=proposal.state_version,
            patch_sha256=proposal.patch_sha256,
        ),
        patch_sha256=proposal.patch_sha256,
        diagnostics={"code": "proposal_base_stale"},
    )


def _state_request_sha256(
    *,
    action: str,
    proposal_id: UUID,
    state_version: int,
    patch_sha256: str | None,
) -> str:
    request = {
        "action": action,
        "proposal_id": str(proposal_id),
        "state_version": state_version,
        "patch_sha256": patch_sha256,
    }
    return hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(request)).hexdigest()


def _decision_request_sha256(
    *,
    proposal_id: UUID,
    subject_id: UUID,
    occurrence_id: UUID,
    actor_id: UUID,
    expected_state_version: int,
    patch_sha256: str,
) -> str:
    request = {
        "action": "reject",
        "proposal_id": str(proposal_id),
        "subject_id": str(subject_id),
        "occurrence_id": str(occurrence_id),
        "actor_id": str(actor_id),
        "expected_state_version": expected_state_version,
        "patch_sha256": patch_sha256,
    }
    return hashlib.sha256(ProductionArtifactStore.canonical_json_bytes(request)).hexdigest()


def _decision_event_matches(
    event: EditorialProposalEvent, actor_id: UUID, request_sha256: str
) -> bool:
    return (
        event.kind is EditorialProposalEventKind.STATE_CHANGED
        and event.state is EditorialProposalState.REJECTED
        and event.actor_id == actor_id
        and event.request_sha256 == request_sha256
    )
