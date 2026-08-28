from __future__ import annotations

from pathlib import Path
from typing import cast
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
        source_set_sha256 = cast(str, stage.manifest["source_set_sha256"])
        record_count = cast(int, stage.manifest["record_count"])
        async with self._uow_factory() as uow:
            existing = await uow.goodware_baselines.get_by_source_set_sha256(source_set_sha256)
            if existing is not None:
                return existing
        source_rows: list[GoodwareSource] = []
        entries = cast(list[dict[str, object]], stage.manifest["sources"])
        for entry in entries:
            filename = cast(str, entry["filename"])
            path = source_dir / filename
            with path.open("rb") as handle:
                blob = await self._blobs.ingest(
                    handle,
                    logical_bucket="goodware-baselines",
                    mime_type="application/octet-stream",
                )
            source_rows.append(
                GoodwareSource(
                    filename=filename,
                    feature_kind=cast(str, entry["feature_kind"]),
                    sha256=cast(str, entry["sha256"]),
                    size=cast(int, entry["size"]),
                    blob_id=blob.id,
                )
            )
        async with self._uow_factory() as uow:
            baseline = GoodwareBaseline(
                id=uuid4(),
                source_set_sha256=source_set_sha256,
                records_sha256=cast(str, stage.manifest["records_sha256"]),
                record_count=cast(int, stage.manifest["record_count"]),
                occurrence_sum=cast(int, stage.manifest["occurrence_sum"]),
                pattern_version="non-discriminant-patterns-v1",
                sources=tuple(source_rows),
            )
            inserted = await uow.goodware_baselines.add_if_absent(baseline)
            if not inserted:
                existing = await uow.goodware_baselines.get_by_source_set_sha256(source_set_sha256)
                if existing is None:
                    raise RuntimeError("goodware baseline conflict without row")
                return existing
            await uow.goodware_baselines.add_sources(baseline.id, baseline.sources)
            copied = await uow.goodware_baselines.add_features(baseline.id, stage.iter_features())
            if copied != record_count:
                raise RuntimeError(
                    "goodware feature COPY row count mismatch: "
                    f"manifest={record_count}, copied={copied}"
                )
            await uow.commit()
            return baseline

    async def bind(self, investigation_id: UUID, baseline_id: UUID) -> None:
        async with self._uow_factory() as uow:
            inserted = await uow.investigation_goodware_baselines.add_if_absent(
                investigation_id, baseline_id
            )
            if not inserted:
                current = await uow.investigation_goodware_baselines.get(investigation_id)
                if current != baseline_id:
                    raise ValueError("investigation is already bound to another goodware baseline")
            else:
                await uow.commit()
