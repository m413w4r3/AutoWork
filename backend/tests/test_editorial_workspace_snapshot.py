from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.editorial_synthesis.targets import resolve_target
from cti_app.application.editorial_workspace.registry import (
    WorkspaceSnapshotRecord,
    allowed_operations,
    project_blocks,
    registry_hash,
    resolve_paragraph_anchor,
    resolve_workspace_target,
)
from cti_app.application.editorial_workspace.service import (
    EditorialWorkspaceService,
    advance_workspace_registry,
)
from cti_app.domain.editorial_proposal_impact import (
    EditorialProposalImpact,
    EditorialProposalImpactState,
)
from cti_app.domain.editorial_workspace import (
    BlockRegistryEntry,
    EditorialIdentityAmbiguousError,
    EditorialRegistryConflictError,
    EditorialSourceStage,
    EditorialWorkspaceError,
    bootstrap_block_registry,
    canonical_content_sha256,
    workspace_pin_hash,
)
from cti_app.domain.production import ProductionArtifactStatus
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.publication_render import PublicationRender
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus


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


def test_snapshot_passes_current_catalog_figure_ids_to_registry_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from unittest.mock import Mock

    from cti_app.application.editorial_figure_catalog import opaque_figure_id
    from cti_app.application.editorial_workspace import service as service_module

    service, subject_id, _repository, artifacts, _artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)), monkeypatch
    )
    synthesis = artifacts.current["synthesis"]
    assert synthesis is not None
    run_id = synthesis.production_run_id
    source_figure_id = uuid4()
    expected_id = opaque_figure_id(subject_id, run_id, source_figure_id)

    class _Catalog:
        async def catalog(self, *, subject_id: UUID, limit: int) -> SimpleNamespace:
            assert subject_id == service_subject_id
            assert limit == 1
            return SimpleNamespace(items=(), available_count=0)

        async def source_figure_catalog_figures(
            self, subject_id: UUID
        ) -> tuple[UUID, dict[UUID, SimpleNamespace]]:
            assert subject_id == service_subject_id
            return run_id, {
                source_figure_id: SimpleNamespace(
                    figure_id=expected_id,
                    original_caption="Synthetic original caption",
                )
            }

    service_subject_id = subject_id
    service._figure_catalog_service = _Catalog()
    project_blocks_spy = Mock(wraps=service_module.project_blocks)
    monkeypatch.setattr(service_module, "project_blocks", project_blocks_spy)

    asyncio.run(service.snapshot(subject_id))

    assert project_blocks_spy.call_args is not None
    assert project_blocks_spy.call_args.kwargs["catalog_figure_ids"] == {
        source_figure_id: expected_id
    }
    assert project_blocks_spy.call_args.kwargs["catalog_figure_captions"] == {
        source_figure_id: "Synthetic original caption"
    }


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


def test_lazy_snapshot_preserves_parent_across_pin_change_and_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("stable paragraph"),)), monkeypatch
    )

    async def exercise() -> None:
        first = await service.snapshot(subject_id)
        first_record = repository.records[-1]
        assert first_record.parent_snapshot_id is None

        synthesis_artifact = artifacts.current["synthesis"]
        assert synthesis_artifact is not None
        publication = _make_artifact(
            "publication",
            {"schema_version": "8"},
            production_run_id=synthesis_artifact.production_run_id,
        )
        artifacts.current["publication"] = publication
        artifact_store.write(publication)

        second = await service.snapshot(subject_id)
        second_record = repository.records[-1]
        assert second_record.parent_snapshot_id == first_record.snapshot_id
        assert second["snapshot_id"] != first["snapshot_id"]
        assert len(repository.records) == 2

        reloaded = await service.snapshot(subject_id)
        assert reloaded["snapshot_id"] == second["snapshot_id"]
        assert len(repository.records) == 2

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


def _paragraph_entries(
    content: dict[str, object],
) -> tuple[BlockRegistryEntry, ...]:
    return bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="9" * 64,
        source_stage="synthesis",
        canonical_content=content,
    )


@pytest.mark.parametrize(
    ("pointer", "operation"),
    (
        ("/sections/0/paragraphs/0", "rewrite_paragraph"),
        ("/sections/0/paragraphs/1", "rewrite_paragraph"),
        ("/sections/0/paragraphs/1", "delete_paragraph"),
        ("/sections/0/paragraphs/2", "rewrite_paragraph"),
        ("/sections/0/heading", "rename_section"),
        ("/sections/0/heading", "rewrite_section"),
        ("/lead/0", "rewrite_paragraph"),
        ("/title", "rewrite_title"),
    ),
)
def test_registered_target_resolves_against_the_full_synthesis(
    pointer: str, operation: str
) -> None:
    # Regression: the typed target used to be built on a payload reduced to the
    # selected occurrence, so a paragraph in a multi-paragraph section carried
    # the hash of a one-paragraph section as `parent_id`. Prepare and adoption
    # resolve the same target against the *full* synthesis and rejected it.
    content = _synthesis(
        (_paragraph("first body"), _paragraph("target body"), _paragraph("third body"))
    )
    entries = _paragraph_entries(content)
    entry = next(item for item in entries if item.structural_pointer == pointer)

    resolved = resolve_workspace_target(
        entry=entry,
        canonical_content=content,
        operation=operation,
    )
    canonical = resolve_target(
        content,
        resolved.target,
        TargetedEditorialRevisionOperation(operation),
    )

    assert canonical.path
    assert canonical.before_hash == resolved.before_hash


def test_multi_paragraph_section_target_keeps_the_full_section_parent() -> None:
    content = _synthesis(
        (_paragraph("first body"), _paragraph("target body"), _paragraph("third body"))
    )
    entries = _paragraph_entries(content)
    target_entry = next(
        item for item in entries if item.structural_pointer == "/sections/0/paragraphs/1"
    )
    section = content["sections"][0]
    assert isinstance(section, dict)

    resolved = resolve_workspace_target(
        entry=target_entry,
        canonical_content=content,
        operation="rewrite_paragraph",
    )
    canonical = resolve_target(
        content,
        resolved.target,
        TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH,
    )

    assert resolved.target.parent_id == canonical_content_sha256(section)
    assert resolved.target.stable_ids == (target_entry.content_sha256,)
    assert canonical.parent_index == 0
    assert canonical.indexes == (1,)
    assert canonical.before_hash == target_entry.content_sha256


def test_duplicate_paragraphs_resolve_to_distinct_canonical_paths() -> None:
    # The same content in two sections keeps two usable targets: the section
    # parent hash disambiguates them on the full payload, while the paragraph
    # content id (and therefore `before_hash`) stays equal.
    content: dict[str, object] = {
        "schema_version": "2",
        "title": "Synthetic title",
        "lead": [{"text": "Synthetic lead", "evidence_refs": []}],
        "sections": [
            {
                "kind": "narrative",
                "heading": "First heading",
                "paragraphs": [_paragraph("same body")],
            },
            {
                "kind": "narrative",
                "heading": "Second heading",
                "paragraphs": [_paragraph("same body")],
            },
        ],
        "timeline": [],
        "source_notes": [],
    }
    entries = _paragraph_entries(content)
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
    assert second.path == "synthesis/sections/1/paragraphs/0"
    assert first.before_hash == second.before_hash
    assert first.target != second.target
    first_canonical = resolve_target(
        content, first.target, TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
    )
    second_canonical = resolve_target(
        content, second.target, TargetedEditorialRevisionOperation.REWRITE_PARAGRAPH
    )
    assert (first_canonical.parent_index, first_canonical.indexes) == (0, (0,))
    assert (second_canonical.parent_index, second_canonical.indexes) == (1, (0,))


def test_identical_paragraphs_in_one_section_get_distinct_occurrence_targets() -> None:
    # Equal content in one section keeps two independent typed targets: the
    # optional occurrence ordinal disambiguates the n-th equal paragraph, so
    # both blocks advertise their text operations instead of the former
    # fail-closed snapshot.
    content = _synthesis((_paragraph("same body"), _paragraph("same body")))
    entries = _paragraph_entries(content)
    paragraphs = [item for item in entries if item.kind == "paragraph"]

    first, second = (
        resolve_workspace_target(
            entry=entry,
            canonical_content=content,
            operation="rewrite_paragraph",
        )
        for entry in paragraphs
    )
    assert first.target != second.target
    assert first.target.occurrence_ordinal == 0
    assert second.target.occurrence_ordinal == 1
    assert first.path != second.path
    assert first.legacy_target_id is None and second.legacy_target_id is None
    assert first.before_hash == second.before_hash
    for entry in paragraphs:
        assert allowed_operations(entry, content) == (
            "rewrite_paragraph",
            "delete_paragraph",
        )
        assert "create_table" in allowed_operations(entry, content, allow_table_insertion=True)

    blocks, _enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=content,
        enrichment=None,
    )
    projected = [block for block in blocks if block["kind"] == "paragraph"]
    assert len(projected) == 2
    assert all(
        block["allowed_operations"] == ["rewrite_paragraph", "delete_paragraph"]
        for block in projected
    )
    assert all(
        block["revision_target"]
        == {
            "block_id": block["block_id"],
            "legacy_target_id": None,
        }
        for block in projected
    )


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


def test_after_paragraph_requires_registered_anchor() -> None:
    synthesis = _synthesis((_paragraph("first"), _paragraph("anchor")))
    synthesis_entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="3" * 64,
        source_stage="synthesis",
        canonical_content=synthesis,
    )
    anchor = next(
        entry
        for entry in synthesis_entries
        if entry.kind == "paragraph" and entry.structural_pointer.endswith("/1")
    )
    enrichment = {
        "tables": [
            {
                "key": "anchored-table",
                "element_key": "anchored-table",
                "caption": "After the registered paragraph",
                "placement": {
                    "kind": "after_paragraph",
                    "anchor_block_id": str(anchor.block_id),
                },
            }
        ],
        "diagrams": [],
        "charts": [],
        "source_figures": [],
    }
    enrichment_entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="4" * 64,
        source_stage="editorial_enrichment",
        canonical_content=enrichment,
    )
    entries = (*synthesis_entries, *enrichment_entries)
    record = _snapshot_record(entries)

    blocks, enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=enrichment,
        record=record,
    )

    assert [block["text"] for block in blocks][-2:] == [
        "anchor",
        "After the registered paragraph",
    ]
    assert enrichments[0]["revision_status"] == "ready"

    unregistered_enrichment = {
        **enrichment,
        "tables": [
            {
                **enrichment["tables"][0],
                "placement": {
                    "kind": "after_paragraph",
                    "anchor_block_id": str(uuid4()),
                },
            }
        ],
    }
    unregistered_entries = (
        *synthesis_entries,
        *bootstrap_block_registry(
            artifact_id=uuid4(),
            canonical_sha256="5" * 64,
            source_stage="editorial_enrichment",
            canonical_content=unregistered_enrichment,
        ),
    )
    stale_blocks, stale_enrichments, _stale_outline = project_blocks(
        entries=unregistered_entries,
        synthesis=synthesis,
        enrichment=unregistered_enrichment,
        record=_snapshot_record(unregistered_entries),
    )

    assert all(block.get("text") != "After the registered paragraph" for block in stale_blocks)
    assert stale_enrichments[0]["revision_status"] == "stale"


def test_deleted_anchor_marks_enrichment_and_preview_stale_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_workspace import service as service_module

    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("anchor paragraph"), _paragraph("paragraph to keep"))),
        monkeypatch,
    )

    async def exercise() -> None:
        initial = await service.snapshot(subject_id)
        anchor = next(block for block in initial["blocks"] if block["text"] == "anchor paragraph")
        synthesis_artifact = artifacts.current["synthesis"]
        assert synthesis_artifact is not None
        enrichment = _make_artifact(
            "editorial_enrichment",
            {
                "schema_version": 9,
                "tables": [
                    {
                        "key": "anchored-table",
                        "element_key": "anchored-table",
                        "caption": "Do not move to the end",
                        "placement": {
                            "kind": "after_paragraph",
                            "anchor_block_id": anchor["block_id"],
                        },
                    }
                ],
                "diagrams": [],
                "charts": [],
                "source_figures": [],
            },
            production_run_id=synthesis_artifact.production_run_id,
        )
        artifacts.current["editorial_enrichment"] = enrichment
        artifact_store.write(enrichment)

        valid = await service.snapshot(subject_id)
        assert valid["preview_status"] == "STALE"
        assert any(block["text"] == "Do not move to the end" for block in valid["blocks"])

        artifacts.replace_synthesis(_synthesis((_paragraph("paragraph to keep"),)), artifact_store)
        stale = await service.snapshot(subject_id)

        assert stale["preview_status"] == "STALE"
        assert all(block["text"] != "Do not move to the end" for block in stale["blocks"])
        assert stale["enrichments"][0]["revision_status"] == "stale"

    monkeypatch.setattr(
        service_module,
        "resolve_publication_kind",
        lambda _routing, _publication: (PublicationKind.ARTICLE, "b" * 64),
    )
    asyncio.run(exercise())


def _snapshot_record(entries: tuple[BlockRegistryEntry, ...]) -> WorkspaceSnapshotRecord:
    return WorkspaceSnapshotRecord(
        snapshot_id=uuid4(),
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_version=1,
        synthesis_input_hash="a" * 64,
        synthesis_canonical_sha256="b" * 64,
        enrichment_artifact_id=uuid4(),
        enrichment_version=1,
        enrichment_input_hash="c" * 64,
        enrichment_canonical_sha256="d" * 64,
        publication_artifact_id=None,
        publication_version=None,
        publication_input_hash=None,
        publication_canonical_sha256=None,
        pipeline_generation=1,
        routing_decision_id=None,
        routing_decision_hash=None,
        publication_kind=PublicationKind.ARTICLE,
        workspace_pin_hash="e" * 64,
        registry_hash=registry_hash(entries),
        block_registry=entries,
    )


def test_unchanged_imported_stage_preserves_registered_occurrence_ids() -> None:
    content = _synthesis((_paragraph("anchored paragraph"),))
    source_artifact_id = uuid4()
    imported_artifact_id = uuid4()
    canonical_sha = "b" * 64
    source_entries = bootstrap_block_registry(
        artifact_id=source_artifact_id,
        canonical_sha256=canonical_sha,
        source_stage="synthesis",
        canonical_content=content,
    )
    imported_entries = bootstrap_block_registry(
        artifact_id=imported_artifact_id,
        canonical_sha256=canonical_sha,
        source_stage="synthesis",
        canonical_content=content,
    )
    previous_record = replace(
        _snapshot_record(source_entries),
        synthesis_artifact_id=imported_artifact_id,
        synthesis_canonical_sha256=canonical_sha,
    )

    preserved = EditorialWorkspaceService._reconcile_changed_stage(
        entries=list(imported_entries),
        previous_record=previous_record,
        stage=EditorialSourceStage.SYNTHESIS,
        unchanged=True,
        current_version=1,
        previous_version=1,
    )

    assert [entry.block_id for entry in preserved] == [entry.block_id for entry in source_entries]


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


def test_paragraph_anchor_survives_rewrite_insertion_before_and_section_move() -> None:
    base = {
        **_synthesis(()),
        "sections": [
            {
                "kind": "narrative",
                "heading": "Original heading",
                "paragraphs": [_paragraph("before"), _paragraph("anchor before edit")],
            }
        ],
    }
    initial = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="1" * 64,
        source_stage="synthesis",
        canonical_content=base,
    )
    target = next(item for item in initial if item.structural_pointer == "/sections/0/paragraphs/1")
    updated = {
        **_synthesis(()),
        "sections": [
            {
                "kind": "narrative",
                "heading": "Inserted section",
                "paragraphs": [_paragraph("inserted before target")],
            },
            {
                "kind": "narrative",
                "heading": "Original heading revised",
                "paragraphs": [_paragraph("anchor after edit")],
            },
        ],
    }
    target_heading = next(item for item in initial if item.kind == "section_heading")
    title = next(item for item in initial if item.kind == "title")
    lead = next(item for item in initial if item.kind == "lead_paragraph")
    lineage: list[dict[str, str | None]] = []
    for item in initial:
        if item.block_id == target_heading.block_id:
            lineage.append(
                {
                    "block_id": str(item.block_id),
                    "transition": "rewrite",
                    "pointer": "/sections/1/heading",
                }
            )
        elif item.block_id == title.block_id:
            lineage.append(
                {"block_id": str(item.block_id), "transition": "move", "pointer": "/title"}
            )
        elif item.block_id == lead.block_id:
            lineage.append(
                {
                    "block_id": str(item.block_id),
                    "transition": "move",
                    "pointer": "/lead/0",
                }
            )
        elif item.block_id == target.block_id:
            lineage.append(
                {
                    "block_id": str(item.block_id),
                    "transition": "rewrite",
                    "pointer": "/sections/1/paragraphs/0",
                }
            )
        else:
            lineage.append(
                {"block_id": str(item.block_id), "transition": "delete", "pointer": None}
            )
    advanced = advance_workspace_registry(
        base_registry=initial,
        artifact_id=uuid4(),
        canonical_sha256="2" * 64,
        source_stage="synthesis",
        canonical_content=updated,
        lineage=(
            *lineage,
            {"transition": "insert", "pointer": "/sections/0/heading"},
            {"transition": "insert", "pointer": "/sections/0/paragraphs/0"},
        ),
    )

    record = _snapshot_record(advanced.active)
    resolved = resolve_paragraph_anchor(record, target.block_id, synthesis=updated)

    assert resolved.block_id == target.block_id
    assert resolved.structural_pointer == "/sections/1/paragraphs/0"
    assert resolved.section_index == 1
    assert resolved.paragraph_index == 0
    assert resolved.canonical_position == 0


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


@pytest.mark.parametrize(
    ("render_status", "expected_preview_status", "expected_pdf_status"),
    [
        (TypstRenderStatus.SUCCEEDED, "READY", "CURRENT"),
        (TypstRenderStatus.RUNNING, "GENERATING", "STALE"),
        (TypstRenderStatus.FAILED, "FAILED", "STALE"),
    ],
)
def test_workspace_snapshot_reads_publication_render_cycle_without_impact(
    monkeypatch: pytest.MonkeyPatch,
    render_status: TypstRenderStatus,
    expected_preview_status: str,
    expected_pdf_status: str,
) -> None:
    from cti_app.application.editorial_workspace import service as service_module
    from cti_app.domain.publication_kind import PublicationKind

    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)), monkeypatch
    )
    publication = _make_artifact(
        "publication",
        {"schema_version": "9", "title": "Synthetic publication"},
        production_run_id=artifacts.current["synthesis"].production_run_id,
    )
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    uow = service._uow_factory()
    uow.publication_render = _publication_render(publication.id, render_status)
    monkeypatch.setattr(
        service_module,
        "resolve_publication_kind",
        lambda _routing, _publication: (PublicationKind.ARTICLE, "b" * 64),
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)

        assert snapshot["impact_status"] == "none"
        assert snapshot["preview_status"] == expected_preview_status
        assert snapshot["pdf_status"] == expected_pdf_status

    asyncio.run(exercise())


def test_workspace_snapshot_preserves_stale_render_status_without_impact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)), monkeypatch
    )
    publication = _make_artifact(
        "publication",
        {"schema_version": "9", "title": "Synthetic publication"},
        production_run_id=artifacts.current["synthesis"].production_run_id,
    )
    publication.status = ProductionArtifactStatus.STALE
    artifacts.current["publication"] = publication
    artifact_store.write(publication)

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["impact_status"] == "none"
        assert snapshot["preview_status"] == "STALE"
        assert snapshot["pdf_status"] == "STALE"

    asyncio.run(exercise())


def test_pending_impact_without_publication_does_not_mark_preview_stale(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, _repository, _artifacts, _artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)),
        monkeypatch,
        impact_state=EditorialProposalImpactState.PENDING,
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["impact_status"] == "pending"
        assert snapshot["publication_artifact_id"] is None
        assert snapshot["preview_status"] == "STALE"
        assert snapshot["pdf_status"] == "STALE"

    asyncio.run(exercise())


def test_pending_impact_marks_preview_stale_when_publication_uses_old_synthesis(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.editorial_workspace import service as service_module
    from cti_app.domain.publication_kind import PublicationKind

    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)),
        monkeypatch,
        impact_state=EditorialProposalImpactState.PENDING,
    )
    old_synthesis = artifacts.current["synthesis"]
    assert old_synthesis is not None
    artifacts.replace_synthesis(
        _synthesis((_paragraph("revised synthetic paragraph"),)), artifact_store
    )
    publication = _make_artifact(
        "publication",
        {"schema_version": "8", "title": "Synthetic publication from old synthesis"},
        production_run_id=old_synthesis.production_run_id,
    )
    publication.metadata = {"input_artifacts": {"synthesis_artifact_id": str(old_synthesis.id)}}
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    monkeypatch.setattr(
        service_module,
        "resolve_publication_kind",
        lambda _routing, _publication: (PublicationKind.BRIEF, "b" * 64),
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["impact_status"] == "pending"
        assert snapshot["publication_artifact_id"] == str(publication.id)
        assert snapshot["preview_status"] == "STALE"
        assert snapshot["pdf_status"] == "STALE"

    asyncio.run(exercise())


@pytest.mark.parametrize(
    ("has_current_enrichment", "expected_preview_status", "expected_pdf_status"),
    [(False, "READY", "CURRENT"), (True, "STALE", "STALE")],
)
def test_publication_without_enrichment_pin_is_stale_only_when_enrichment_exists(
    monkeypatch: pytest.MonkeyPatch,
    has_current_enrichment: bool,
    expected_preview_status: str,
    expected_pdf_status: str,
) -> None:
    from cti_app.application.editorial_workspace import service as service_module
    from cti_app.domain.publication_kind import PublicationKind

    service, subject_id, _repository, artifacts, artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)),
        monkeypatch,
        impact_state=EditorialProposalImpactState.PENDING,
    )
    synthesis_artifact = artifacts.current["synthesis"]
    assert synthesis_artifact is not None
    if has_current_enrichment:
        enrichment = _make_artifact(
            "editorial_enrichment",
            {"schema_version": "8", "elements": []},
            production_run_id=synthesis_artifact.production_run_id,
        )
        artifacts.current["editorial_enrichment"] = enrichment
        artifact_store.write(enrichment)

    publication = _make_artifact(
        "publication",
        {"schema_version": "9", "title": "Synthetic publication without enrichment"},
        production_run_id=synthesis_artifact.production_run_id,
    )
    publication.metadata = {
        "input_artifacts": {"synthesis_artifact_id": str(synthesis_artifact.id)}
    }
    artifacts.current["publication"] = publication
    artifact_store.write(publication)
    uow = service._uow_factory()
    uow.publication_render = _publication_render(publication.id, TypstRenderStatus.SUCCEEDED)
    monkeypatch.setattr(
        service_module,
        "resolve_publication_kind",
        lambda _routing, _publication: (PublicationKind.BRIEF, "b" * 64),
    )

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["impact_status"] == "pending"
        assert snapshot["preview_status"] == expected_preview_status
        assert snapshot["pdf_status"] == expected_pdf_status

    asyncio.run(exercise())


def test_snapshot_keeps_latest_stale_publication_visible_after_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    service, subject_id, _repository, artifacts, _artifact_store = _workspace_service(
        _synthesis((_paragraph("synthetic paragraph"),)),
        monkeypatch,
        impact_state=EditorialProposalImpactState.DONE,
    )
    stale_publication = _make_artifact(
        "publication",
        {"schema_version": "8", "title": "Synthetic stale publication"},
        production_run_id=artifacts.current["synthesis"].production_run_id,
    )
    stale_publication.status = ProductionArtifactStatus.STALE
    artifacts.history.append(stale_publication)

    async def exercise() -> None:
        snapshot = await service.snapshot(subject_id)
        assert snapshot["impact_status"] == "up_to_date"
        assert snapshot["publication_artifact_id"] is None
        assert snapshot["preview_status"] == "STALE"
        assert snapshot["pdf_status"] == "STALE"

    asyncio.run(exercise())


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
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=artifact_id,
        metadata={},
        content=content,
        production_run_id=production_run_id,
    )


def _publication_render(
    publication_artifact_id: UUID,
    status: TypstRenderStatus,
) -> PublicationRender:
    succeeded = status is TypstRenderStatus.SUCCEEDED
    failed = status is TypstRenderStatus.FAILED
    now = datetime.now(UTC)
    return PublicationRender(
        id=uuid4(),
        publication_artifact_id=publication_artifact_id,
        renderer="typst",
        renderer_version="1",
        template_version="1",
        template_sha256="b" * 64,
        compiler="typst",
        compiler_version="1",
        font_bundle_version="1",
        render_policy_version="1",
        format=TypstRenderFormat.PDF,
        input_hash="c" * 64,
        source_blob_id=None,
        render_data_blob_id=None,
        output_blob_id=uuid4() if succeeded else None,
        output_sha256="d" * 64 if succeeded else None,
        output_byte_size=128 if succeeded else None,
        status=status,
        error_code="typst_binary_unavailable" if failed else None,
        error_message="Typst binary is unavailable" if failed else None,
        created_at=now,
        updated_at=now,
    )


def _workspace_service(
    synthesis: dict[str, object],
    monkeypatch: pytest.MonkeyPatch,
    *,
    impact_state: EditorialProposalImpactState | None = None,
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
    impact = (
        EditorialProposalImpact(
            impact_id=uuid4(),
            subject_id=subject_id,
            production_run_id=run.id,
            proposal_id=uuid4(),
            accepted_artifact_id=synthesis_artifact.id,
            accepted_snapshot_id=uuid4(),
            base_pin_hash="a" * 64,
            accepted_pin_hash="b" * 64,
            sequence=1,
            state=impact_state,
            completed_at=(
                datetime.now(UTC)
                if impact_state
                in {
                    EditorialProposalImpactState.DONE,
                    EditorialProposalImpactState.FAILED,
                    EditorialProposalImpactState.SUPERSEDED,
                }
                else None
            ),
        )
        if impact_state is not None
        else None
    )
    uow = _FakeUnitOfWork(subject_id, run, artifacts, repository, impact)
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
        self.history: list[SimpleNamespace] = []
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

    async def list_for_run(self, _run_id: UUID) -> tuple[SimpleNamespace, ...]:
        return (
            *self.history,
            *(item for item in self.current.values() if item is not None),
        )

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
        impact: EditorialProposalImpact | None,
    ):
        self.subjects = SimpleNamespace(get=self._get_subject)
        self.production_runs = SimpleNamespace(
            get_current_for_subject=self._get_run,
            lock_creation_for_subject=self._lock_creation_for_subject,
        )
        self.production_artifacts = artifacts
        self.editorial_workspace_snapshots = repository
        self.editorial_proposal_impacts = SimpleNamespace(latest_for_run=self._latest_impact)
        self.publication_render = None
        self.publication_renders = SimpleNamespace(
            get_latest_for_artifact=self._latest_publication_render
        )
        self.subject_id = subject_id
        self.run = run
        self.impact = impact

    async def _get_subject(self, subject_id: UUID) -> SimpleNamespace | None:
        return SimpleNamespace(id=subject_id) if subject_id == self.subject_id else None

    async def _get_run(self, _subject_id: UUID) -> SimpleNamespace:
        return self.run

    async def _lock_creation_for_subject(self, _subject_id: UUID) -> None:
        return None

    async def _latest_impact(self, _run_id: UUID) -> EditorialProposalImpact | None:
        return self.impact

    async def _latest_publication_render(self, _artifact_id: UUID):
        return self.publication_render

    async def __aenter__(self) -> _FakeUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


def _order_fixture_synthesis() -> dict[str, object]:
    return _synthesis(
        (
            _paragraph("first paragraph"),
            _paragraph("anchor paragraph"),
            _paragraph("last paragraph"),
        )
    )


def _order_fixture_enrichment(
    anchor_block_id: UUID, *, explicit: bool = False
) -> dict[str, object]:
    def group_order(value: int | None) -> dict[str, int]:
        return {} if value is None else {"group_order": value}

    after_paragraph = {
        "kind": "after_paragraph",
        "anchor_block_id": str(anchor_block_id),
    }
    after_section = {"kind": "after_section", "section_index": 0}
    return {
        "tables": [
            {
                "key": "table_anchor",
                "placement": after_paragraph,
                **group_order(1 if explicit else None),
            },
            {
                "key": "table_end",
                "placement": after_section,
            },
        ],
        "diagrams": [],
        "charts": [],
        "source_figures": [
            {
                "key": "figure_z",
                "order": 1,
                "placement": after_paragraph,
                **group_order(2 if explicit else None),
            },
            {
                "key": "figure_a",
                "order": 0,
                "placement": after_paragraph,
                **group_order(0 if explicit else None),
            },
            {
                "key": "figure_end",
                "placement": after_section,
            },
        ],
    }


def _project_order_fixture(
    *, explicit: bool
) -> tuple[
    list[dict[str, object]],
    list[dict[str, object]],
    dict[UUID, str],
]:
    synthesis = _order_fixture_synthesis()
    synthesis_entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="6" * 64,
        source_stage="synthesis",
        canonical_content=synthesis,
    )
    anchor = next(
        entry
        for entry in synthesis_entries
        if entry.structural_pointer == "/sections/0/paragraphs/1"
    )
    enrichment = _order_fixture_enrichment(anchor.block_id, explicit=explicit)
    enrichment_entries = bootstrap_block_registry(
        artifact_id=uuid4(),
        canonical_sha256="7" * 64,
        source_stage="editorial_enrichment",
        canonical_content=enrichment,
    )
    entries = (*synthesis_entries, *enrichment_entries)
    blocks, enrichments, _outline = project_blocks(
        entries=entries,
        synthesis=synthesis,
        enrichment=enrichment,
        record=_snapshot_record(entries),
    )
    key_by_block_id = {
        entry.block_id: str(
            enrichment[entry.structural_pointer.split("/")[1]][
                int(entry.structural_pointer.split("/")[2])
            ]["key"]
        )
        for entry in enrichment_entries
    }
    return blocks, enrichments, key_by_block_id


def _enrichment_key(block: dict[str, object], keys: dict[UUID, str]) -> str:
    return keys[UUID(str(block["block_id"]))]


def test_snapshot_legacy_order_ignores_figure_order_and_matches_the_pdf() -> None:
    blocks, enrichments, keys = _project_order_fixture(explicit=False)

    anchor_index = next(
        index for index, block in enumerate(blocks) if block.get("text") == "anchor paragraph"
    )
    ordered_keys = [
        _enrichment_key(block, keys)
        for block in blocks[anchor_index + 1 :]
        if block["kind"] in {"table", "figure"}
    ]
    # after_paragraph rank is (family-local index, key): figure_z (index 0)
    # precedes table_anchor (index 0, key after f) which precedes figure_a
    # (index 1). The declared figure.order values are deliberately reversed.
    assert ordered_keys[:3] == ["figure_z", "table_anchor", "figure_a"]
    # after_section rank is (family rank, family-local index, key).
    assert ordered_keys[-2:] == ["table_end", "figure_end"]
    assert [_enrichment_key(block, keys) for block in enrichments] == [
        "figure_z",
        "table_anchor",
        "figure_a",
        "table_end",
        "figure_end",
    ]


def test_snapshot_explicit_group_order_orders_the_after_paragraph_group() -> None:
    blocks, _enrichments, keys = _project_order_fixture(explicit=True)

    anchor_index = next(
        index for index, block in enumerate(blocks) if block.get("text") == "anchor paragraph"
    )
    ordered_keys = [
        _enrichment_key(block, keys)
        for block in blocks[anchor_index + 1 :]
        if block["kind"] in {"table", "figure"}
    ]
    assert ordered_keys[:3] == ["figure_a", "table_anchor", "figure_z"]
    assert ordered_keys[-2:] == ["table_end", "figure_end"]
