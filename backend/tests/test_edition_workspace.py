from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_workspace import (
    EditionProductionCheckpointService,
    EditionWorkspaceMaterializer,
    safe_slug,
)
from cti_app.application.production_state import ProductionStateError, ProductionStateSnapshotV1
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.production import (
    EditionProductionBatchItem,
    ProductionProfile,
    SubjectProductionRun,
)


def _state(
    *,
    reference_hash: str = "a" * 64,
    extraction_hash: str = "b" * 64,
    synthesis_hash: str = "c" * 64,
) -> ProductionStateSnapshotV1:
    return ProductionStateSnapshotV1.model_validate(
        {
            "format": "autowork.production-state",
            "schema_version": 1,
            "exported_at": "2026-08-29T10:00:00Z",
            "origin": {
                "subject_title": "Sujet / à vérifier",
                "editorial_type": "brief",
                "profile": "brief_auto",
                "research_date": "2026-08-01",
            },
            "artifacts": {
                "references": {"input_hash": reference_hash, "canonical_content": {"items": []}},
                "extraction": {
                    "input_hash": extraction_hash,
                    "canonical_content": {"items": []},
                },
                "synthesis": {"input_hash": synthesis_hash, "rendered_content": "Article"},
            },
            "content_sha256": "d" * 64,
        }
    )


@pytest.mark.asyncio
async def test_edition_layout_is_deterministic_and_does_not_copy_sample_bytes(
    tmp_path: Path,
) -> None:
    materializer = EditionWorkspaceMaterializer(tmp_path / "editions")
    state = _state()
    first = await materializer.materialize(
        edition_id=UUID("11111111-1111-4111-8111-111111111111"),
        period=date(2026, 8, 1),
        country_code="fr",
        position=2,
        subject_id=UUID("22222222-2222-4222-8222-222222222222"),
        subject_title="Sujet ../../extérieur",
        production_state=state,
        publication={"schema_version": "1", "title": "Sujet"},
        rendered_content="# Sujet\n",
        assets=({"id": "sample-1", "blob_id": "blob-1", "size": "999"},),
    )
    second = await materializer.materialize(
        edition_id=UUID("11111111-1111-4111-8111-111111111111"),
        period=date(2026, 8, 1),
        country_code="FR",
        position=2,
        subject_id=UUID("22222222-2222-4222-8222-222222222222"),
        subject_title="Sujet ../../extérieur",
        production_state=state,
        publication={"schema_version": "1", "title": "Sujet"},
        rendered_content="# Sujet\n",
        assets=({"id": "sample-1", "blob_id": "blob-1", "size": "999"},),
    )

    assert first.item_path == second.item_path
    assert (first.path / "manifest.json").exists()
    manifest = json.loads((first.path / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["canonical"] is False
    assert manifest["edition_id"] == "11111111-1111-4111-8111-111111111111"
    assert manifest["period"] == "2026-08"
    assert manifest["country_code"] == "FR"
    saved_state = json.loads(
        (first.item_path / "pipeline/production-state.json").read_text(encoding="utf-8")
    )
    assert saved_state == state.model_dump(mode="json")
    assert not (first.item_path / "assets/sample-1").exists()
    assert not (tmp_path / "outside").exists()
    assert list(first.item_path.glob("**/*.tmp")) == []

    without_rendered = await materializer.materialize(
        edition_id=UUID("11111111-1111-4111-8111-111111111111"),
        period=date(2026, 8, 1),
        country_code="FR",
        position=3,
        subject_id=uuid4(),
        subject_title="Sans rendu",
        production_state=state,
        publication={"schema_version": "1", "title": "Sans rendu"},
    )
    assert (without_rendered.item_path / "article/publication.json").exists()
    assert not (without_rendered.item_path / "article/publication.md").exists()


def test_safe_slug_is_bounded_and_traversal_cannot_become_a_path() -> None:
    assert safe_slug("../../outside/" + "x" * 200) == "outside-" + "x" * 72


@pytest.mark.asyncio
async def test_invalid_country_path_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="country code"):
        await EditionWorkspaceMaterializer(tmp_path).materialize(
            edition_id=uuid4(),
            period=date(2026, 8, 1),
            country_code="../outside",
            position=1,
            subject_id=uuid4(),
            subject_title="Sujet",
            production_state=_state(),
        )


class _Artifacts:
    async def get_current(self, run_id: UUID, stage: str) -> None:
        return None


class _Runs:
    def __init__(self, run: SubjectProductionRun) -> None:
        self.run = run

    async def get(self, run_id: UUID) -> SubjectProductionRun | None:
        return self.run if run_id == self.run.id else None


class _Items:
    def __init__(self, item: EditionProductionBatchItem) -> None:
        self.item = item

    async def get_by_run(self, run_id: UUID) -> EditionProductionBatchItem | None:
        return self.item if run_id == self.item.production_run_id else None


class _Editions:
    def __init__(self, edition: Edition) -> None:
        self.edition = edition

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.edition if edition_id == self.edition.id else None


class _Snapshots:
    def __init__(self, title: str, run_id: UUID) -> None:
        self.title = title
        self.run_id = run_id

    async def get_by_run(self, run_id: UUID) -> Any:
        return (
            type("Snapshot", (), {"subject_title": self.title})() if run_id == self.run_id else None
        )


class _Uow:
    def __init__(
        self, run: SubjectProductionRun, edition: Edition, item: EditionProductionBatchItem
    ) -> None:
        self.subject_production_runs = _Runs(run)
        self.edition_production_batch_items = _Items(item)
        self.editions = _Editions(edition)
        self.production_input_snapshots = _Snapshots("Sujet", run.id)
        self.production_artifacts = _Artifacts()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None


class _State:
    def __init__(
        self,
        *,
        exact_state: ProductionStateSnapshotV1 | None = None,
        current_state: ProductionStateSnapshotV1 | None = None,
    ) -> None:
        self.exact_run_ids: list[UUID] = []
        self.exact_state = exact_state or _state()
        self.current_state = current_state or _state()

    async def export_state(
        self, *, subject_id: UUID, subject_title: str
    ) -> ProductionStateSnapshotV1:
        return self.current_state

    async def export_run_state(
        self, run_id: UUID, *, subject_title: str
    ) -> ProductionStateSnapshotV1:
        self.exact_run_ids.append(run_id)
        return self.exact_state


class _IncompleteState(_State):
    async def export_run_state(
        self, run_id: UUID, *, subject_title: str
    ) -> ProductionStateSnapshotV1:
        raise ProductionStateError(
            code="production_state_incomplete", message="Artifacts are not complete"
        )


class _BrokenMaterializer:
    async def materialize(self, **kwargs: object) -> None:
        raise PermissionError("workspace denied")


@pytest.mark.asyncio
async def test_filesystem_error_is_best_effort_and_returns_no_failure(tmp_path: Path) -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        profile=ProductionProfile.BRIEF_AUTO,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_major_articles=0,
        target_briefs=1,
        source_profile="default",
        id=run.edition_id,
    )
    item = EditionProductionBatchItem(
        batch_id=uuid4(), subject_id=run.subject_id, production_run_id=run.id, position=1
    )
    service = EditionProductionCheckpointService(
        lambda: _Uow(run, edition, item),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        tmp_path,
        materializer=_BrokenMaterializer(),  # type: ignore[arg-type]
        state_service=_State(),  # type: ignore[arg-type]
    )

    assert await service.checkpoint(run.id) is None


@pytest.mark.asyncio
async def test_checkpoint_exports_the_requested_run_exactly(tmp_path: Path) -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        profile=ProductionProfile.BRIEF_AUTO,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_major_articles=0,
        target_briefs=1,
        source_profile="default",
        id=run.edition_id,
    )
    item = EditionProductionBatchItem(
        batch_id=uuid4(), subject_id=run.subject_id, production_run_id=run.id, position=1
    )
    state = _State(
        exact_state=_state(reference_hash="a" * 64),
        current_state=_state(reference_hash="b" * 64),
    )
    service = EditionProductionCheckpointService(
        lambda: _Uow(run, edition, item),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        tmp_path,
        state_service=state,  # type: ignore[arg-type]
    )

    result = await service.checkpoint(run.id)

    assert result is not None
    assert state.exact_run_ids == [run.id]
    saved = json.loads(
        (result.item_path / "pipeline/production-state.json").read_text(encoding="utf-8")
    )
    assert saved["artifacts"]["references"]["input_hash"] == "a" * 64


@pytest.mark.asyncio
async def test_non_exportable_checkpoint_has_no_failure_diagnostic(tmp_path: Path) -> None:
    run = SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        profile=ProductionProfile.BRIEF_AUTO,
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
    )
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_major_articles=0,
        target_briefs=1,
        source_profile="default",
        id=run.edition_id,
    )
    item = EditionProductionBatchItem(
        batch_id=uuid4(), subject_id=run.subject_id, production_run_id=run.id, position=1
    )
    service = EditionProductionCheckpointService(
        lambda: _Uow(run, edition, item),  # type: ignore[arg-type]
        object(),  # type: ignore[arg-type]
        tmp_path,
        state_service=_IncompleteState(),  # type: ignore[arg-type]
    )

    assert await service.checkpoint(run.id) is None
