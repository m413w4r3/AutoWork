# ruff: noqa: E501

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.goodware import GoodwareBaseline, GoodwareSource
from cti_app.infrastructure.goodware_stage import load_stage


class GoodwareService:
    def __init__(self, blobs: BlobCatalogService, uow_factory: UnitOfWorkFactory) -> None:
        self._blobs, self._uow_factory = blobs, uow_factory

    async def import_stage(self, stage_dir: Path, source_dir: Path) -> GoodwareBaseline:
        stage = load_stage(stage_dir, source_dir)
        source_rows: list[GoodwareSource] = []
        for entry in stage.manifest["sources"]:
            path = source_dir / entry["filename"]
            with path.open("rb") as handle:
                blob = await self._blobs.ingest(handle, logical_bucket="goodware-baselines", mime_type="application/octet-stream")
            source_rows.append(GoodwareSource(filename=entry["filename"], feature_kind=entry["feature_kind"], sha256=entry["sha256"], size=entry["size"], blob_id=blob.id))
        async with self._uow_factory() as uow:
            existing = await uow.goodware_baselines.get_by_source_set_sha256(stage.manifest["source_set_sha256"])
            if existing is not None:
                return existing
            baseline = GoodwareBaseline(id=uuid4(), source_set_sha256=stage.manifest["source_set_sha256"], records_sha256=stage.manifest["records_sha256"], record_count=stage.manifest["record_count"], occurrence_sum=stage.manifest["occurrence_sum"], pattern_version="non-discriminant-patterns-v1", sources=tuple(source_rows))
            await uow.goodware_baselines.add(baseline)
            await uow.goodware_baselines.add_sources(baseline.id, baseline.sources)
            await uow.goodware_baselines.add_features(baseline.id, stage.iter_features())
            await uow.commit()
            return baseline

    async def bind(self, investigation_id: UUID, baseline_id: UUID) -> None:
        async with self._uow_factory() as uow:
            current = await uow.investigation_goodware_baselines.get(investigation_id)
            if current is not None and current != baseline_id:
                raise ValueError("investigation is already bound to another goodware baseline")
            if current is None:
                await uow.investigation_goodware_baselines.add(investigation_id, baseline_id)
                await uow.commit()
