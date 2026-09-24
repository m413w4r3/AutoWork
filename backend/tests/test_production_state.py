from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import MAX_ARTIFACT_BYTES
from cti_app.application.production_parsers import (
    reference_report_from_json,
    technical_extraction_from_json,
    validate_synthesis,
)
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_state import (
    MAX_PRODUCTION_STATE_BYTES,
    ProductionStateError,
    ProductionStateService,
    ProductionStateSnapshotV4,
    _exported_repair_block,
    _validate_snapshot,
    compute_production_state_checksum,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    EditionProductionBatchItem,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRun,
    ProductionRunStatus,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from tools.production_state_checksum import canonical_checksum


def _payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": "autowork.production-state",
        "schema_version": 4,
        "exported_at": "2026-08-26T15:00:00Z",
        "origin": {
            "subject_title": "Titre original",
            "subject_id": str(uuid4()),
            "production_run_id": str(uuid4()),
            "research_date": "2026-08-26",
            "discovery_snapshot_id": str(uuid4()),
            "discovery_snapshot_version": 1,
        },
        "artifacts": {
            "references": {
                "input_hash": "a" * 64,
                "canonical_content": {
                    "sources": [
                        {
                            "id": "S1",
                            "title": "Source",
                            "url": "https://example.test/source",
                            "canonical_url": "https://example.test/source",
                        }
                    ],
                    "events": [],
                },
            },
            "extraction": {"input_hash": "b" * 64, "canonical_content": {"items": []}},
            "synthesis": {"input_hash": "c" * 64, "rendered_content": "Fait [S1]"},
        },
        "content_sha256": "0" * 64,
    }
    snapshot = ProductionStateSnapshotV4.model_validate(payload)
    payload["content_sha256"] = compute_production_state_checksum(snapshot)
    return payload


class _FailingFactory:
    def __call__(self) -> Any:
        raise AssertionError("UoW must not be opened for invalid input")


class _ImportUow:
    def __init__(self, current: ProductionRun, item: Any | None) -> None:
        self.production_runs = SimpleNamespace(
            lock_creation_for_subject=AsyncMock(),
            get_current_for_subject=AsyncMock(return_value=current),
            allocate_next_run_number=AsyncMock(return_value=current.run_number + 1),
            add=AsyncMock(),
        )
        self.edition_production_batch_items = SimpleNamespace(
            get_by_run=AsyncMock(return_value=item),
            save=AsyncMock(),
        )
        discovery_subject_id = uuid4()
        self.editions = SimpleNamespace(
            get_for_update=AsyncMock(return_value=SimpleNamespace(state=None)),
            get=AsyncMock(
                return_value=SimpleNamespace(
                    period_start=date(2026, 1, 1), period_end=date(2026, 12, 31)
                )
            ),
        )
        self.subjects = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    id=current.subject_id,
                    edition_id=current.edition_id,
                    title="Imported subject",
                    version=1,
                    tlp=TLP.AMBER,
                )
            )
        )
        self.subject_discovery_origins = SimpleNamespace(
            get_by_subject=AsyncMock(
                return_value=SimpleNamespace(
                    subject_id=current.subject_id,
                    edition_id=current.edition_id,
                    discovery_subject_id=discovery_subject_id,
                    selection_decision_id=uuid4(),
                )
            )
        )
        self.discovery_subject_identities = SimpleNamespace(
            resolve_canonical_subject=AsyncMock(return_value=discovery_subject_id)
        )
        self.discovery_snapshots = SimpleNamespace(
            get_active=AsyncMock(
                return_value=SimpleNamespace(
                    id=uuid4(),
                    version=1,
                    subjects=[
                        SimpleNamespace(
                            subject_id=discovery_subject_id,
                            member_references=(),
                            candidate=SimpleNamespace(summary="Imported subject"),
                        )
                    ],
                )
            )
        )
        self.discovery_candidates = SimpleNamespace(list_for_edition=AsyncMock(return_value=[]))
        self.production_artifacts = SimpleNamespace(append=AsyncMock())
        self.production_input_snapshots = SimpleNamespace(add=AsyncMock())
        self.commit = AsyncMock()

    async def __aenter__(self) -> "_ImportUow":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _ImportFactory:
    def __init__(self, uow: _ImportUow) -> None:
        self.uow = uow

    def __call__(self) -> _ImportUow:
        return self.uow


class _ImportArtifactStore:
    async def store_stage_payloads(
        self,
        *,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[Any, Any, Any]:
        if canonical is not None:
            return None, uuid4(), None
        assert rendered is not None
        return None, None, uuid4()


def _import_service(item: Any | None) -> tuple[ProductionStateService, _ImportUow, UUID, UUID]:
    subject_id = uuid4()
    edition_id = uuid4()
    current = ProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        status=ProductionRunStatus.NEEDS_REVIEW,
    )
    uow = _ImportUow(current, item)
    service = ProductionStateService(_ImportFactory(uow), _ImportArtifactStore())
    return service, uow, subject_id, edition_id


@pytest.mark.asyncio
async def test_import_repoints_existing_batch_item_and_resets_auto_recovery() -> None:
    service, uow, subject_id, edition_id = _import_service(None)
    current_run_id = uow.production_runs.get_current_for_subject.return_value.id
    item = EditionProductionBatchItem(
        batch_id=uuid4(),
        subject_id=subject_id,
        production_run_id=current_run_id,
        position=1,
        auto_recovery_count=1,
    )
    uow.edition_production_batch_items.get_by_run.return_value = item

    result = await service.import_state(
        subject_id=subject_id, edition_id=edition_id, payload=_payload()
    )

    assert item.production_run_id == result.run_id
    assert item.auto_recovery_count == 0
    uow.edition_production_batch_items.save.assert_awaited_once_with(item)
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_import_without_batch_item_succeeds() -> None:
    service, uow, subject_id, edition_id = _import_service(None)

    result = await service.import_state(
        subject_id=subject_id, edition_id=edition_id, payload=_payload()
    )

    assert result.status == "needs_review"
    uow.edition_production_batch_items.get_by_run.assert_awaited_once()
    uow.edition_production_batch_items.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_round_trip_preserves_repair_audit_without_regeneration() -> None:
    payload = _payload()
    payload["origin"]["subject_title"] = "Titre original"
    decision_id = str(uuid4())
    base_id = str(uuid4())
    materialization = {
        "planner_version": "33.1",
        "impact_kind": "publication_only",
        "affected_outputs": ["extraction", "publication", "checkpoint"],
        "model_call_required": False,
        "decision_ids": [decision_id],
        "base_extraction_artifact_id": base_id,
        "result_extraction_artifact_id": str(uuid4()),
        "reused_synthesis_artifact_id": str(uuid4()),
        "result_publication_artifact_id": str(uuid4()),
    }
    payload["repair"] = {
        "projection_version": "33.1",
        "base_extraction_artifact_id": base_id,
        "included_repair_keys": ["d" * 64],
        "decisions": [
            {
                "repair_key": "d" * 64,
                "decision_id": decision_id,
                "issue_kind": "rejected_indicator",
                "action": "include",
                "actor_id": "analyst",
                "decided_at": "2026-08-26T15:00:00Z",
                "reason": "validated",
            }
        ],
        "materialization": materialization,
    }
    payload["content_sha256"] = compute_production_state_checksum(
        ProductionStateSnapshotV4.model_validate(payload)
    )
    service, uow, subject_id, edition_id = _import_service(None)

    await service.import_state(subject_id=subject_id, edition_id=edition_id, payload=payload)

    extraction = uow.production_artifacts.append.await_args_list[1].args[0]
    imported_audit = extraction.metadata["imported_repair_audit"]
    assert imported_audit["decisions"][0]["decision_id"] == decision_id
    assert extraction.metadata["repair_materialization"] == materialization

    exported = _exported_repair_block(extraction.metadata, {})
    assert exported is not None
    assert exported.materialization == materialization
    assert exported.decisions[0].decision_id == decision_id


def test_checksum_tool_repairs_edited_snapshot() -> None:
    payload = _payload()
    payload["artifacts"]["synthesis"]["rendered_content"] = "Fait [S1] corrigé par l'analyste"
    payload["content_sha256"] = canonical_checksum(payload)

    snapshot = _validate_snapshot(payload)

    assert snapshot.content_sha256 == compute_production_state_checksum(snapshot)


class _ExportStore:
    """The minimal blob catalogue an export needs: JSON plus text."""

    def __init__(self) -> None:
        self.json: dict[UUID, dict[str, Any]] = {}
        self.text: dict[UUID, str] = {}

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.json[blob_id]

    async def read_text(self, blob_id: UUID) -> str:
        return self.text[blob_id]

    async def store_stage_payloads(
        self,
        *,
        raw: str | None = None,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        raw_id = uuid4() if raw is not None else None
        canonical_id = uuid4() if canonical is not None else None
        rendered_id = uuid4() if rendered is not None else None
        if raw_id is not None:
            self.text[raw_id] = raw if raw is not None else ""
        if canonical_id is not None:
            self.json[canonical_id] = canonical if canonical is not None else {}
        if rendered_id is not None:
            self.text[rendered_id] = rendered if rendered is not None else ""
        return raw_id, canonical_id, rendered_id


class _ExportUow:
    def __init__(self, run: Any, snapshot: Any, artifacts: dict[str, Any]) -> None:
        self.production_runs = SimpleNamespace(get=AsyncMock(return_value=run))
        self.production_input_snapshots = SimpleNamespace(
            get_by_run=AsyncMock(return_value=snapshot)
        )
        self.production_artifacts = SimpleNamespace(
            get_current=AsyncMock(side_effect=lambda _run_id, stage: artifacts.get(stage))
        )

    async def __aenter__(self) -> "_ExportUow":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


def _corpus_state_artifacts(
    store: _ExportStore,
    *,
    run_id: UUID,
    subject_id: UUID,
) -> tuple[ProductionArtifact, dict[str, Any]]:
    """One AW-010 run: a canonical corpus, its RAW and a legacy projection."""
    raw = """# REFERENCES
editorial-title: [Publication] Titre
## SOURCE S1
title: Source
url: https://example.test/source
publisher: Publisher
published-at: 2026-08-20
role: independent
kind: publication
reason: Documents the campaign
## EVENT R1
date: 2026-08-21
sources: S1
text: Shared fact
"""
    corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=date(2026, 8, 26),
        production_input_hash="a" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url="https://example.test/source",
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.INDEPENDENT,
                title="Source",
                publisher="Publisher",
                published_at=date(2026, 8, 20),
                source_collection_id=uuid4(),
                source_document_id=uuid4(),
                discovery_candidate_ids=(uuid4(),),
                collection_state=CollectionState.ARCHIVED,
                content_sha256="b" * 64,
                relevance_reason=None,
                proposed_by_model=False,
                eligible_for_extraction=True,
            ),
        ),
        warnings=(),
    )
    raw_id = uuid4()
    canonical_id = uuid4()
    store.text[raw_id] = raw
    store.json[canonical_id] = production_reference_corpus_to_json(corpus)
    return (
        ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.REFERENCES,
            version=1,
            input_hash="a" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            raw_blob_id=raw_id,
            canonical_blob_id=canonical_id,
        ),
        {"raw": raw, "corpus": corpus},
    )


@pytest.mark.asyncio
async def test_corpus_export_projects_v4_references_and_import_stays_legacy() -> None:
    """AW-010: a corpus exports as the V4 references contract, never as itself."""
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        status=ProductionRunStatus.NEEDS_REVIEW,
    )
    store = _ExportStore()
    references, _ = _corpus_state_artifacts(store, run_id=run.id, subject_id=run.subject_id)
    extraction = await store.store_stage_payloads(canonical={"items": []})
    synthesis = await store.store_stage_payloads(rendered="Fait [S1]")
    artifacts = {
        "references": references,
        ProductionArtifactStage.EXTRACTION.value: ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash="b" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=extraction[1],
        ),
        ProductionArtifactStage.SYNTHESIS.value: ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="c" * 64,
            status=ProductionArtifactStatus.VERIFIED,
            rendered_blob_id=synthesis[2],
        ),
    }
    snapshot = SimpleNamespace(
        subject_title="Titre original",
        subject_id=run.subject_id,
        research_date=date(2026, 8, 26),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
    )
    service = ProductionStateService(
        cast(Any, lambda: _ExportUow(run, snapshot, artifacts)), cast(Any, store)
    )

    exported = await service.export_run_state(run.id)

    refs_content = exported.artifacts.references.canonical_content
    # V4 keeps its historical contract: a projected ReferenceReport, not the corpus.
    assert "production_input_hash" not in refs_content
    assert "tier" not in refs_content["sources"][0]
    assert [source["id"] for source in refs_content["sources"]] == ["S1"]
    assert refs_content["editorial_title"] == "[Publication] Titre"

    _, uow, subject_id, edition_id = _import_service(None)
    imported_service = ProductionStateService(cast(Any, _ImportFactory(uow)), cast(Any, store))
    result = await imported_service.import_state(
        subject_id=subject_id,
        edition_id=edition_id,
        payload=exported.model_dump(mode="json"),
    )

    imported_references = uow.production_artifacts.append.await_args_list[0].args[0]
    assert imported_references.metadata["legacy_reference_report"] is True
    imported_content = store.json[imported_references.canonical_blob_id]
    report = reference_report_from_json(imported_content)
    assert [source.local_id for source in report.sources] == ["S1"]
    # The import never fabricates the collection identity the report lacks.
    assert "tier" not in imported_content["sources"][0]
    assert "collection_state" not in imported_content["sources"][0]
    assert "source_document_id" not in imported_content["sources"][0]
    assert "content_sha256" not in imported_content["sources"][0]
    # Assembly-compatible: the synthesis validates against the imported report.
    assert validate_synthesis(
        exported.artifacts.synthesis.rendered_content,
        report,
        technical_extraction_from_json(exported.artifacts.extraction.canonical_content),
    ).usable
    assert result.status == "needs_review"


@pytest.mark.asyncio
async def test_import_accepts_v4_checksum_and_rejects_unknown_fields() -> None:
    payload = _payload()
    snapshot = ProductionStateSnapshotV4.model_validate(payload)
    assert snapshot.content_sha256 == compute_production_state_checksum(snapshot)

    payload["unexpected"] = True
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("format", "other", "production_state_invalid_format"),
        ("schema_version", 2, "production_state_version_unsupported"),
    ],
)
async def test_import_rejects_format_and_version_before_uow(
    field: str, value: Any, code: str
) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_import_rejects_bad_checksum_without_side_effects() -> None:
    payload = _payload()
    payload["content_sha256"] = "d" * 64
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_checksum_mismatch"


def test_checksum_is_deterministic_and_excludes_checksum_field() -> None:
    payload = _payload()
    snapshot = ProductionStateSnapshotV4.model_validate(payload)
    changed = snapshot.model_copy(update={"content_sha256": "e" * 64})
    assert compute_production_state_checksum(snapshot) == compute_production_state_checksum(changed)


def test_snapshot_limits_are_defined() -> None:
    assert MAX_ARTIFACT_BYTES < MAX_PRODUCTION_STATE_BYTES
    assert datetime.now(UTC).tzinfo is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ("references", "extraction", "synthesis"))
async def test_import_rejects_each_oversized_artifact_before_creating_a_run(artifact: str) -> None:
    payload = _payload()
    if artifact == "synthesis":
        payload["artifacts"][artifact]["rendered_content"] = "x" * (MAX_ARTIFACT_BYTES + 1)
    else:
        payload["artifacts"][artifact]["canonical_content"]["padding"] = "x" * (
            MAX_ARTIFACT_BYTES + 1
        )
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_too_large"


@pytest.mark.asyncio
async def test_import_rejects_oversized_snapshot_before_creating_a_run() -> None:
    payload = _payload()
    payload["origin"]["subject_title"] = "x" * (MAX_PRODUCTION_STATE_BYTES + 1)
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_too_large"
