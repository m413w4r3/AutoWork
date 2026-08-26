from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWork
from cti_app.application.virustotal import (
    VirusTotalFile,
    VirusTotalFileReport,
    VirusTotalPage,
    VirusTotalSearchResult,
)
from cti_app.application.virustotal_persistence import (
    VIRUSTOTAL_RAW_BUCKET,
    VirusTotalObservationService,
)
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.virustotal import VirusTotalFileView, VirusTotalObservation
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore


class _Blobs:
    def __init__(self, values: dict[UUID, BlobRecord]) -> None:
        self.values = values

    async def add(self, value: BlobRecord) -> None:
        self.values[value.id] = value

    async def get(self, value: UUID) -> BlobRecord | None:
        return self.values.get(value)

    async def get_by_address(self, bucket: str, sha: str) -> BlobRecord | None:
        return next(
            (
                v
                for v in self.values.values()
                if v.descriptor.logical_bucket == bucket and v.descriptor.sha256 == sha
            ),
            None,
        )


class _Observations:
    def __init__(self, values: list[VirusTotalObservation]) -> None:
        self.values = values

    async def add(self, value: VirusTotalObservation) -> None:
        self.values.append(value)


class _Views:
    def __init__(self, values: list[VirusTotalFileView]) -> None:
        self.values = values

    async def add_if_absent(self, value: VirusTotalFileView) -> bool:
        if any(item.observation_id == value.observation_id for item in self.values):
            return False
        self.values.append(value)
        return True


class _Uow:
    def __init__(self, factory: _Factory) -> None:
        self.blobs = _Blobs(factory.blobs)
        self.virustotal_observations = _Observations(factory.observations)
        self.virustotal_file_views = _Views(factory.views)

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Factory:
    def __init__(self) -> None:
        self.blobs: dict[UUID, BlobRecord] = {}
        self.observations: list[VirusTotalObservation] = []
        self.views: list[VirusTotalFileView] = []

    def __call__(self) -> UnitOfWork:
        return cast(UnitOfWork, _Uow(self))


@pytest.mark.asyncio
async def test_report_is_raw_content_addressed_and_normalized(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    raw = b'{"data":{"id":"a"}}'
    report = VirusTotalFileReport(
        file=VirusTotalFile(
            id="a", type="file", lookup_value="a" * 64, meaningful_name="x", tags=("tag",)
        ),
        raw_json=raw,
    )
    when = datetime(2026, 1, 1, tzinfo=UTC)
    first = await service.store_file_report(report, observed_at=when)
    second = await service.store_file_report(report, observed_at=when)
    assert len(factory.blobs) == 1 and len(factory.observations) == 2
    assert (
        first.blob_id == second.blob_id
        and first.raw_sha256 == next(iter(factory.blobs.values())).descriptor.sha256
    )
    assert factory.views[0].observation_id == first.id
    assert next(iter(factory.blobs.values())).descriptor.logical_bucket == VIRUSTOTAL_RAW_BUCKET
    assert await store.read(next(iter(factory.blobs.values())).descriptor, max_bytes=100) == raw


@pytest.mark.asyncio
async def test_pages_preserve_cursor_order_and_bounds(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    one = b'{"data":[{"id":"1"}],"meta":{"cursor":"next"}}'
    two = b'{"data":[{"id":"2"}]}'
    page = VirusTotalPage(
        items=(),
        next_cursor=None,
        observed_count=2,
        stopped_due_to_limit=True,
        exhaustive=False,
        raw_json_pages=(one, two),
    )
    observations = await service.store_file_relationship(
        page,
        file_hash="b" * 64,
        relation="contacted_urls",
        input_cursor="start",
        observed_at=datetime.now(UTC),
    )
    search = VirusTotalSearchResult(
        items=(),
        next_cursor=None,
        observed_count=1,
        stopped_due_to_limit=True,
        exhaustive=False,
        raw_json_pages=(one,),
    )
    await service.store_intelligence_search(
        search, query="type:peexe", observed_at=datetime.now(UTC)
    )
    assert [
        (o.input_cursor, o.output_cursor, o.observed_count, o.exhaustive, o.page_order)
        for o in observations
    ] == [("start", "next", 1, False, 0), ("next", None, 1, False, 1)]
    assert len(factory.blobs) == 2  # the first intelligence page deduplicates with relation page
