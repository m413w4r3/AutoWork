import hashlib
from copy import deepcopy
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import MAX_ARTIFACT_BYTES
from cti_app.application.production_extraction import references_corpus_hash
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_state import (
    MAX_PRODUCTION_STATE_BYTES,
    ProductionStateError,
    ProductionStateService,
    ProductionStateSnapshotV5,
    _exported_repair_block,
    _validate_snapshot,
    compute_production_state_checksum,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.application.subject_production import capture_production_input_snapshot
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    EditionProductionBatchItem,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionRun,
    ProductionRunStatus,
)
from cti_app.domain.production_editorial_enrichment import (
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    EnrichmentTableKind,
    TableColumnV1,
    TableRowV1,
    TableSpecV1,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionFactV1,
    ExtractionReuseState,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)
from cti_app.domain.production_synthesis import (
    PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
    SYNTHESIS_POLICY_VERSION,
    ProductionSynthesisV1,
    SynthesisParagraphV1,
    SynthesisSectionKind,
    SynthesisSectionV1,
    extraction_evidence_refs_v1,
    production_synthesis_to_json,
)
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tools.production_state_checksum import canonical_checksum


def canonical_production_state_artifacts(
    subject_id: UUID, production_input_hash: str = "a" * 64
) -> dict[str, dict[str, Any]]:
    """Build a portable canonical chain with matching AW-015 lineage."""
    document_id = uuid4()
    corpus = ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=subject_id,
        research_date=date(2026, 8, 26),
        production_input_hash=production_input_hash,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=(
            ProductionReferenceSourceV1(
                canonical_url="https://example.test/source",
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                title="Source",
                publisher="Publisher",
                published_at=date(2026, 8, 20),
                source_collection_id=uuid4(),
                source_document_id=document_id,
                discovery_candidate_ids=(uuid4(),),
                collection_state=CollectionState.ARCHIVED,
                content_sha256="b" * 64,
                relevance_reason="Documents the campaign",
                proposed_by_model=False,
                eligible_for_extraction=True,
            ),
        ),
        warnings=(),
    )
    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash=production_input_hash,
        references_corpus_hash=references_corpus_hash(corpus),
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=(
            ProductionSourceExtractionV1(
                source_document_id=document_id,
                canonical_url="https://example.test/source",
                content_sha256="b" * 64,
                tier=ProductionReferenceTier.CORE,
                kind=ProductionReferenceKind.PUBLICATION,
                role=SourceRole.PRIMARY,
                profile=ExtractionProfile.FULL,
                checkpoint_id=None,
                reuse_state=ExtractionReuseState.FRESH,
                facts=(
                    ExtractionFactV1(
                        category="malware",
                        value="ExampleRAT",
                        attack_id=None,
                        context="",
                        evidence_quote="ExampleRAT",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(document_id,),
                    ),
                    ExtractionFactV1(
                        category="campaigns",
                        value="Example campaign",
                        attack_id=None,
                        context="",
                        evidence_quote="Example campaign",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                        source_document_ids=(document_id,),
                    ),
                ),
                events=(),
                indicators=(),
                rules=(),
                uncertainties=(),
            ),
        ),
        omitted_sources=(),
        warnings=(),
    )
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    paragraph = SynthesisParagraphV1("ExampleRAT activity is documented.", (evidence_ref,))
    synthesis = ProductionSynthesisV1(
        schema_version=PRODUCTION_SYNTHESIS_SCHEMA_VERSION,
        subject_id=subject_id,
        production_input_hash=production_input_hash,
        extraction_hash=canonical_extraction_hash(extraction),
        publication_language="fr",
        synthesis_policy_version=SYNTHESIS_POLICY_VERSION,
        title="Example report",
        lead=(paragraph,),
        sections=(
            SynthesisSectionV1(
                kind=SynthesisSectionKind.OVERVIEW,
                heading="Overview",
                paragraphs=(paragraph,),
            ),
        ),
        timeline=(),
        uncertainties=(),
        warnings=(),
    )
    enrichment = replace(
        build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis),
        tables=(
            TableSpecV1(
                key="facts",
                kind=EnrichmentTableKind.CUSTOM,
                title="Evidence",
                caption=None,
                columns=(TableColumnV1("fact", "Fact"), TableColumnV1("context", "Context")),
                rows=(
                    TableRowV1(
                        cells=("ExampleRAT", "Observed in the report"),
                        evidence_refs=(extraction_evidence_refs_v1(extraction)[0],),
                    ),
                ),
                placement=EnrichmentPlacementV1(EnrichmentPlacementKind.AFTER_LEAD),
            ),
        ),
    )
    return {
        "references": production_reference_corpus_to_json(corpus),
        "extraction": production_extraction_to_json(extraction),
        "synthesis": production_synthesis_to_json(synthesis),
        "editorial_enrichment": editorial_enrichment_to_json(enrichment),
    }


def _payload(
    subject_id: UUID | None = None,
    *,
    production_input_hash: str = "a" * 64,
    subject_title: str = "Imported subject",
    discovery_snapshot_id: UUID | None = None,
    discovery_snapshot_version: int = 1,
) -> dict[str, Any]:
    subject_id = subject_id or uuid4()
    canonical = canonical_production_state_artifacts(subject_id, production_input_hash)
    payload: dict[str, Any] = {
        "format": "autowork.production-state",
        "schema_version": 5,
        "exported_at": "2026-08-26T15:00:00Z",
        "origin": {
            "subject_title": subject_title,
            "subject_id": str(subject_id),
            "production_run_id": str(uuid4()),
            "research_date": "2026-08-26",
            "discovery_snapshot_id": str(discovery_snapshot_id or uuid4()),
            "discovery_snapshot_version": discovery_snapshot_version,
        },
        "artifacts": {
            "references": {"input_hash": "c" * 64, "canonical_content": canonical["references"]},
            "extraction": {"input_hash": "d" * 64, "canonical_content": canonical["extraction"]},
            "synthesis": {"input_hash": "e" * 64, "canonical_content": canonical["synthesis"]},
            "editorial_enrichment": {
                "input_hash": "f" * 64,
                "canonical_content": canonical["editorial_enrichment"],
            },
        },
        "content_sha256": "0" * 64,
    }
    snapshot = ProductionStateSnapshotV5.model_validate(payload)
    payload["content_sha256"] = compute_production_state_checksum(snapshot)
    return payload


class _FailingFactory:
    def __call__(self) -> Any:
        raise AssertionError("UoW must not be opened for invalid input")


class _ImportUow:
    def __init__(
        self, current: ProductionRun, item: Any | None, *, subject_title: str = "Imported subject"
    ) -> None:
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
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 12, 31),
                    languages=("fr",),
                )
            ),
        )
        self.subjects = SimpleNamespace(
            get=AsyncMock(
                return_value=SimpleNamespace(
                    id=current.subject_id,
                    edition_id=current.edition_id,
                    title=subject_title,
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
        self.discovery_snapshot = SimpleNamespace(
            id=uuid4(),
            version=1,
            subjects=[
                SimpleNamespace(
                    subject_id=discovery_subject_id,
                    member_references=(),
                    candidate=SimpleNamespace(summary=subject_title),
                )
            ],
        )
        self.discovery_snapshots = SimpleNamespace(
            get_active=AsyncMock(return_value=self.discovery_snapshot)
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


def _import_service(
    item: Any | None,
    *,
    subject_id: UUID | None = None,
    subject_title: str = "Imported subject",
) -> tuple[ProductionStateService, _ImportUow, UUID, UUID]:
    subject_id = subject_id or uuid4()
    edition_id = uuid4()
    current = ProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        status=ProductionRunStatus.NEEDS_REVIEW,
    )
    uow = _ImportUow(current, item, subject_title=subject_title)
    service = ProductionStateService(_ImportFactory(uow), _ImportArtifactStore())
    return service, uow, subject_id, edition_id


async def _payload_for_import(
    uow: _ImportUow, *, subject_id: UUID, edition_id: UUID
) -> dict[str, Any]:
    local = await capture_production_input_snapshot(
        uow,
        production_run_id=uuid4(),
        subject_id=subject_id,
        edition_id=edition_id,
        research_date=date(2026, 8, 26),
        captured_at=datetime.now(UTC),
    )
    return _payload(
        subject_id,
        production_input_hash=local.input_hash,
        subject_title=local.subject_title,
        discovery_snapshot_id=local.discovery_snapshot_id,
        discovery_snapshot_version=local.discovery_snapshot_version,
    )


@pytest.mark.asyncio
async def test_import_repoints_existing_batch_item_and_resets_auto_recovery() -> None:
    subject_id = uuid4()
    service, uow, subject_id, edition_id = _import_service(None, subject_id=subject_id)
    payload = await _payload_for_import(uow, subject_id=subject_id, edition_id=edition_id)
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
        subject_id=subject_id, edition_id=edition_id, payload=payload
    )

    assert result.current_stage == "relevance_projection"
    assert item.production_run_id == result.run_id
    assert item.auto_recovery_count == 0
    uow.edition_production_batch_items.save.assert_awaited_once_with(item)
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_import_without_batch_item_succeeds() -> None:
    subject_id = uuid4()
    service, uow, subject_id, edition_id = _import_service(None, subject_id=subject_id)
    payload = await _payload_for_import(uow, subject_id=subject_id, edition_id=edition_id)

    result = await service.import_state(
        subject_id=subject_id, edition_id=edition_id, payload=payload
    )

    assert result.status == "needs_review"
    uow.edition_production_batch_items.get_by_run.assert_awaited_once()
    uow.edition_production_batch_items.save.assert_not_awaited()


@pytest.mark.asyncio
async def test_import_round_trip_preserves_repair_audit_without_regeneration() -> None:
    subject_id = uuid4()
    service, uow, subject_id, edition_id = _import_service(None, subject_id=subject_id)
    payload = await _payload_for_import(uow, subject_id=subject_id, edition_id=edition_id)
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
        ProductionStateSnapshotV5.model_validate(payload)
    )
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
    payload["artifacts"]["editorial_enrichment"]["canonical_content"]["warnings"].append(
        "Reviewed by analyst"
    )
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


@pytest.mark.asyncio
async def test_v5_export_preserves_the_four_canonical_artifacts() -> None:
    """V5 exports a directly portable canonical pre-Assembly chain."""
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        status=ProductionRunStatus.NEEDS_REVIEW,
    )
    import_uow = _ImportUow(run, None)
    local_snapshot = await capture_production_input_snapshot(
        import_uow,
        production_run_id=run.id,
        subject_id=run.subject_id,
        edition_id=run.edition_id,
        research_date=date(2026, 8, 26),
        captured_at=datetime.now(UTC),
    )
    store = _ExportStore()
    canonical = canonical_production_state_artifacts(run.subject_id, local_snapshot.input_hash)
    artifacts: dict[str, ProductionArtifact] = {}
    for stage_name, stage in (
        ("references", ProductionArtifactStage.REFERENCES),
        ("extraction", ProductionArtifactStage.EXTRACTION),
        ("synthesis", ProductionArtifactStage.SYNTHESIS),
        ("editorial_enrichment", ProductionArtifactStage.EDITORIAL_ENRICHMENT),
    ):
        _, canonical_blob_id, _ = await store.store_stage_payloads(canonical=canonical[stage_name])
        assert canonical_blob_id is not None
        artifacts[stage_name] = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=1,
            input_hash=hashlib.sha256(stage_name.encode()).hexdigest(),
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=canonical_blob_id,
        )
    service = ProductionStateService(
        cast(Any, lambda: _ExportUow(run, local_snapshot, artifacts)), cast(Any, store)
    )

    exported = await service.export_run_state(run.id)

    assert exported.schema_version == 5
    for stage_name in ("references", "extraction", "synthesis", "editorial_enrichment"):
        assert getattr(exported.artifacts, stage_name).canonical_content == canonical[stage_name]

    result = await ProductionStateService(
        cast(Any, _ImportFactory(import_uow)), cast(Any, store)
    ).import_state(
        subject_id=run.subject_id,
        edition_id=run.edition_id,
        payload=exported.model_dump(mode="json"),
    )

    imported_artifacts = [
        call.args[0] for call in import_uow.production_artifacts.append.await_args_list
    ]
    assert [artifact.stage.value for artifact in imported_artifacts] == [
        "references",
        "extraction",
        "synthesis",
        "editorial_enrichment",
    ]
    assert all(
        artifact.status is ProductionArtifactStatus.VERIFIED for artifact in imported_artifacts
    )
    assert result.status == "needs_review"


@pytest.mark.asyncio
async def test_import_accepts_v5_checksum_and_rejects_unknown_fields() -> None:
    payload = _payload()
    snapshot = ProductionStateSnapshotV5.model_validate(payload)
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
        ("schema_version", 4, "production_state_version_unsupported"),
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
    snapshot = ProductionStateSnapshotV5.model_validate(payload)
    changed = snapshot.model_copy(update={"content_sha256": "e" * 64})
    assert compute_production_state_checksum(snapshot) == compute_production_state_checksum(changed)


@pytest.mark.parametrize(
    ("artifact", "field", "value"),
    (
        ("references", "subject_id", str(uuid4())),
        ("extraction", "references_corpus_hash", "9" * 64),
        ("extraction", "production_input_hash", "9" * 64),
        ("synthesis", "extraction_hash", "9" * 64),
        ("synthesis", "production_input_hash", "9" * 64),
        ("editorial_enrichment", "extraction_hash", "9" * 64),
        ("editorial_enrichment", "synthesis_hash", "9" * 64),
    ),
)
def test_snapshot_rejects_canonical_artifacts_with_broken_lineage(
    artifact: str, field: str, value: str
) -> None:
    payload = _payload()
    payload["artifacts"][artifact]["canonical_content"][field] = value
    payload["content_sha256"] = canonical_checksum(payload)

    with pytest.raises(ProductionStateError) as exc_info:
        _validate_snapshot(payload)

    assert exc_info.value.code == "production_state_invalid"


def test_snapshot_rejects_enrichment_with_unknown_evidence_reference() -> None:
    payload = _payload()
    reference = payload["artifacts"]["editorial_enrichment"]["canonical_content"]["tables"][0][
        "rows"
    ][0]["evidence_refs"][0]
    reference["evidence_key"] = "9" * 64
    payload["content_sha256"] = canonical_checksum(payload)

    with pytest.raises(ProductionStateError) as exc_info:
        _validate_snapshot(payload)

    assert exc_info.value.code == "production_state_invalid"


def test_checksum_covers_enrichment_cells_evidence_and_input_hash() -> None:
    payload = _payload()
    original = compute_production_state_checksum(payload)

    edited_cell = deepcopy(payload)
    edited_cell["artifacts"]["editorial_enrichment"]["canonical_content"]["tables"][0]["rows"][0][
        "cells"
    ][0] = "ExampleRAT revised"
    changed_cell_checksum = compute_production_state_checksum(edited_cell)

    edited_evidence = deepcopy(payload)
    refs = edited_evidence["artifacts"]["editorial_enrichment"]["canonical_content"]["tables"][0][
        "rows"
    ][0]["evidence_refs"]
    refs[0]["evidence_key"] = "8" * 64
    changed_evidence_checksum = compute_production_state_checksum(edited_evidence)

    edited_input_hash = deepcopy(payload)
    edited_input_hash["artifacts"]["editorial_enrichment"]["input_hash"] = "9" * 64
    changed_input_hash_checksum = compute_production_state_checksum(edited_input_hash)

    assert len({original, changed_cell_checksum, changed_evidence_checksum}) == 3
    assert changed_input_hash_checksum != original


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fault", "expected_code"),
    (
        ("missing_enrichment", "production_state_incomplete"),
        ("stale_enrichment", "production_state_unverified"),
        ("missing_enrichment_blob", "production_state_incomplete"),
        ("rendered_only_synthesis", "production_state_incomplete"),
    ),
)
async def test_export_requires_verified_canonical_enrichment_and_synthesis(
    fault: str, expected_code: str
) -> None:
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        status=ProductionRunStatus.NEEDS_REVIEW,
    )
    store = _ExportStore()
    canonical = canonical_production_state_artifacts(run.subject_id)
    stages = (
        ("references", ProductionArtifactStage.REFERENCES),
        ("extraction", ProductionArtifactStage.EXTRACTION),
        ("synthesis", ProductionArtifactStage.SYNTHESIS),
        ("editorial_enrichment", ProductionArtifactStage.EDITORIAL_ENRICHMENT),
    )
    artifacts: dict[str, ProductionArtifact] = {}
    for stage_name, stage in stages:
        if fault == "missing_enrichment" and stage_name == "editorial_enrichment":
            continue
        _, canonical_blob_id, _ = await store.store_stage_payloads(canonical=canonical[stage_name])
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=stage,
            version=1,
            input_hash=hashlib.sha256(stage_name.encode()).hexdigest(),
            status=ProductionArtifactStatus.VERIFIED,
            canonical_blob_id=canonical_blob_id,
        )
        if fault == "stale_enrichment" and stage_name == "editorial_enrichment":
            artifact.status = ProductionArtifactStatus.STALE
        if fault == "missing_enrichment_blob" and stage_name == "editorial_enrichment":
            artifact.canonical_blob_id = None
        if fault == "rendered_only_synthesis" and stage_name == "synthesis":
            artifact.canonical_blob_id = None
            artifact.rendered_blob_id = uuid4()
        artifacts[stage_name] = artifact
    local_snapshot = SimpleNamespace(
        subject_title="Titre original",
        subject_id=run.subject_id,
        research_date=date(2026, 8, 26),
        discovery_snapshot_id=uuid4(),
        discovery_snapshot_version=1,
    )
    service = ProductionStateService(
        cast(Any, lambda: _ExportUow(run, local_snapshot, artifacts)), cast(Any, store)
    )

    with pytest.raises(ProductionStateError) as exc_info:
        await service.export_run_state(run.id)

    assert exc_info.value.code == expected_code


def test_snapshot_limits_are_defined() -> None:
    assert MAX_ARTIFACT_BYTES < MAX_PRODUCTION_STATE_BYTES
    assert datetime.now(UTC).tzinfo is not None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "artifact", ("references", "extraction", "synthesis", "editorial_enrichment")
)
async def test_import_rejects_each_oversized_artifact_before_creating_a_run(artifact: str) -> None:
    payload = _payload()
    payload["artifacts"][artifact]["canonical_content"]["padding"] = "x" * (MAX_ARTIFACT_BYTES + 1)
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


@pytest.mark.asyncio
async def test_v5_rejects_the_old_rendered_only_synthesis_and_missing_enrichment() -> None:
    """The historical V4 shape cannot masquerade as a V5 assembly checkpoint."""
    payload = _payload()
    synthesis = payload["artifacts"]["synthesis"]
    synthesis.pop("canonical_content")
    synthesis["rendered_content"] = "Fait [S1]"
    payload["artifacts"].pop("editorial_enrichment")
    payload["content_sha256"] = canonical_checksum(payload)

    with pytest.raises(ProductionStateError) as exc_info:
        _validate_snapshot(payload)

    assert exc_info.value.code == "production_state_invalid"
