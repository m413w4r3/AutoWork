from __future__ import annotations

import asyncio
import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    project_blocks,
    resolve_workspace_target,
)
from cti_app.application.editorial_workspace.service import (
    EditorialWorkspaceService,
    advance_workspace_registry,
)
from cti_app.domain.editorial_workspace import (
    EditorialIdentityAmbiguousError,
    EditorialRegistryConflictError,
    EditorialWorkspaceError,
    bootstrap_block_registry,
    workspace_pin_hash,
)


def _synthesis(paragraphs: tuple[dict[str, object], ...]) -> dict[str, object]:
    return {
        "schema_version": "2",
        "title": "Synthetic title",
        "lead": [{"text": "Synthetic lead", "evidence_refs": []}],
        "sections": [
            {
                "kind": "narrative",
                "heading": "Synthetic heading",
                "paragraphs": list(paragraphs),
            }
        ],
        "timeline": [],
        "source_notes": [],
    }


def _paragraph(text: str) -> dict[str, object]:
    return {"text": text, "evidence_refs": []}


def test_legacy_bootstrap_is_stable_and_duplicate_ids_differ() -> None:
    artifact_id = uuid4()
    canonical_sha256 = hashlib.sha256(b"synthetic canonical artifact").hexdigest()
    content = _synthesis((_paragraph("same"), _paragraph("same")))

    first = bootstrap_block_registry(
        artifact_id=artifact_id,
        canonical_sha256=canonical_sha256,
        source_stage="synthesis",
        canonical_content=content,
    )
    reloaded = bootstrap_block_registry(
        artifact_id=artifact_id,
        canonical_sha256=canonical_sha256,
        source_stage="synthesis",
        canonical_content=content,
    )

    paragraphs = [item for item in first if item.kind == "paragraph"]
    assert [item.block_id for item in first] == [item.block_id for item in reloaded]
    assert paragraphs[0].block_id != paragraphs[1].block_id
    assert all(isinstance(item.block_id, UUID) for item in paragraphs)


def test_registry_contains_no_text_or_evidence() -> None:
    entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="a" * 64,
        source_stage="synthesis",
        canonical_content=_synthesis((_paragraph("private synthetic prose"),)),
    )

    encoded = repr([item.to_json() for item in entries])
    assert "private synthetic prose" not in encoded
    assert "evidence_refs" not in encoded
    assert "text" not in encoded


def test_projection_exposes_revision_target_object_with_unique_legacy_id() -> None:
    synthesis = _synthesis(
        (_paragraph("one unique paragraph"), _paragraph("another unique paragraph"))
    )
    entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="8" * 64,
        source_stage="synthesis",
        canonical_content=synthesis,
    )

    blocks, _enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=None,
    )
    paragraph = next(
        block
        for block in blocks
        if block["kind"] == "paragraph" and block["text"] == "one unique paragraph"
    )

    assert paragraph["revision_target"] == {
        "block_id": paragraph["block_id"],
        "legacy_target_id": paragraph["revision_target"]["legacy_target_id"],
    }
    assert isinstance(paragraph["revision_target"]["legacy_target_id"], str)
    assert len(paragraph["revision_target"]["legacy_target_id"]) == 64


def test_projection_has_no_legacy_target_for_duplicate_catalog_occurrences() -> None:
    synthesis = _synthesis((_paragraph("same"), _paragraph("same")))
    entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="7" * 64,
        source_stage="synthesis",
        canonical_content=synthesis,
    )

    blocks, _enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=None,
    )
    paragraphs = [block for block in blocks if block["kind"] == "paragraph"]

    assert all(block["revision_target"]["legacy_target_id"] is None for block in paragraphs)


def test_unresolved_publication_kind_suppresses_operations_and_resolve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, _repository, _artifacts, _artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)), monkeypatch
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["publication_kind"] is None
        assert all(not block["allowed_operations"] for block in snapshot["blocks"])
        block = next(block for block in snapshot["blocks"] if block["kind"] == "paragraph")
        with pytest.raises(EditorialWorkspaceError, match="publication_kind_unresolved"):
            await service.resolve(
                subject_id=subject_id,
                snapshot_id=UUID(snapshot["snapshot_id"]),
                block_id=UUID(block["block_id"]),
                operation="rewrite_paragraph",
            )

    asyncio.run(exercise())


def test_service_resolves_revision_target_from_current_occurrence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_workspace import service as service_module
    from cti_app.domain.publication_kind import PublicationKind

    service, subject_id, _repository, _artifacts, _artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)), monkeypatch
    )
    monkeypatch.setattr(
        service_module,
        "resolve_publication_kind",
        lambda _routing, _publication: (PublicationKind.ARTICLE, "b" * 64),
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        resolved = await service.resolve_revision_target(
            subject_id=subject_id,
            snapshot_id=UUID(snapshot["snapshot_id"]),
            block_id=UUID(title["block_id"]),
            operation="rewrite_title",
        )

        assert resolved.record.snapshot_id == UUID(snapshot["snapshot_id"])
        assert resolved.occurrence.entry is not None
        assert resolved.occurrence.entry.block_id == UUID(title["block_id"])
        assert resolved.target.kind.value == "title"
        assert resolved.target_id == title["revision_target"]["legacy_target_id"]
        assert resolved.before_hash == resolved.record.block_registry[0].content_sha256

    asyncio.run(exercise())


def test_unchanged_block_keeps_id_after_unrelated_paragraph_rewrite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("rewritten paragraph"), _paragraph("unchanged paragraph"))),
        monkeypatch,
    )

    async def exercise() -> None:
        previous = await service.snapshot(subject_id)
        previous_unchanged = next(
            block for block in previous["blocks"] if block["text"] == "unchanged paragraph"
        )
        artifacts.replace_synthesis(
            _synthesis((_paragraph("new paragraph text"), _paragraph("unchanged paragraph"))),
            artifact_store,
        )
        current = await service.snapshot(subject_id)
        current_unchanged = next(
            block for block in current["blocks"] if block["text"] == "unchanged paragraph"
        )

        assert current_unchanged["block_id"] == previous_unchanged["block_id"]
        assert current_unchanged["revision_status"] == "ready"

    asyncio.run(exercise())


def test_moved_equal_duplicate_paragraphs_require_review(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _synthesis(
        (_paragraph("duplicate"), _paragraph("middle"), _paragraph("duplicate"), _paragraph("tail"))
    )
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        original, monkeypatch
    )

    async def exercise() -> None:
        previous = await service.snapshot(subject_id)
        previous_duplicate_ids = {
            block["block_id"] for block in previous["blocks"] if block["text"] == "duplicate"
        }
        artifacts.replace_synthesis(
            _synthesis(
                (
                    _paragraph("middle"),
                    _paragraph("duplicate"),
                    _paragraph("tail"),
                    _paragraph("duplicate"),
                )
            ),
            artifact_store,
        )
        current = await service.snapshot(subject_id)
        moved_duplicates = [block for block in current["blocks"] if block["text"] == "duplicate"]

        assert len(moved_duplicates) == 2
        assert all(block["revision_status"] == "needs_review" for block in moved_duplicates)
        assert all(block["block_id"] not in previous_duplicate_ids for block in moved_duplicates)

    asyncio.run(exercise())


def test_snapshot_reload_is_idempotent(monkeypatch: pytest.MonkeyPatch) -> None:
    service, subject_id, _repository, _artifacts, _artifact_store = _workspace_service(
        _synthesis((_paragraph("stable paragraph"),)), monkeypatch
    )

    async def exercise() -> None:
        first = await service.snapshot(subject_id)
        reloaded = await service.snapshot(subject_id)

        assert reloaded["snapshot_id"] == first["snapshot_id"]
        assert [block["block_id"] for block in reloaded["blocks"]] == [
            block["block_id"] for block in first["blocks"]
        ]

    asyncio.run(exercise())


def test_snapshot_rejects_artifact_pointer_change_between_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("before race"),)), monkeypatch
    )
    current_synthesis = artifacts.current["synthesis"]
    assert current_synthesis is not None
    replacement = _make_artifact(
        "synthesis",
        _synthesis((_paragraph("after race"),)),
        version=current_synthesis.version + 1,
        production_run_id=current_synthesis.production_run_id,
    )
    artifact_store.write(replacement)
    artifacts.change_synthesis_on_second_read = replacement

    async def exercise() -> None:
        with pytest.raises(EditorialWorkspaceError, match="editorial_snapshot_stale"):
            await service.snapshot(subject_id)
        assert repository.records == []

    asyncio.run(exercise())


def test_duplicate_paragraphs_resolve_to_distinct_canonical_paths() -> None:
    artifact_id = uuid4()
    content = _synthesis((_paragraph("same"), _paragraph("same")))
    entries = bootstrap_block_registry(
        artifact_id=artifact_id,
        canonical_sha256="9" * 64,
        source_stage="synthesis",
        canonical_content=content,
    )
    paragraphs = [item for item in entries if item.kind == "paragraph"]

    first = resolve_workspace_target(
        entry=paragraphs[0],
        canonical_content=content,
        operation="rewrite_paragraph",
    )
    second = resolve_workspace_target(
        entry=paragraphs[1],
        canonical_content=content,
        operation="rewrite_paragraph",
    )

    assert first.block_id != second.block_id
    assert first.path == "synthesis/sections/0/paragraphs/0"
    assert second.path == "synthesis/sections/0/paragraphs/1"
    assert first.before_hash == second.before_hash


def test_enrichment_projection_preserves_pinned_legacy_placement() -> None:
    synthesis = _synthesis((_paragraph("body"),))
    enrichment = {
        "tables": [
            {
                "caption": "After lead table",
                "placement": {"kind": "after_lead", "section_index": None},
            },
            {
                "caption": "After section table",
                "placement": {"kind": "after_section", "section_index": 0},
            },
        ],
        "diagrams": [],
        "charts": [],
        "source_figures": [],
    }
    entries = (
        *bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="1" * 64,
            source_stage="synthesis",
            canonical_content=synthesis,
        ),
        *bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="2" * 64,
            source_stage="editorial_enrichment",
            canonical_content=enrichment,
        ),
    )

    blocks, enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=enrichment,
    )

    assert [block["text"] for block in blocks] == [
        "Synthetic title",
        "Synthetic lead",
        "After lead table",
        "Synthetic heading",
        "body",
        "After section table",
    ]
    assert [block["text"] for block in enrichments] == [
        "After lead table",
        "After section table",
    ]


def test_bootstrap_gives_duplicate_sections_distinct_occurrence_ids() -> None:
    duplicate_section = {
        "kind": "narrative",
        "heading": "Repeated",
        "paragraphs": [_paragraph("same")],
    }
    content = {
        **_synthesis(()),
        "sections": [duplicate_section, duplicate_section],
    }

    registry = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="b" * 64,
        source_stage="synthesis",
        canonical_content=content,
    )
    headings = [item for item in registry if item.kind == "section_heading"]
    assert headings[0].block_id != headings[1].block_id


def test_advance_preserves_rewritten_id_and_retires_deleted_occurrence() -> None:
    artifact_id = uuid4()
    initial = bootstrap_block_registry(
        artifact_id=artifact_id,
        canonical_sha256="c" * 64,
        source_stage="synthesis",
        canonical_content=_synthesis((_paragraph("before"), _paragraph("delete me"))),
    )
    paragraphs = [item for item in initial if item.kind == "paragraph"]
    rewritten_id, deleted_id = paragraphs[0].block_id, paragraphs[1].block_id

    lineage = [
        {
            "block_id": str(item.block_id),
            "transition": "delete" if item.block_id == deleted_id else "rewrite",
            "pointer": None if item.block_id == deleted_id else item.structural_pointer,
        }
        for item in initial
    ]
    lineage = [
        {
            **item,
            "pointer": (
                "/sections/0/paragraphs/0"
                if item["block_id"] == str(rewritten_id)
                else item["pointer"]
            ),
        }
        for item in lineage
    ]
    advanced = advance_workspace_registry(
        base_registry=initial,
        artifact_id=uuid4(),
        canonical_sha256="d" * 64,
        source_stage="synthesis",
        canonical_content=_synthesis((_paragraph("after"), _paragraph("inserted"))),
        lineage=(
            *lineage,
            {"transition": "insert", "pointer": "/sections/0/paragraphs/1"},
        ),
    )

    paragraph = next(item for item in advanced.active if item.kind == "paragraph")
    assert paragraph.block_id == rewritten_id
    assert (
        paragraph.content_sha256
        == hashlib.sha256(b'{"evidence_refs":[],"text":"after"}').hexdigest()
    )
    assert deleted_id in advanced.retired_block_ids
    inserted = [item for item in advanced.active if item.transition == "insert"]
    assert len(inserted) == 1
    assert inserted[0].structural_pointer == "/sections/0/paragraphs/1"


def test_advance_refuses_missing_legacy_correspondence() -> None:
    base = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="f" * 64,
        source_stage="synthesis",
        canonical_content=_synthesis((_paragraph("same"), _paragraph("same"))),
    )

    with pytest.raises(EditorialIdentityAmbiguousError):
        advance_workspace_registry(
            base_registry=base,
            artifact_id=uuid4(),
            canonical_sha256="0" * 64,
            source_stage="synthesis",
            canonical_content=_synthesis((_paragraph("same"),)),
            lineage=(),
        )


def test_advance_rejects_duplicate_retired_ids_before_normalizing() -> None:
    retired_id = uuid4()

    with pytest.raises(EditorialRegistryConflictError):
        advance_workspace_registry(
            base_registry=(),
            artifact_id=uuid4(),
            canonical_sha256="f" * 64,
            source_stage="synthesis",
            canonical_content={},
            lineage=(),
            retired_block_ids=(retired_id, retired_id),
        )


def test_workspace_pin_hash_is_stable_for_same_pins() -> None:
    production_run_id = uuid4()
    synthesis_artifact_id = uuid4()
    pins = {
        "production_run_id": production_run_id,
        "synthesis_artifact_id": synthesis_artifact_id,
        "synthesis_version": 1,
        "synthesis_canonical_sha256": "e" * 64,
        "enrichment_artifact_id": None,
        "enrichment_version": None,
        "enrichment_canonical_sha256": None,
        "publication_artifact_id": None,
        "publication_version": None,
        "publication_canonical_sha256": None,
        "pipeline_generation": 0,
    }
    assert workspace_pin_hash(pins) == workspace_pin_hash(dict(pins))


def _make_artifact(
    stage: str,
    content: dict[str, object],
    *,
    version: int = 1,
    production_run_id: UUID | None = None,
) -> SimpleNamespace:
    artifact_id = uuid4()
    return SimpleNamespace(
        id=artifact_id,
        stage=stage,
        version=version,
        input_hash="a" * 64,
        canonical_blob_id=artifact_id,
        content=content,
        production_run_id=production_run_id,
    )


def _workspace_service(
    synthesis: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[
    EditorialWorkspaceService,
    UUID,
    _FakeWorkspaceRepository,
    _FakeProductionArtifacts,
    _FakeArtifactStore,
]:

    from cti_app.application.editorial_workspace import service as service_module

    subject_id = uuid4()
    run = SimpleNamespace(id=uuid4(), pipeline_generation=1)
    synthesis_artifact = _make_artifact("synthesis", synthesis, production_run_id=run.id)
    repository = _FakeWorkspaceRepository()
    artifacts = _FakeProductionArtifacts(synthesis_artifact)
    uow = _FakeUnitOfWork(subject_id, run, artifacts, repository)
    artifact_store = _FakeArtifactStore()
    artifact_store.write(synthesis_artifact)

    async def no_routing_decision(**_kwargs: object) -> None:
        return None

    monkeypatch.setattr(
        service_module,
        "get_adopted_production_routing_decision",
        no_routing_decision,
    )
    service = EditorialWorkspaceService(
        uow_factory=lambda: uow,
        artifact_store=artifact_store,
    )
    return service, subject_id, repository, artifacts, artifact_store


class _FakeArtifactStore:
    def __init__(self) -> None:
        self._payloads: dict[UUID, bytes] = {}

    def write(self, artifact: SimpleNamespace) -> None:
        self._payloads[artifact.canonical_blob_id] = json.dumps(artifact.content).encode()

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return self._payloads[blob_id]


class _FakeProductionArtifacts:
    def __init__(self, synthesis: SimpleNamespace) -> None:
        self.current: dict[str, SimpleNamespace | None] = {
            "synthesis": synthesis,
            "editorial_enrichment": None,
            "publication": None,
        }
        self._synthesis_reads = 0
        self.change_synthesis_on_second_read: SimpleNamespace | None = None

    async def get_current_for_revision(self, _run_id: UUID, stage: str) -> SimpleNamespace:
        assert stage == "synthesis"
        self._synthesis_reads += 1
        if self._synthesis_reads == 2 and self.change_synthesis_on_second_read is not None:
            return self.change_synthesis_on_second_read
        artifact = self.current[stage]
        assert artifact is not None
        return artifact

    async def get_current(self, _run_id: UUID, stage: str) -> SimpleNamespace | None:
        return self.current[stage]

    def replace_synthesis(
        self, content: dict[str, object], artifact_store: _FakeArtifactStore
    ) -> None:
        previous = self.current["synthesis"]
        assert previous is not None
        artifact = _make_artifact(
            "synthesis",
            content,
            version=previous.version + 1,
            production_run_id=previous.production_run_id,
        )
        self.current["synthesis"] = artifact
        artifact_store.write(artifact)


class _FakeWorkspaceRepository:
    def __init__(self) -> None:
        self.records: list[WorkspaceSnapshotRecord] = []

    async def get_by_pin(self, run_id: UUID, pin_hash: str) -> WorkspaceSnapshotRecord | None:
        return next(
            (
                record
                for record in self.records
                if record.production_run_id == run_id and record.workspace_pin_hash == pin_hash
            ),
            None,
        )

    async def append_if_absent(self, record: WorkspaceSnapshotRecord) -> WorkspaceSnapshotRecord:
        existing = await self.get_by_pin(record.production_run_id, record.workspace_pin_hash)
        if existing is not None:
            return existing
        self.records.append(record)
        return record

    async def latest_for_run(self, run_id: UUID) -> WorkspaceSnapshotRecord | None:
        return next(
            (record for record in reversed(self.records) if record.production_run_id == run_id),
            None,
        )


class _FakeUnitOfWork:
    def __init__(
        self,
        subject_id: UUID,
        run: SimpleNamespace,
        artifacts: _FakeProductionArtifacts,
        repository: _FakeWorkspaceRepository,
    ):
        self.subjects = SimpleNamespace(get=self._get_subject)
        self.production_runs = SimpleNamespace(
            get_current_for_subject=self._get_run,
            lock_creation_for_subject=self._lock_creation_for_subject,
        )
        self.production_artifacts = artifacts
        self.editorial_workspace_snapshots = repository
        self.subject_id = subject_id
        self.run = run

    async def _get_subject(self, subject_id: UUID) -> SimpleNamespace | None:
        return SimpleNamespace(id=subject_id) if subject_id == self.subject_id else None

    async def _get_run(self, _subject_id: UUID) -> SimpleNamespace:
        return self.run

    async def _lock_creation_for_subject(self, _subject_id: UUID) -> None:
        return None

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None
