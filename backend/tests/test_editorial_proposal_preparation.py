from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

if TYPE_CHECKING:
    from cti_app.application.editorial_proposals import EditorialProposalPreparation

from cti_app.application.editorial_synthesis.targets import (
    build_targeted_revision_target_catalog,
    resolve_target,
)
from cti_app.application.editorial_workspace.registry import (
    resolve_occurrence,
    resolve_workspace_target,
)
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_proposal import EditorialProposalOrigin
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialBlockKind,
    EditorialRevisionStatus,
    EditorialSourceStage,
    bootstrap_block_registry,
)
from cti_app.domain.model_conversations import ConversationTurnStatus
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentElementKind
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
    TargetedEditorialRevisionPlacement,
    TargetedEditorialRevisionPlacementKind,
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

        async def lock_adoption_for_run(self, production_run_id):
            calls.append(("adoption_guard", production_run_id))

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

        async def get_for_occurrence_for_update(self, subject_id, occurrence_id, proposal_id):
            calls.append("proposal_lock")
            return await self.get_for_occurrence(subject_id, occurrence_id, proposal_id)

        async def get_event_by_idempotency(self, proposal_id, idempotency_key):
            return next(
                (
                    event
                    for event in self.events
                    if event.proposal_id == proposal_id and event.idempotency_key == idempotency_key
                ),
                None,
            )

        async def transition_if_current(
            self,
            *,
            subject_id,
            occurrence_id,
            proposal_id,
            expected_state,
            expected_state_version,
            state,
            event,
        ):
            record = await self.get_for_occurrence(subject_id, occurrence_id, proposal_id)
            if (
                record is None
                or record.state is not expected_state
                or record.state_version != expected_state_version
            ):
                return record, False
            transitioned = record.transition(state, now=event.created_at)
            self.records[self.records.index(record)] = transitioned
            self.events.append(event)
            return transitioned, True

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

        async def get_for_update(self, _run_id):
            calls.append("run_lock")
            return SimpleNamespace(id=run_id)

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

    async def resolve_occurrence_in_uow(_uow, requested_subject_id, requested_occurrence_id):
        return await current_occurrence(requested_subject_id, requested_occurrence_id)

    workspace_service = SimpleNamespace(
        resolve_occurrence=current_occurrence,
        resolve_occurrence_in_uow=resolve_occurrence_in_uow,
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
        blob_records=blob_records,
        blob_records_by_id=blob_records_by_id,
        repository=repository,
        thread_events=thread_events,
        canonical_writes=canonical_writes,
        workspace_service=workspace_service,
        subject_id=subject_id,
        occurrence_id=occurrence_id,
        actor_id=actor_id,
        model_run_id=model_run_id,
        model_turn_id=model_turn_id,
        current_snapshot=proposal_snapshot,
    )


def _install_real_workspace_resolver(setup):
    preparation = setup.preparation()
    content = dict(preparation.base_content)
    content["sections"] = [
        {
            "heading": "Section synthétique",
            "paragraphs": [
                {"text": "Premier paragraphe synthétique"},
                {"text": "Deuxième paragraphe synthétique"},
            ],
        }
    ]
    canonical_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(content)
    ).hexdigest()
    entries = list(
        bootstrap_block_registry(
            artifact_id=preparation.base_artifact_id,
            canonical_sha256=canonical_sha256,
            source_stage="synthesis",
            canonical_content=content,
        )
    )
    entries[0] = replace(entries[0], block_id=setup.occurrence_id)
    setup.current_snapshot.block_registry = tuple(entries)
    setup.current_snapshot.synthesis_canonical_sha256 = canonical_sha256
    setup.current_snapshot.publication_kind = "article"

    class StaticWorkspaceService(EditorialWorkspaceService):
        async def _load_current(self, subject_id):
            return {}, setup.current_snapshot, content

    setup.service._workspace_service = StaticWorkspaceService(
        uow_factory=None,
        artifact_store=None,
    )
    return (
        replace(
            setup.preparation(),
            base_content=content,
            base_canonical_sha256=canonical_sha256,
        ),
        tuple(entries),
        content,
    )


def _paragraph_wire_response(
    *,
    target_path: str,
    operation: str,
    base_sha256: str,
    before_hash: str,
    after: str,
) -> str:
    return "\n".join(
        (
            "@@ TARGET @@",
            target_path,
            "@@ ACTION @@",
            operation,
            "@@ BASE_ARTIFACT_HASH @@",
            base_sha256,
            "@@ BEFORE_HASH @@",
            before_hash,
            "@@ AFTER @@",
            after,
            "@@ RATIONALE @@",
            "Raison synthétique.",
            "@@ END @@",
        )
    )


def _install_paragraph_proposal_context(setup):
    from cti_app.application.editorial_conversation_turns import (
        EditorialParagraphEvidenceContext,
    )
    from cti_app.application.production_synthesis import (
        build_synthesis_evidence_pack,
        canonical_extraction_hash,
    )
    from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage
    from cti_app.domain.production_synthesis import (
        SYNTHESIS_POLICY_VERSION,
        extraction_evidence_elements,
    )
    from tests.test_production_synthesis_application import (
        make_extraction,
        make_fact,
        make_snapshot,
        make_source,
    )

    base = setup.preparation()
    source_id = uuid4()
    fact = make_fact(source_id, "FooRAT")
    extraction = make_extraction(
        setup.subject_id,
        (make_source(source_id, facts=(fact,)),),
    )
    snapshot = make_snapshot(setup.subject_id)
    extraction = replace(extraction, production_input_hash=snapshot.input_hash)
    evidence_pack = build_synthesis_evidence_pack(snapshot, extraction)
    evidence_ref, _payload = extraction_evidence_elements(extraction)[0]
    evidence_handle = evidence_pack.handle_for(evidence_ref)
    assert evidence_handle is not None

    paragraph = {
        "text": "FooRAT appears in the report.",
        "evidence_refs": [
            {
                "source_document_id": str(evidence_ref.source_document_id),
                "kind": evidence_ref.kind.value,
                "evidence_key": evidence_ref.evidence_key,
            }
        ],
    }
    base_content = dict(base.base_content)
    base_content["schema_version"] = 2
    base_content["synthesis_policy_version"] = SYNTHESIS_POLICY_VERSION
    base_content["production_input_hash"] = snapshot.input_hash
    base_content["extraction_hash"] = canonical_extraction_hash(extraction)
    base_content["lead"] = [paragraph]
    canonical_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(base_content)
    ).hexdigest()
    entries = list(
        bootstrap_block_registry(
            artifact_id=base.base_artifact_id,
            canonical_sha256=canonical_sha256,
            source_stage="synthesis",
            canonical_content=base_content,
        )
    )
    paragraph_entry = next(
        entry for entry in entries if entry.kind is EditorialBlockKind.LEAD_PARAGRAPH
    )
    entries[entries.index(paragraph_entry)] = replace(paragraph_entry, block_id=setup.occurrence_id)
    setup.current_snapshot.block_registry = tuple(entries)
    setup.current_snapshot.synthesis_canonical_sha256 = canonical_sha256
    setup.current_snapshot.publication_kind = "brief"

    class StaticWorkspaceService(EditorialWorkspaceService):
        async def _load_current(self, subject_id):
            return {}, setup.current_snapshot, base_content

    setup.service._workspace_service = StaticWorkspaceService(
        uow_factory=None,
        artifact_store=None,
    )
    target_resolution = resolve_workspace_target(
        entry=next(entry for entry in entries if entry.block_id == setup.occurrence_id),
        canonical_content=base_content,
        operation="rewrite_paragraph",
    )
    artifact = ProductionArtifact(
        production_run_id=setup.current_snapshot.production_run_id,
        subject_id=setup.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=base.base_artifact_version,
        input_hash=base.base_input_hash,
        canonical_blob_id=uuid4(),
        id=base.base_artifact_id,
    )
    context = EditorialParagraphEvidenceContext(
        evidence_pack=evidence_pack,
        extraction=extraction,
        snapshot=snapshot,
        base_artifact=artifact,
        evidence_refs=(evidence_ref,),
        evidence_handles=(evidence_handle,),
        publication_kind=None,
    )
    preparation = replace(
        base,
        target=target_resolution.target,
        target_id=target_resolution.legacy_target_id,
        operation=TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
        before_hash=target_resolution.before_hash,
        base_content=base_content,
        base_canonical_sha256=canonical_sha256,
        paragraph_evidence_context=context,
        strict_wire=True,
    )
    return preparation, evidence_handle


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
async def test_preparation_rejects_target_from_another_real_block_without_writing() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    preparation, entries, content = _install_real_workspace_resolver(setup)
    paragraphs = [entry for entry in entries if entry.kind is EditorialBlockKind.PARAGRAPH]
    assert len(paragraphs) == 2
    other_target = resolve_workspace_target(
        entry=paragraphs[1],
        canonical_content=content,
        operation="rewrite_paragraph",
    )
    assert other_target.legacy_target_id is not None
    forged = replace(
        preparation,
        occurrence_id=paragraphs[0].block_id,
        operation=TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
        target=other_target.target,
        target_id=other_target.legacy_target_id,
        before_hash=other_target.before_hash,
    )

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_bridge_result(forged)

    assert error.value.code == "proposal_target_mismatch"
    assert setup.blob_bytes == {}
    assert "uow_open" not in setup.calls


@pytest.mark.asyncio
async def test_preparation_rejects_occurrence_from_another_subject_without_writing() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    preparation, _entries, _content = _install_real_workspace_resolver(setup)

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_bridge_result(replace(preparation, subject_id=uuid4()))

    assert error.value.code == "proposal_base_stale"
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
async def test_preparation_revalidates_pins_under_run_lock_before_inserting() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    original_read = setup.service._blob_store.read

    async def read_then_advance(descriptor, *, max_bytes):
        content = await original_read(descriptor, max_bytes=max_bytes)
        setup.current_snapshot.workspace_pin_hash = "8" * 64
        setup.current_snapshot.synthesis_canonical_sha256 = "9" * 64
        return content

    setup.service._blob_store.read = read_then_advance

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.prepare_bridge_result(setup.preparation())

    assert error.value.code == "proposal_base_stale"
    assert setup.repository.records == []
    assert setup.repository.events == []
    assert setup.thread_events == []
    assert setup.calls.index(("adoption_guard", setup.preparation().production_run_id)) < (
        setup.calls.index("blob_address_lock")
    )


@pytest.mark.asyncio
async def test_bridge_patch_becomes_immutable_proposal_with_exact_sha() -> None:
    setup = _setup()
    preparation = replace(setup.preparation(), strict_wire=True)
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
    refused = await setup.service.prepare_bridge_result(
        replace(setup.preparation(refusal), strict_wire=True)
    )
    needs_review = await setup.service.prepare_bridge_result(
        replace(
            setup.preparation("Texte libre avant le patch. " + setup.preparation().raw_response),
            strict_wire=True,
        )
    )
    valid_wire = setup.preparation().raw_response
    markup_response = valid_wire.replace("Titre proposé synthétique", "<svg>libre</svg>")
    markup = await setup.service.prepare_bridge_result(
        replace(setup.preparation(markup_response), strict_wire=True)
    )

    assert refused.state == "refused"
    assert needs_review.state == "needs_review"
    assert markup.state == "needs_review"
    assert setup.blob_bytes == {}
    assert setup.canonical_writes == []
    assert len(setup.thread_events) == 3


@pytest.mark.parametrize(
    "freeform_text",
    [
        '#link("https://example.invalid", [x])',
        '#raw("<script>run()</script>")',
        '#import "local.typ"',
        '#read("/etc/passwd")',
        '#include "local.typ"',
        "<script>alert(1)</script>",
        '<svg><a href="javascript:alert(1)">x</a></svg>',
    ],
)
@pytest.mark.asyncio
async def test_strict_proposal_text_rejects_typst_html_and_svg_markup(
    freeform_text: str,
) -> None:
    setup = _setup()
    valid_wire = setup.preparation().raw_response
    response = valid_wire.replace("Titre proposé synthétique", freeform_text)

    proposal = await setup.service.prepare_bridge_result(
        replace(setup.preparation(response), strict_wire=True)
    )

    assert proposal.state == "needs_review"
    assert proposal.diagnostics["code"] == "revision_wire_markup_forbidden"
    assert setup.blob_bytes == {}
    assert setup.canonical_writes == []


@pytest.mark.asyncio
async def test_rewrite_paragraph_patch_is_prepared_with_exact_patch_sha() -> None:
    setup = _setup()
    preparation, handle = _install_paragraph_proposal_context(setup)
    paragraph = preparation.base_content["lead"][0]
    assert isinstance(paragraph, dict)
    resolved = resolve_target(
        preparation.base_content,
        preparation.target,
        TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
    )
    raw_response = _paragraph_wire_response(
        target_path=resolved.path,
        operation="rewrite_paragraph",
        base_sha256=preparation.base_canonical_sha256,
        before_hash=resolved.before_hash,
        after=(
            f"@@LEAD@@\n@@CLAIM L001@@\nEVIDENCE: {handle}\nTEXT: The report identifies FooRAT.\n"
        ),
    )
    setup.preparation(raw_response)
    proposal = await setup.service.prepare_bridge_result(
        replace(preparation, raw_response=raw_response)
    )

    assert proposal.state.value == "proposed", proposal.diagnostics
    assert proposal.patch_sha256 is not None
    encoded = setup.blob_bytes[proposal.patch_sha256]
    assert hashlib.sha256(encoded).hexdigest() == proposal.patch_sha256
    assert json.loads(encoded)["after"] == (
        f"@@LEAD@@\n@@CLAIM L001@@\nEVIDENCE: {handle}\nTEXT: The report identifies FooRAT."
    )


@pytest.mark.asyncio
async def test_rewrite_paragraph_stale_fence_and_unsupported_fact_write_nothing() -> None:
    setup = _setup()
    stale, handle = _install_paragraph_proposal_context(setup)
    resolved = resolve_target(
        stale.base_content,
        stale.target,
        TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
    )
    stale = replace(
        stale,
        raw_response=_paragraph_wire_response(
            target_path=resolved.path,
            operation="rewrite_paragraph",
            base_sha256=stale.base_canonical_sha256,
            before_hash="f" * 64,
            after=(
                f"@@LEAD@@\n@@CLAIM L001@@\nEVIDENCE: {handle}\n"
                "TEXT: The report identifies FooRAT.\n"
            ),
        ),
    )

    _stale_patch, stale_state, _kind, stale_diagnostics = await setup.service._validate_result(
        stale
    )

    assert stale_state.value == "needs_review"
    assert stale_diagnostics["code"] == "proposal_base_stale"
    assert setup.canonical_writes == []

    unsupported = replace(
        stale,
        raw_response=_paragraph_wire_response(
            target_path=resolved.path,
            operation="rewrite_paragraph",
            base_sha256=stale.base_canonical_sha256,
            before_hash=resolved.before_hash,
            after=(
                f"@@LEAD@@\n@@CLAIM L001@@\nEVIDENCE: {handle}\n"
                "TEXT: UnsupportedFamilyX controlled the infected environment.\n"
            ),
        ),
    )

    (
        _unsupported_patch,
        unsupported_state,
        _kind,
        diagnostics,
    ) = await setup.service._validate_result(unsupported)

    assert unsupported_state.value == "needs_review"
    assert diagnostics["code"] == "synthesis_relation_not_evidenced", diagnostics
    assert setup.canonical_writes == []


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
async def test_changed_pins_project_proposal_stale_and_persist_before_any_decision() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

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
    assert setup.repository.records[0].state.value == "stale"
    assert setup.repository.records[0].state_version == proposal.state_version + 1
    assert setup.repository.events[-1].state.value == "stale"
    assert setup.repository.events[-1].state_version == proposal.state_version + 1
    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.reject(
            setup.subject_id,
            setup.occurrence_id,
            proposal.id,
            actor_id=setup.actor_id,
            expected_state_version=proposal.state_version,
            patch_sha256=proposal.patch_sha256,
            idempotency_key=f"reject_{uuid4().hex}",
            workspace_service=setup.workspace_service,
        )
    assert error.value.code == "proposal_stale"
    assert setup.repository.records[0].state.value == "stale"
    assert len(setup.repository.events) == 2


@pytest.mark.asyncio
async def test_reject_is_terminal_and_keeps_artifact_pins() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    proposal = await setup.service.prepare_bridge_result(setup.preparation())
    original_pins = (
        setup.current_snapshot.workspace_pin_hash,
        setup.current_snapshot.synthesis_artifact_id,
        setup.current_snapshot.synthesis_version,
        setup.current_snapshot.synthesis_canonical_sha256,
    )
    idempotency_key = f"reject_{uuid4().hex}"

    rejected = await setup.service.reject(
        setup.subject_id,
        setup.occurrence_id,
        proposal.id,
        actor_id=setup.actor_id,
        expected_state_version=proposal.state_version,
        patch_sha256=proposal.patch_sha256,
        idempotency_key=idempotency_key,
        workspace_service=setup.workspace_service,
    )
    replay = await setup.service.reject(
        setup.subject_id,
        setup.occurrence_id,
        proposal.id,
        actor_id=setup.actor_id,
        expected_state_version=proposal.state_version,
        patch_sha256=proposal.patch_sha256,
        idempotency_key=idempotency_key,
        workspace_service=setup.workspace_service,
    )

    assert rejected.state.value == "rejected"
    assert rejected.state_version == proposal.state_version + 1
    assert replay == rejected
    assert setup.repository.records[0].state.value == "rejected"
    assert setup.repository.events[-1].state.value == "rejected"
    assert len(setup.repository.events) == 2
    assert len(setup.repository.model_runs) == 1
    assert setup.canonical_writes == []
    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.reject(
            setup.subject_id,
            setup.occurrence_id,
            proposal.id,
            actor_id=setup.actor_id,
            expected_state_version=rejected.state_version,
            patch_sha256=proposal.patch_sha256,
            idempotency_key=f"reject_{uuid4().hex}",
            workspace_service=setup.workspace_service,
        )
    assert getattr(error.value, "code", None) == "proposal_state_conflict"
    assert original_pins == (
        setup.current_snapshot.workspace_pin_hash,
        setup.current_snapshot.synthesis_artifact_id,
        setup.current_snapshot.synthesis_version,
        setup.current_snapshot.synthesis_canonical_sha256,
    )


@pytest.mark.asyncio
async def test_reject_revalidates_pins_under_run_lock_after_inspect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    proposal = await setup.service.prepare_bridge_result(setup.preparation())
    idempotency_key = f"reject_{uuid4().hex}"
    original_inspect = setup.service.inspect

    async def inspect_then_advance_pins(*args, **kwargs):
        view = await original_inspect(*args, **kwargs)
        setup.current_snapshot.workspace_pin_hash = "8" * 64
        return view

    monkeypatch.setattr(setup.service, "inspect", inspect_then_advance_pins)

    with pytest.raises(EditorialProposalApplicationError) as error:
        await setup.service.reject(
            setup.subject_id,
            setup.occurrence_id,
            proposal.id,
            actor_id=setup.actor_id,
            expected_state_version=proposal.state_version,
            patch_sha256=proposal.patch_sha256,
            idempotency_key=idempotency_key,
            workspace_service=setup.workspace_service,
        )

    assert error.value.code == "proposal_stale"
    assert setup.repository.records[0].state.value == "stale"
    assert setup.repository.records[0].state_version == proposal.state_version + 1
    assert setup.repository.events[-1].state.value == "stale"
    assert setup.repository.events[-1].idempotency_key == (
        f"stale-{proposal.id.hex}-{proposal.state_version}"
    )
    assert setup.calls.index("proposal_lock") < setup.calls.index("run_lock")
    assert setup.repository.records[0].state.value != "rejected"

    event_count = len(setup.repository.events)
    with pytest.raises(EditorialProposalApplicationError) as replay_error:
        await setup.service.reject(
            setup.subject_id,
            setup.occurrence_id,
            proposal.id,
            actor_id=setup.actor_id,
            expected_state_version=proposal.state_version,
            patch_sha256=proposal.patch_sha256,
            idempotency_key=idempotency_key,
            workspace_service=setup.workspace_service,
        )
    assert replay_error.value.code == "proposal_stale"
    assert len(setup.repository.events) == event_count


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
    assert setup.calls.count("blob_delete") == 0
    assert len(setup.blob_bytes) == 1

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
    orphan = BlobRecord(descriptor=descriptor)
    cleanup.blob_records[f"{descriptor.logical_bucket}:{descriptor.sha256}"] = orphan
    cleanup.blob_records_by_id[orphan.id] = orphan

    assert await cleanup.service.cleanup_orphan_blob(descriptor)
    assert not await cleanup.service.cleanup_orphan_blob(descriptor)
    assert cleanup.calls.count("blob_delete") == 0
    assert cleanup.blob_bytes[orphan_sha256] == orphan_bytes


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


def _enrichment_workspace_record() -> SimpleNamespace:
    subject_id, run_id, snapshot_id = uuid4(), uuid4(), uuid4()
    synthesis_artifact_id, enrichment_artifact_id = uuid4(), uuid4()
    synthesis_content = {
        "title": "Titre synthétique",
        "lead": [{"text": "Chapeau synthétique"}],
        "sections": [],
    }
    enrichment_content = {"tables": [{"key": "table-a", "title": "Table A"}]}
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_content)
    ).hexdigest()
    enrichment_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(enrichment_content)
    ).hexdigest()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=synthesis_artifact_id,
        canonical_sha256=synthesis_sha256,
        source_stage="synthesis",
        canonical_content=synthesis_content,
    )
    enrichment_entries = bootstrap_block_registry(
        artifact_id=enrichment_artifact_id,
        canonical_sha256=enrichment_sha256,
        source_stage="editorial_enrichment",
        canonical_content=enrichment_content,
    )
    return SimpleNamespace(
        subject_id=subject_id,
        production_run_id=run_id,
        snapshot_id=snapshot_id,
        workspace_pin_hash="5" * 64,
        routing_decision_hash="3" * 64,
        synthesis_artifact_id=synthesis_artifact_id,
        synthesis_version=4,
        synthesis_input_hash="1" * 64,
        synthesis_canonical_sha256=synthesis_sha256,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_version=2,
        enrichment_input_hash="2" * 64,
        enrichment_canonical_sha256=enrichment_sha256,
        block_registry=(*synthesis_entries, *enrichment_entries),
        publication_kind="article",
    )


def _pinned_proposal(
    record: SimpleNamespace,
    *,
    operation: TargetedEditorialRevisionOperation,
    before_hash: str,
) -> SimpleNamespace:
    return SimpleNamespace(
        subject_id=record.subject_id,
        production_run_id=record.production_run_id,
        snapshot_id=record.snapshot_id,
        base_artifact_id=record.enrichment_artifact_id,
        base_artifact_version=record.enrichment_version,
        base_input_hash=record.enrichment_input_hash,
        base_canonical_sha256=record.enrichment_canonical_sha256,
        base_pin_hash=record.workspace_pin_hash,
        base_routing_hash=record.routing_decision_hash,
        operation=operation.value,
        before_hash=before_hash,
    )


def test_proposal_pins_match_all_non_create_enrichment_element_operations() -> None:
    from cti_app.application import editorial_proposals as editorial_proposals_module

    record = _enrichment_workspace_record()
    table_entry = next(
        entry
        for entry in record.block_registry
        if entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
        and entry.kind is EditorialBlockKind.TABLE
    )
    resolution = resolve_occurrence(record, table_entry.block_id)
    non_create_operations = (
        TargetedEditorialRevisionOperation.REWRITE_TABLE,
        TargetedEditorialRevisionOperation.DELETE_TABLE,
        TargetedEditorialRevisionOperation.REWRITE_DIAGRAM,
        TargetedEditorialRevisionOperation.DELETE_DIAGRAM,
        TargetedEditorialRevisionOperation.REWRITE_CHART,
        TargetedEditorialRevisionOperation.DELETE_CHART,
        TargetedEditorialRevisionOperation.REWRITE_SOURCE_FIGURE,
        TargetedEditorialRevisionOperation.REPLACE_SOURCE_FIGURE,
        TargetedEditorialRevisionOperation.DELETE_SOURCE_FIGURE,
        TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION,
        TargetedEditorialRevisionOperation.MOVE_ENRICHMENT_ELEMENT,
    )

    for operation in non_create_operations:
        proposal = _pinned_proposal(
            record, operation=operation, before_hash=table_entry.content_sha256
        )
        assert editorial_proposals_module._proposal_pins_match(proposal, resolution), operation

    stale_proposal = _pinned_proposal(
        record,
        operation=TargetedEditorialRevisionOperation.UPDATE_VISUAL_CAPTION,
        before_hash=table_entry.content_sha256,
    )
    record.enrichment_canonical_sha256 = "f" * 64
    stale_resolution = resolve_occurrence(record, table_entry.block_id)
    assert not editorial_proposals_module._proposal_pins_match(stale_proposal, stale_resolution)


def test_proposal_pins_match_reorder_uses_synthesis_anchor_and_enrichment_base() -> None:
    from cti_app.application import editorial_proposals as editorial_proposals_module

    record = _enrichment_workspace_record()
    paragraph_entry = next(
        entry
        for entry in record.block_registry
        if entry.source_stage is EditorialSourceStage.SYNTHESIS
        and entry.kind is EditorialBlockKind.LEAD_PARAGRAPH
    )
    proposal = _pinned_proposal(
        record,
        operation=TargetedEditorialRevisionOperation.REORDER_ENRICHMENT_ELEMENTS,
        before_hash="9" * 64,
    )

    assert editorial_proposals_module._proposal_pins_match(
        proposal, resolve_occurrence(record, paragraph_entry.block_id)
    )

    enrichment_entry = next(
        entry
        for entry in record.block_registry
        if entry.source_stage is EditorialSourceStage.EDITORIAL_ENRICHMENT
        and entry.kind is EditorialBlockKind.TABLE
    )
    assert not editorial_proposals_module._proposal_pins_match(
        proposal, resolve_occurrence(record, enrichment_entry.block_id)
    )

    record.workspace_pin_hash = "e" * 64
    assert not editorial_proposals_module._proposal_pins_match(
        proposal, resolve_occurrence(record, paragraph_entry.block_id)
    )


def _install_dual_stage_registry(
    setup: SimpleNamespace,
) -> tuple[BlockRegistryEntry, BlockRegistryEntry, UUID, str]:
    record = setup.current_snapshot
    synthesis_content = {
        "title": "Titre synthétique",
        "lead": [{"text": "Chapeau synthétique"}],
        "sections": [],
    }
    enrichment_content = {"tables": [{"key": "table-a", "title": "Table A"}]}
    synthesis_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(synthesis_content)
    ).hexdigest()
    enrichment_sha256 = hashlib.sha256(
        ProductionArtifactStore.canonical_json_bytes(enrichment_content)
    ).hexdigest()
    enrichment_artifact_id = uuid4()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=record.synthesis_artifact_id,
        canonical_sha256=synthesis_sha256,
        source_stage="synthesis",
        canonical_content=synthesis_content,
    )
    enrichment_entries = bootstrap_block_registry(
        artifact_id=enrichment_artifact_id,
        canonical_sha256=enrichment_sha256,
        source_stage="editorial_enrichment",
        canonical_content=enrichment_content,
    )
    record.block_registry = (*synthesis_entries, *enrichment_entries)
    record.synthesis_canonical_sha256 = synthesis_sha256
    record.enrichment_artifact_id = enrichment_artifact_id
    record.enrichment_version = 2
    record.enrichment_input_hash = "2" * 64
    record.enrichment_canonical_sha256 = enrichment_sha256
    paragraph_entry = next(
        entry for entry in synthesis_entries if entry.kind is EditorialBlockKind.LEAD_PARAGRAPH
    )
    table_entry = next(
        entry for entry in enrichment_entries if entry.kind is EditorialBlockKind.TABLE
    )
    return paragraph_entry, table_entry, enrichment_artifact_id, enrichment_sha256


def _manual_enrichment_preparation(
    setup: SimpleNamespace,
    *,
    occurrence_id: UUID,
    target: TargetedEditorialRevisionTarget,
    operation: TargetedEditorialRevisionOperation,
    before_hash: str,
    enrichment_artifact_id: UUID,
    enrichment_sha256: str,
) -> EditorialProposalPreparation:
    return cast(
        "EditorialProposalPreparation",
        replace(
            setup.preparation(),
            occurrence_id=occurrence_id,
            base_artifact_id=enrichment_artifact_id,
            base_artifact_version=2,
            base_input_hash="2" * 64,
            base_canonical_sha256=enrichment_sha256,
            target=target,
            operation=operation,
            before_hash=before_hash,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            thread_id=None,
            model_run_id=None,
            model_turn_id=None,
            manual_patch=SimpleNamespace(),
        ),
    )


@pytest.mark.asyncio
async def test_exact_origin_accepts_a_move_on_an_existing_enrichment_element() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    _paragraph_entry, table_entry, enrichment_artifact_id, enrichment_sha256 = (
        _install_dual_stage_registry(setup)
    )
    preparation = _manual_enrichment_preparation(
        setup,
        occurrence_id=table_entry.block_id,
        target=TargetedEditorialRevisionTarget(
            kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ELEMENT,
            element_kind=EditorialEnrichmentElementKind.TABLE,
            element_key="table-a",
        ),
        operation=TargetedEditorialRevisionOperation.MOVE_ENRICHMENT_ELEMENT,
        before_hash=table_entry.content_sha256,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_sha256=enrichment_sha256,
    )

    async with setup.service._uow_factory() as uow:
        await setup.service._assert_exact_origin(uow, preparation)

    stale = replace(preparation, base_canonical_sha256="f" * 64)
    with pytest.raises(EditorialProposalApplicationError) as exc_info:
        async with setup.service._uow_factory() as stale_uow:
            await setup.service._assert_exact_origin(stale_uow, stale)
    assert exc_info.value.code == "proposal_base_stale"
    assert exc_info.value.reason == "pins:enrichment_canonical_sha256"


@pytest.mark.asyncio
async def test_exact_origin_accepts_reorder_on_its_synthesis_anchor() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError

    setup = _setup()
    paragraph_entry, _table_entry, enrichment_artifact_id, enrichment_sha256 = (
        _install_dual_stage_registry(setup)
    )
    target = TargetedEditorialRevisionTarget(
        kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
        placement=TargetedEditorialRevisionPlacement(
            kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
            anchor_block_id=paragraph_entry.block_id,
        ),
    )
    preparation = _manual_enrichment_preparation(
        setup,
        occurrence_id=paragraph_entry.block_id,
        target=target,
        operation=TargetedEditorialRevisionOperation.REORDER_ENRICHMENT_ELEMENTS,
        before_hash="9" * 64,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_sha256=enrichment_sha256,
    )

    async with setup.service._uow_factory() as uow:
        await setup.service._assert_exact_origin(uow, preparation)

    misplaced = replace(
        preparation,
        target=TargetedEditorialRevisionTarget(
            kind=TargetedEditorialRevisionTargetKind.ENRICHMENT_ORDER,
            placement=TargetedEditorialRevisionPlacement(
                kind=TargetedEditorialRevisionPlacementKind.AFTER_PARAGRAPH,
                anchor_block_id=uuid4(),
            ),
        ),
    )
    with pytest.raises(EditorialProposalApplicationError) as exc_info:
        async with setup.service._uow_factory() as stale_uow:
            await setup.service._assert_exact_origin(stale_uow, misplaced)
    assert exc_info.value.code == "editorial_anchor_stale"


def _visual_loader(
    setup: SimpleNamespace,
    *,
    context,
    entry: BlockRegistryEntry,
    enrichment_artifact_id: UUID,
    enrichment_sha256: str,
):
    from cti_app.application.editorial_visual_revision import (
        VisualRevisionContext,
        VisualRevisionPreparation,
    )
    from cti_app.domain.production import ProductionArtifact, ProductionArtifactStage

    if not isinstance(context, VisualRevisionContext):
        raise TypeError("synthetic_visual_context_invalid")
    artifact = ProductionArtifact(
        id=enrichment_artifact_id,
        production_run_id=setup.current_snapshot.production_run_id,
        subject_id=setup.subject_id,
        stage=ProductionArtifactStage.EDITORIAL_ENRICHMENT,
        version=2,
        input_hash="2" * 64,
        canonical_blob_id=uuid4(),
    )

    class _Loader:
        async def load(self, *, subject_id, snapshot_id, occurrence_id):
            assert subject_id == setup.subject_id
            assert occurrence_id == entry.block_id
            return VisualRevisionPreparation(
                context=context,
                base_artifact=artifact,
                entry=entry,
                snapshot_id=snapshot_id,
                subject_id=subject_id,
                production_run_id=setup.current_snapshot.production_run_id,
                base_canonical_sha256=enrichment_sha256,
                base_pin_hash=setup.current_snapshot.workspace_pin_hash,
                routing_decision_hash=setup.current_snapshot.routing_decision_hash,
            )

    return _Loader()


def _install_visual_workspace(setup: SimpleNamespace) -> SimpleNamespace:
    async def resolve_current_occurrence(subject_id, occurrence_id):
        del subject_id
        return resolve_occurrence(setup.current_snapshot, occurrence_id)

    workspace = SimpleNamespace(resolve_occurrence=resolve_current_occurrence)
    setup.service._workspace_service = workspace
    return workspace


@pytest.mark.asyncio
async def test_prepare_visual_caption_proposal_is_non_canonical_and_idempotent() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalConflict
    from cti_app.domain.editorial_enrichment_patch import UpdateVisualCaptionPatch
    from cti_app.domain.editorial_proposal import EditorialProposalState
    from cti_app.domain.production_editorial_enrichment import (
        EditorialEnrichmentElementKind,
    )
    from tests.test_editorial_visual_revision import _context

    setup = _setup()
    _paragraph_entry, table_entry, enrichment_artifact_id, enrichment_sha256 = (
        _install_dual_stage_registry(setup)
    )
    _install_visual_workspace(setup)
    context = _context()
    setup.service._visual_revision_loader = _visual_loader(
        setup,
        context=context,
        entry=table_entry,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_sha256=enrichment_sha256,
    )
    patch = UpdateVisualCaptionPatch(
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_one",
        caption="Nouvelle légende synthétique",
    )
    proposal = await setup.service.prepare_visual_revision(
        subject_id=setup.subject_id,
        snapshot_id=setup.current_snapshot.snapshot_id,
        occurrence_id=table_entry.block_id,
        actor_id=setup.actor_id,
        idempotency_key="visual-caption-key-0001",
        patch=patch,
    )

    assert proposal.state is EditorialProposalState.PROPOSED
    assert proposal.operation == "update_visual_caption"
    assert proposal.before_hash == table_entry.content_sha256
    assert proposal.base_artifact_id == enrichment_artifact_id
    assert proposal.base_canonical_sha256 == enrichment_sha256
    preview = cast(dict[str, object], proposal.diagnostics["preview"])
    assert preview["element_key"] == "table_one"
    assert proposal.patch_sha256 is not None
    assert proposal.patch_blob_ref is not None
    assert setup.canonical_writes == []
    assert setup.repository.model_runs == []

    replay = await setup.service.prepare_visual_revision(
        subject_id=setup.subject_id,
        snapshot_id=setup.current_snapshot.snapshot_id,
        occurrence_id=table_entry.block_id,
        actor_id=setup.actor_id,
        idempotency_key="visual-caption-key-0001",
        patch=patch,
    )
    assert replay.id == proposal.id

    with pytest.raises(EditorialProposalConflict):
        await setup.service.prepare_visual_revision(
            subject_id=setup.subject_id,
            snapshot_id=setup.current_snapshot.snapshot_id,
            occurrence_id=table_entry.block_id,
            actor_id=setup.actor_id,
            idempotency_key="visual-caption-key-0001",
            patch=UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="table_one",
                caption="Autre légende synthétique",
            ),
        )


@pytest.mark.asyncio
async def test_prepare_visual_move_targets_the_new_anchor_and_reorder_uses_the_group_hash() -> None:
    from cti_app.application.editorial_visual_revision import enrichment_order_group_hash
    from cti_app.domain.editorial_enrichment_patch import (
        MoveEnrichmentElementPatch,
        ReorderEnrichmentElementsPatch,
    )
    from cti_app.domain.production_editorial_enrichment import (
        EditorialEnrichmentElementKind,
        EnrichmentElementRef,
        EnrichmentPlacementKind,
        EnrichmentPlacementV1,
    )
    from tests.test_editorial_visual_revision import (
        _anchor,
        _base_enrichment,
        _context,
        _table,
    )

    setup = _setup()
    paragraph_entry, table_entry, enrichment_artifact_id, enrichment_sha256 = (
        _install_dual_stage_registry(setup)
    )
    _install_visual_workspace(setup)
    base = _base_enrichment()
    setup.service._visual_revision_loader = _visual_loader(
        setup,
        context=_context_for(base),
        entry=table_entry,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_sha256=enrichment_sha256,
    )
    from tests.test_editorial_visual_revision import ANCHOR_B

    move = MoveEnrichmentElementPatch(
        element_kind=EditorialEnrichmentElementKind.TABLE,
        element_key="table_one",
        anchor_block_id=ANCHOR_B,
    )
    moved = await setup.service.prepare_visual_revision(
        subject_id=setup.subject_id,
        snapshot_id=setup.current_snapshot.snapshot_id,
        occurrence_id=table_entry.block_id,
        actor_id=setup.actor_id,
        idempotency_key="visual-move-key-000001",
        patch=move,
    )
    assert moved.operation == "move_enrichment_element"
    assert moved.target_id is not None

    group_anchor = paragraph_entry.block_id
    grouped = replace(
        base,
        tables=(
            _table("table_one", _anchor(group_anchor), "Which command is documented first?"),
            _table("table_two", _anchor(group_anchor), "Which command is documented second?"),
        ),
    )
    setup.service._visual_revision_loader = _visual_loader(
        setup,
        context=_context(grouped, registered=frozenset({group_anchor, ANCHOR_B})),
        entry=paragraph_entry,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_sha256=enrichment_sha256,
    )
    order = (
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_two"),
        EnrichmentElementRef(EditorialEnrichmentElementKind.TABLE, "table_one"),
    )
    reordered = await setup.service.prepare_visual_revision(
        subject_id=setup.subject_id,
        snapshot_id=setup.current_snapshot.snapshot_id,
        occurrence_id=paragraph_entry.block_id,
        actor_id=setup.actor_id,
        idempotency_key="visual-reorder-key-0001",
        patch=ReorderEnrichmentElementsPatch(anchor_block_id=group_anchor, order=order),
    )
    assert reordered.operation == "reorder_enrichment_elements"
    expected_hash = enrichment_order_group_hash(
        grouped,
        EnrichmentPlacementV1(
            EnrichmentPlacementKind.AFTER_PARAGRAPH, anchor_block_id=group_anchor
        ),
    )
    assert reordered.before_hash == expected_hash


def _context_for(enrichment):
    from tests.test_editorial_visual_revision import _context

    return _context(enrichment)


@pytest.mark.asyncio
async def test_prepare_visual_revision_maps_planner_rejections_to_typed_errors() -> None:
    from cti_app.application.editorial_proposals import EditorialProposalApplicationError
    from cti_app.domain.editorial_enrichment_patch import (
        ReviseChartPatch,
        UpdateVisualCaptionPatch,
    )
    from cti_app.domain.production_editorial_enrichment import (
        EditorialEnrichmentElementKind,
    )
    from tests.test_editorial_visual_revision import _context, _timeline_chart

    setup = _setup()
    _paragraph_entry, table_entry, enrichment_artifact_id, enrichment_sha256 = (
        _install_dual_stage_registry(setup)
    )
    _install_visual_workspace(setup)
    setup.service._visual_revision_loader = _visual_loader(
        setup,
        context=_context(),
        entry=table_entry,
        enrichment_artifact_id=enrichment_artifact_id,
        enrichment_sha256=enrichment_sha256,
    )
    with pytest.raises(EditorialProposalApplicationError) as no_change:
        await setup.service.prepare_visual_revision(
            subject_id=setup.subject_id,
            snapshot_id=setup.current_snapshot.snapshot_id,
            occurrence_id=table_entry.block_id,
            actor_id=setup.actor_id,
            idempotency_key="visual-nochange-key-001",
            patch=UpdateVisualCaptionPatch(
                element_kind=EditorialEnrichmentElementKind.TABLE,
                element_key="table_one",
                caption="Caption table_one",
            ),
        )
    assert no_change.value.code == "enrichment_revision_no_change"
    assert no_change.value.diagnostics["element_key"] == "table_one"

    with pytest.raises(EditorialProposalApplicationError) as unsupported:
        await setup.service.prepare_visual_revision(
            subject_id=setup.subject_id,
            snapshot_id=setup.current_snapshot.snapshot_id,
            occurrence_id=table_entry.block_id,
            actor_id=setup.actor_id,
            idempotency_key="visual-unsupported-key-01",
            patch=ReviseChartPatch(element_key="chart_one", chart=_timeline_chart()),
        )
    # F2-03b implements the chart variant; the fixture's base chart is a
    # timeline chart, so the typed refusal is the BAR-only kind check.
    assert unsupported.value.code == "enrichment_element_kind_mismatch"
    assert setup.repository.records == []
