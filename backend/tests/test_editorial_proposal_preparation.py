from __future__ import annotations

import hashlib
from dataclasses import replace
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.application.editorial_synthesis.targets import (
    build_targeted_revision_target_catalog,
    resolve_target,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.editorial_workspace import (
    EditorialBlockKind,
    EditorialRevisionStatus,
    EditorialSourceStage,
)
from cti_app.domain.model_conversations import ConversationTurnStatus
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionTarget,
    TargetedEditorialRevisionTargetKind,
)


def _setup(
    *,
    fail_commit: bool = False,
    remove_before_first_address_lock: bool = False,
    remove_before_first_blob_read: bool = False,
):
    from cti_app.application.editorial_proposals import (
        EditorialProposalPreparation,
        EditorialProposalService,
    )

    calls: list[str] = []
    blob_bytes: dict[str, bytes] = {}
    remove_on_address_lock = remove_before_first_address_lock
    remove_on_first_blob_read = remove_before_first_blob_read
    thread_events: list[SimpleNamespace] = []
    canonical_writes: list[object] = []

    class MemoryBlobStore:
        async def put(self, source, *, logical_bucket: str, mime_type: str):
            content = source.read()
            digest = hashlib.sha256(content).hexdigest()
            descriptor = BlobDescriptor(
                sha256=digest,
                size=len(content),
                mime_type=mime_type,
                logical_bucket=logical_bucket,
            )
            blob_bytes[digest] = content
            calls.append("blob_put")
            return descriptor

        async def read(self, descriptor, *, max_bytes: int):
            nonlocal remove_on_first_blob_read
            calls.append("blob_read")
            if remove_on_first_blob_read:
                blob_bytes.pop(descriptor.sha256, None)
                remove_on_first_blob_read = False
            try:
                return blob_bytes[descriptor.sha256]
            except KeyError:
                raise FileNotFoundError from None

        async def delete(self, descriptor):
            if descriptor.sha256 in blob_bytes:
                calls.append("blob_delete")
                blob_bytes.pop(descriptor.sha256, None)

    class MemoryRepository:
        def __init__(self) -> None:
            self.records: list[object] = []
            self.events: list[object] = []
            self.model_runs: list[object] = []

        async def get_by_idempotency_key(self, subject_id, actor_id, idempotency_key):
            return next(
                (
                    record
                    for record in self.records
                    if record.subject_id == subject_id
                    and record.actor_id == actor_id
                    and record.idempotency_key == idempotency_key
                ),
                None,
            )

        async def add_if_absent(self, record, *, event, model_run):
            existing = await self.get_by_idempotency_key(
                record.subject_id, record.actor_id, record.idempotency_key
            )
            if existing is not None:
                return existing, False
            self.records.append(record)
            self.events.append(event)
            if model_run is not None:
                self.model_runs.append(model_run)
            return record, True

        async def get(self, proposal_id):
            return next((item for item in self.records if item.id == proposal_id), None)

        async def get_for_occurrence(self, subject_id, occurrence_id, proposal_id):
            return next(
                (
                    item
                    for item in self.records
                    if item.id == proposal_id
                    and item.subject_id == subject_id
                    and item.occurrence_id == occurrence_id
                ),
                None,
            )

    repository = MemoryRepository()
    blob_records: dict[str, object] = {}
    blob_records_by_id: dict[object, object] = {}
    proposal_snapshot = None
    thread_id = None
    subject_id = None
    run_id = None
    occurrence_id = None
    snapshot_id = None
    artifact_id = None
    model_run_id = None
    model_turn_id = None
    conversation_id = None
    turn_output_sha256 = None

    class MemoryBlobs:
        async def lock_address(self, logical_bucket: str, digest: str):
            nonlocal remove_on_address_lock
            calls.append("blob_address_lock")
            if remove_on_address_lock:
                blob_bytes.pop(digest, None)
                remove_on_address_lock = False

        async def get_by_address(self, logical_bucket: str, digest: str):
            return blob_records.get(f"{logical_bucket}:{digest}")

        async def add_if_absent(self, record):
            key = f"{record.descriptor.logical_bucket}:{record.descriptor.sha256}"
            stored = blob_records.setdefault(key, record)
            blob_records_by_id[stored.id] = stored
            return stored

        async def get(self, blob_id):
            return blob_records_by_id.get(blob_id)

        async def count_references(self, blob_id):
            return 0

        async def delete(self, blob_id):
            stored = blob_records_by_id.pop(blob_id, None)
            if stored is not None:
                blob_records.pop(
                    f"{stored.descriptor.logical_bucket}:{stored.descriptor.sha256}", None
                )

    class MemorySnapshots:
        async def get_by_id(self, requested_snapshot_id):
            if requested_snapshot_id == snapshot_id:
                return proposal_snapshot
            return None

        async def latest_for_run(self, requested_run_id):
            return proposal_snapshot if requested_run_id == run_id else None

    class MemoryThreads:
        async def get(self, requested_thread_id):
            if requested_thread_id != thread_id:
                return None
            return SimpleNamespace(
                thread=SimpleNamespace(
                    origin=SimpleNamespace(
                        subject_id=subject_id,
                        production_run_id=run_id,
                        occurrence_id=occurrence_id,
                    ),
                    model_conversation_id=conversation_id,
                )
            )

        async def append_event(self, **values):
            calls.append("thread_event")
            existing = next(
                (
                    event
                    for event in thread_events
                    if event.idempotency_key == values["idempotency_key"]
                ),
                None,
            )
            if existing is not None:
                if (
                    existing.request_sha256 != values["request_sha256"]
                    or existing.proposal_id != values["proposal_id"]
                ):
                    raise RuntimeError("synthetic thread event idempotency conflict")
                return existing
            event = SimpleNamespace(**values)
            thread_events.append(event)
            return event

    class MemoryTurns:
        async def get(self, requested_turn_id):
            if requested_turn_id != model_turn_id:
                return None
            return SimpleNamespace(
                model_run_id=model_run_id,
                objective="editorial_proposal",
                conversation_id=conversation_id,
                status=ConversationTurnStatus.SUCCEEDED,
                output_sha256=turn_output_sha256,
            )

    class MemoryModelRuns:
        async def get(self, requested_run_id):
            if requested_run_id != model_run_id:
                return None
            return SimpleNamespace(id=model_run_id, status=ModelRunStatus.SUCCEEDED)

    class MemoryProductionRuns:
        async def lock_creation_for_subject(self, _subject_id):
            calls.append("run_creation_lock")

    async def current_occurrence(requested_subject_id, requested_occurrence_id):
        if requested_subject_id != subject_id or requested_occurrence_id != occurrence_id:
            return SimpleNamespace(
                entry=None,
                binding_state="stale",
                record=proposal_snapshot,
            )
        from cti_app.application.editorial_workspace.registry import resolve_occurrence

        return resolve_occurrence(proposal_snapshot, requested_occurrence_id)

    async def resolve_revision_target(**_values):
        occurrence = await current_occurrence(subject_id, occurrence_id)
        return SimpleNamespace(
            record=proposal_snapshot,
            occurrence=occurrence,
            canonical_content=base_content,
            target=target,
            target_id=str(target_entry["id"]),
            before_hash=resolved.before_hash,
        )

    workspace_service = SimpleNamespace(
        resolve_occurrence=current_occurrence,
        resolve_revision_target=resolve_revision_target,
    )

    class MemoryUnitOfWork:
        def __init__(self) -> None:
            self.editorial_proposals = repository
            self.blobs = MemoryBlobs()
            self.editorial_workspace_snapshots = MemorySnapshots()
            self.editorial_threads = MemoryThreads()
            self.model_conversation_turns = MemoryTurns()
            self.model_runs = MemoryModelRuns()
            self.production_runs = MemoryProductionRuns()
            self.production_artifact_writes = canonical_writes
            self._before: (
                tuple[int, int, int, int, dict[str, object], dict[object, object]] | None
            ) = None

        async def __aenter__(self):
            self._before = (
                len(repository.records),
                len(repository.events),
                len(repository.model_runs),
                len(thread_events),
                dict(blob_records),
                dict(blob_records_by_id),
            )
            calls.append("uow_open")
            return self

        async def __aexit__(self, _exc_type, _exc, _traceback):
            if _exc_type is not None:
                await self.rollback()
            calls.append("uow_close")
            return None

        async def commit(self):
            calls.append("uow_commit")
            if fail_commit:
                raise RuntimeError("synthetic relational failure")

        async def rollback(self):
            calls.append("uow_rollback")
            if self._before is None:
                return
            (
                proposal_count,
                event_count,
                model_run_count,
                thread_event_count,
                before_blobs,
                before_ids,
            ) = self._before
            del repository.records[proposal_count:]
            del repository.events[event_count:]
            del repository.model_runs[model_run_count:]
            del thread_events[thread_event_count:]
            blob_records.clear()
            blob_records.update(before_blobs)
            blob_records_by_id.clear()
            blob_records_by_id.update(before_ids)

    def uow_factory():
        return MemoryUnitOfWork()

    service = EditorialProposalService(
        uow_factory=uow_factory,
        blob_store=MemoryBlobStore(),
        workspace_service=workspace_service,
    )

    subject_id = uuid4()
    run_id = uuid4()
    occurrence_id = uuid4()
    thread_id = uuid4()
    snapshot_id = uuid4()
    artifact_id = uuid4()
    actor_id = uuid4()
    model_run_id = uuid4()
    model_turn_id = uuid4()
    conversation_id = uuid4()
    base_content = {
        "schema_version": "2",
        "subject_id": str(subject_id),
        "production_input_hash": "1" * 64,
        "extraction_hash": "2" * 64,
        "publication_language": "fr",
        "synthesis_policy_version": "synthetic-v1",
        "timeline": [],
        "source_notes": [],
        "uncertainties": [],
        "warnings": [],
        "title": "Titre synthétique",
        "lead": [],
        "sections": [],
    }
    target = TargetedEditorialRevisionTarget(kind=TargetedEditorialRevisionTargetKind.TITLE)
    target_entry = build_targeted_revision_target_catalog("synthesis", base_content)[0]
    resolved = resolve_target(
        base_content,
        target,
        TargetedEditorialRevisionOperation.REWRITE_TITLE,
    )
    base_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(base_content)
    ).hexdigest()
    base_pin_hash = "5" * 64
    proposal_snapshot = SimpleNamespace(
        snapshot_id=snapshot_id,
        subject_id=subject_id,
        production_run_id=run_id,
        workspace_pin_hash=base_pin_hash,
        routing_decision_hash="3" * 64,
        synthesis_input_hash="1" * 64,
        block_registry=(
            SimpleNamespace(
                block_id=occurrence_id,
                kind=EditorialBlockKind.TITLE,
                source_stage=EditorialSourceStage.SYNTHESIS,
                revision_status=EditorialRevisionStatus.READY,
            ),
        ),
        synthesis_artifact_id=artifact_id,
        synthesis_version=4,
        synthesis_canonical_sha256=base_sha256,
        enrichment_artifact_id=None,
        enrichment_version=None,
        enrichment_canonical_sha256=None,
    )

    def preparation(raw_response: str | None = None):
        nonlocal turn_output_sha256
        operation = TargetedEditorialRevisionOperation.REWRITE_TITLE
        response = raw_response or "\n".join(
            (
                "@@ TARGET @@",
                resolved.path,
                "@@ ACTION @@",
                operation.value,
                "@@ BASE_ARTIFACT_HASH @@",
                base_sha256,
                "@@ BEFORE_HASH @@",
                resolved.before_hash,
                "@@ AFTER @@",
                "Titre proposé synthétique",
                "@@ RATIONALE @@",
                "Raison synthétique",
                "@@ END @@",
            )
        )
        turn_output_sha256 = hashlib.sha256(response.encode("utf-8")).hexdigest()
        return EditorialProposalPreparation(
            subject_id=subject_id,
            production_run_id=run_id,
            snapshot_id=snapshot_id,
            occurrence_id=occurrence_id,
            thread_id=thread_id,
            base_artifact_id=artifact_id,
            base_artifact_version=4,
            base_input_hash="1" * 64,
            base_canonical_sha256=base_sha256,
            base_pin_hash=base_pin_hash,
            routing_decision_hash="3" * 64,
            target=target,
            target_id=str(target_entry["id"]),
            operation=operation,
            before_hash=resolved.before_hash,
            base_content=base_content,
            raw_response=response,
            model_run_id=model_run_id,
            model_turn_id=model_turn_id,
            actor_id=actor_id,
            idempotency_key=f"proposal_{uuid4().hex}",
            request_sha256="4" * 64,
        )

    return SimpleNamespace(
        service=service,
        preparation=preparation,
        calls=calls,
        blob_bytes=blob_bytes,
        repository=repository,
        thread_events=thread_events,
        canonical_writes=canonical_writes,
        workspace_service=workspace_service,
        subject_id=subject_id,
        occurrence_id=occurrence_id,
        model_run_id=model_run_id,
        model_turn_id=model_turn_id,
        current_snapshot=proposal_snapshot,
    )


@pytest.mark.asyncio
async def test_preparation_rejects_a_supplied_target_that_differs_from_current_occurrence() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    preparation = replace(setup.preparation(), target_id="client-selected-target")

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_bridge_result(preparation)

    assert error.value.code == "proposal_target_mismatch"
    assert setup.blob_bytes == {}
    assert "uow_open" not in setup.calls


@pytest.mark.asyncio
async def test_preparation_rejects_mismatched_target_shape() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    preparation = replace(
        setup.preparation(),
        target=TargetedEditorialRevisionTarget(kind=TargetedEditorialRevisionTargetKind.LEAD),
    )

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_bridge_result(preparation)

    assert error.value.code == "proposal_target_mismatch"
    assert setup.blob_bytes == {}
    assert "uow_open" not in setup.calls


@pytest.mark.asyncio
async def test_preparation_rejects_base_content_that_does_not_match_current_snapshot() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    preparation = setup.preparation()
    changed_content = {**preparation.base_content, "title": "Autre titre synthétique"}

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_bridge_result(
            replace(preparation, base_content=changed_content)
        )

    assert error.value.code == "proposal_base_stale"
    assert setup.blob_bytes == {}
    assert "uow_open" not in setup.calls


@pytest.mark.asyncio
async def test_preparation_refuses_a_snapshot_that_advanced_after_the_bridge_turn() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    setup.current_snapshot.snapshot_id = uuid4()
    setup.current_snapshot.workspace_pin_hash = "8" * 64
    setup.current_snapshot.synthesis_artifact_id = uuid4()
    setup.current_snapshot.synthesis_version = 5
    setup.current_snapshot.synthesis_canonical_sha256 = "9" * 64

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_bridge_result(setup.preparation())

    assert error.value.code == "proposal_base_stale"
    assert setup.blob_bytes == {}
    assert "uow_open" not in setup.calls


@pytest.mark.asyncio
async def test_bridge_patch_becomes_immutable_proposal_with_exact_sha() -> None:
    setup = _setup()
    preparation = setup.preparation()
    proposal = await setup.service.prepare_bridge_result(preparation)
    replay = await setup.service.prepare_bridge_result(preparation)

    stored = setup.blob_bytes[proposal.patch_sha256]
    assert proposal.state == "proposed", proposal.diagnostics
    assert hashlib.sha256(stored).hexdigest() == proposal.patch_sha256
    assert proposal.model_run_id == setup.model_run_id
    assert proposal.model_turn_id == setup.model_turn_id
    assert proposal.patch_blob_ref is not None
    assert replay.id == proposal.id
    assert len(setup.thread_events) == 1
    assert setup.thread_events[0].proposal_id == proposal.id
    assert setup.thread_events[0].model_turn_id == setup.model_turn_id


@pytest.mark.asyncio
async def test_refusal_and_needs_review_do_not_write_canonical_artifact() -> None:
    setup = _setup()
    refusal = "\n".join(
        (
            "@@ REFUSAL @@",
            "code: revision_not_applicable",
            "reason: Données synthétiques insuffisantes",
            "@@ END @@",
        )
    )
    refused = await setup.service.prepare_bridge_result(setup.preparation(refusal))
    needs_review = await setup.service.prepare_bridge_result(
        setup.preparation("Réponse synthétique sans patch typé")
    )

    assert refused.state == "refused"
    assert needs_review.state == "needs_review"
    assert setup.blob_bytes == {}
    assert setup.canonical_writes == []
    assert len(setup.thread_events) == 2


@pytest.mark.asyncio
async def test_proposal_get_projects_persisted_state_and_base_version() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    proposal = await setup.service.prepare_bridge_result(setup.preparation())

    view = await setup.service.inspect(
        setup.subject_id,
        setup.occurrence_id,
        proposal.id,
        workspace_service=setup.workspace_service,
    )

    assert view.state == "proposed"
    assert view.base_artifact_version == 4
    assert view.base_canonical_sha256 == proposal.base_canonical_sha256

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.inspect(
            setup.subject_id,
            uuid4(),
            proposal.id,
            workspace_service=setup.workspace_service,
        )
    assert error.value.code == "proposal_not_found"


@pytest.mark.asyncio
async def test_proposal_get_projects_stale_when_current_pins_advance() -> None:
    setup = _setup()
    proposal = await setup.service.prepare_bridge_result(setup.preparation())
    setup.current_snapshot.workspace_pin_hash = "8" * 64
    setup.current_snapshot.synthesis_artifact_id = uuid4()
    setup.current_snapshot.synthesis_version = 5
    setup.current_snapshot.synthesis_input_hash = "a" * 64
    setup.current_snapshot.synthesis_canonical_sha256 = "9" * 64

    view = await setup.service.inspect(
        setup.subject_id,
        setup.occurrence_id,
        proposal.id,
        workspace_service=setup.workspace_service,
    )

    assert view.state == "stale"
    assert view.patch is not None
    assert setup.repository.records[0].state.value == "proposed"


@pytest.mark.asyncio
async def test_proposal_get_detects_patch_blob_altered_after_persistence() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    proposal = await setup.service.prepare_bridge_result(setup.preparation())
    assert proposal.patch_sha256 is not None
    setup.blob_bytes[proposal.patch_sha256] = b"altered synthetic patch"

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.inspect(
            setup.subject_id,
            setup.occurrence_id,
            proposal.id,
            workspace_service=setup.workspace_service,
        )

    assert error.value.code == "proposal_patch_hash_mismatch"


@pytest.mark.asyncio
async def test_get_non_current_proposal_returns_neutral_detail_without_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.api import editorial_workspace as editorial_workspace_api

    subject_id = uuid4()
    occurrence_id = uuid4()
    proposal_id = uuid4()
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    app.state.uow_factory = lambda: None
    app.state.production_artifact_store = object()

    async def unresolved_occurrence(_subject_id, _occurrence_id):
        return SimpleNamespace(entry=None, binding_state="stale", record=None)

    monkeypatch.setattr(
        editorial_workspace_api,
        "_service",
        lambda _request: SimpleNamespace(resolve_occurrence=unresolved_occurrence),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.get(
            f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/{proposal_id}"
        )

    assert response.status_code == 404
    assert response.json() == {
        "detail": {"code": "proposal_not_found", "message": "Proposition introuvable."}
    }
    assert "patch" not in response.json()


@pytest.mark.asyncio
async def test_blob_verified_before_uow_and_orphan_cleanup_idempotent() -> None:
    setup = _setup(fail_commit=True)
    with pytest.raises(RuntimeError, match="synthetic relational failure"):
        await setup.service.prepare_bridge_result(setup.preparation())

    assert setup.calls.index("blob_put") < setup.calls.index("blob_read")
    assert setup.calls.index("blob_read") < setup.calls.index("uow_open")
    assert setup.calls.count("blob_delete") == 1

    cleanup = _setup()
    orphan_bytes = b"synthetic orphan"
    orphan_sha256 = hashlib.sha256(orphan_bytes).hexdigest()
    cleanup.blob_bytes[orphan_sha256] = orphan_bytes
    descriptor = BlobDescriptor(
        sha256=orphan_sha256,
        size=len(orphan_bytes),
        mime_type="application/vnd.autowork.editorial-patch+json",
        logical_bucket="editorial-proposal-patches",
    )
    await cleanup.service.cleanup_orphan_blob(descriptor)
    await cleanup.service.cleanup_orphan_blob(descriptor)
    assert cleanup.calls.count("blob_delete") == 1
    assert orphan_sha256 not in cleanup.blob_bytes


@pytest.mark.asyncio
async def test_missing_patch_under_address_lock_reuploads_after_uow_exit() -> None:
    setup = _setup(remove_before_first_address_lock=True)

    proposal = await setup.service.prepare_bridge_result(setup.preparation())

    assert proposal.patch_sha256 is not None
    assert setup.calls.count("blob_put") == 2
    first_uow_close = setup.calls.index("uow_close")
    retry_put = setup.calls.index("blob_put", setup.calls.index("blob_address_lock") + 1)
    assert first_uow_close < retry_put
    assert setup.calls.index("blob_put") < setup.calls.index("uow_open")
    assert setup.calls.count("blob_address_lock") == 2
    assert len(setup.thread_events) == 1


@pytest.mark.asyncio
async def test_missing_patch_during_initial_readback_reuploads_before_uow() -> None:
    setup = _setup(remove_before_first_blob_read=True)

    proposal = await setup.service.prepare_bridge_result(setup.preparation())

    assert proposal.patch_sha256 is not None
    assert setup.calls.count("blob_put") == 2
    reads = [index for index, call in enumerate(setup.calls) if call == "blob_read"]
    assert reads[1] < setup.calls.index("uow_open")
    assert setup.calls.count("blob_address_lock") == 1
