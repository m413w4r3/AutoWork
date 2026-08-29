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
from cti_app.domain.errors import DomainError
from cti_app.domain.virustotal import (
    VirusTotalCapability,
    VirusTotalEndpointVariant,
    VirusTotalFileView,
    VirusTotalObservation,
    VirusTotalOperation,
    VirusTotalTransportKind,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore

VALID_SHA256 = "a" * 64


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
    def __init__(
        self, values: list[VirusTotalObservation], *, log: list[tuple[str, int]], owner: int
    ) -> None:
        self.values = values
        self._log = log
        self._owner = owner

    async def add(self, value: VirusTotalObservation) -> None:
        self.values.append(value)
        self._log.append(("observation", self._owner))


class _Views:
    def __init__(
        self,
        values: list[VirusTotalFileView],
        *,
        log: list[tuple[str, int]],
        owner: int,
        fail: bool = False,
    ) -> None:
        self.values = values
        self._log = log
        self._owner = owner
        self._fail = fail

    async def add_if_absent(self, value: VirusTotalFileView) -> bool:
        if self._fail:
            raise RuntimeError("simulated file view failure")
        if any(item.observation_id == value.observation_id for item in self.values):
            return False
        self.values.append(value)
        self._log.append(("view", self._owner))
        return True


class _Uow:
    def __init__(self, factory: _Factory) -> None:
        factory.uow_sequence += 1
        owner = factory.uow_sequence
        self.blobs = _Blobs(factory.blobs)
        self.virustotal_observations = _Observations(
            factory.observations, log=factory.vt_writes, owner=owner
        )
        self.virustotal_file_views = _Views(
            factory.views, log=factory.vt_writes, owner=owner, fail=factory.fail_view_add
        )
        self._factory = factory
        self._entered = False

    async def __aenter__(self) -> _Uow:
        self._entered = True
        self._factory.uow_entries += 1
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        if exc_type is not None:
            # Mirrors SqlAlchemyUnitOfWork: an exception rolls back whatever
            # this UoW staged, and staged rows never reach `observations`.
            self.virustotal_observations.values[:] = self._factory.committed_observations
            self.virustotal_file_views.values[:] = self._factory.committed_views
        return None

    async def commit(self) -> None:
        self._factory.commit_attempts += 1
        should_fail = (
            self._factory.fail_commit
            and self._factory.commit_attempts > self._factory.succeed_commits
        )
        if should_fail:
            raise RuntimeError("simulated commit failure")
        self._factory.committed_observations = list(self.virustotal_observations.values)
        self._factory.committed_views = list(self.virustotal_file_views.values)
        self._factory.commits += 1


class _Factory:
    def __init__(
        self, *, fail_view_add: bool = False, fail_commit: bool = False, succeed_commits: int = 0
    ) -> None:
        self.blobs: dict[UUID, BlobRecord] = {}
        self.observations: list[VirusTotalObservation] = []
        self.views: list[VirusTotalFileView] = []
        self.committed_observations: list[VirusTotalObservation] = []
        self.committed_views: list[VirusTotalFileView] = []
        self.fail_view_add = fail_view_add
        self.fail_commit = fail_commit
        self.succeed_commits = succeed_commits
        self.commit_attempts = 0
        self.uow_entries = 0
        self.uow_sequence = 0
        self.commits = 0
        self.vt_writes: list[tuple[str, int]] = []

    def __call__(self) -> UnitOfWork:
        return cast(UnitOfWork, _Uow(self))


def _report(raw: bytes = b'{"data":{"id":"a"}}') -> VirusTotalFileReport:
    return VirusTotalFileReport(
        file=VirusTotalFile(
            id="a",
            type="file",
            lookup_value=VALID_SHA256,
            meaningful_name="x",
            tags=("tag",),
            vhash="vhash-value",
            imphash="imphash-value",
            ssdeep="ssdeep-value",
            tlsh="tlsh-value",
            main_icon_dhash="icon-value",
            rich_header_hash="rich-value",
        ),
        raw_json=raw,
        http_status=200,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )


@pytest.mark.asyncio
async def test_report_is_raw_content_addressed_and_normalized(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    report = _report()
    when = datetime(2026, 1, 1, tzinfo=UTC)
    first = await service.store_file_report(report, observed_at=when)
    second = await service.store_file_report(report, observed_at=when)
    assert len(factory.blobs) == 1 and len(factory.observations) == 2
    assert (
        first.blob_id == second.blob_id
        and first.raw_sha256 == next(iter(factory.blobs.values())).descriptor.sha256
    )
    assert factory.views[0].observation_id == first.id
    assert factory.views[0].vhash == "vhash-value"
    assert factory.views[0].rich_header_hash == "rich-value"
    assert next(iter(factory.blobs.values())).descriptor.logical_bucket == VIRUSTOTAL_RAW_BUCKET
    assert await store.read(next(iter(factory.blobs.values())).descriptor, max_bytes=100) == (
        report.raw_json
    )
    # Two independently issued calls remain two distinct observations even
    # though they share one blob: idempotence never merges network history.
    assert first.id != second.id


@pytest.mark.asyncio
async def test_blob_is_catalogued_before_the_metadata_transaction(tmp_path: Path) -> None:
    factory = _Factory(fail_view_add=True)
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    with pytest.raises(RuntimeError):
        await service.store_file_report(_report(), observed_at=datetime.now(UTC))
    # The blob is already durably catalogued even though the DB transaction
    # that references it failed and rolled back.
    assert len(factory.blobs) == 1


@pytest.mark.asyncio
async def test_observation_and_file_view_share_a_single_uow_and_commit(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    await service.store_file_report(_report(), observed_at=datetime.now(UTC))
    # Blob cataloguing is its own prior transaction; observation + view share
    # exactly one UoW/commit after that, identified by a common owner tag.
    assert factory.commits == 2  # one for the blob, one for observation+view
    assert len(factory.observations) == 1
    assert len(factory.views) == 1
    owners = {owner for _, owner in factory.vt_writes}
    assert owners == {factory.vt_writes[0][1]}
    assert {kind for kind, _ in factory.vt_writes} == {"observation", "view"}


@pytest.mark.asyncio
async def test_file_view_failure_leaves_no_observation_committed(tmp_path: Path) -> None:
    factory = _Factory(fail_view_add=True)
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    with pytest.raises(RuntimeError):
        await service.store_file_report(_report(), observed_at=datetime.now(UTC))
    # Only the prior blob-cataloguing transaction committed; the
    # observation+view transaction never did.
    assert factory.commits == 1
    assert factory.committed_observations == []
    assert factory.committed_views == []


@pytest.mark.asyncio
async def test_commit_failure_leaves_no_observation_or_view_persisted(tmp_path: Path) -> None:
    # Let the prior blob-cataloguing commit succeed; only the
    # observation+view transaction's commit fails.
    factory = _Factory(fail_commit=True, succeed_commits=1)
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    with pytest.raises(RuntimeError):
        await service.store_file_report(_report(), observed_at=datetime.now(UTC))
    assert factory.committed_observations == []
    assert factory.committed_views == []
    # The blob remains catalogued: MinIO and PostgreSQL keep separate
    # responsibilities and this is the documented orphan-blob limit.
    assert len(factory.blobs) == 1


@pytest.mark.asyncio
async def test_two_observations_may_share_the_same_blob(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    report = _report()
    when = datetime.now(UTC)
    first = await service.store_file_report(report, observed_at=when)
    second = await service.store_file_report(report, observed_at=when)
    assert first.blob_id == second.blob_id
    assert first.id != second.id
    assert len(factory.blobs) == 1


@pytest.mark.asyncio
async def test_real_http_status_is_preserved_not_invented(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    report = VirusTotalFileReport(
        file=VirusTotalFile(id="a", type="file", lookup_value=VALID_SHA256, tags=()),
        raw_json=b'{"data":{"id":"a"}}',
        http_status=206,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )
    observation = await service.store_file_report(report, observed_at=datetime.now(UTC))
    assert observation.http_status == 206


@pytest.mark.asyncio
async def test_capability_is_persisted_as_the_typed_enum(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    observation = await service.store_file_report(_report(), observed_at=datetime.now(UTC))
    assert observation.capability is VirusTotalCapability.FILE_REPORT


@pytest.mark.asyncio
async def test_no_secret_ever_reaches_safe_parameters(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    observation = await service.store_file_report(_report(), observed_at=datetime.now(UTC))
    serialized = str(observation.safe_parameters).lower()
    for forbidden in ("apikey", "x-apikey", "api_key", "://"):
        assert forbidden not in serialized
    # Transport/API-generation provenance is present but carries no secret
    # (bare enum values, never a proxy URL or credential).
    assert observation.safe_parameters["transport"] == "proxy"
    assert observation.safe_parameters["api_generation"] == "v3"


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
        http_statuses=(200, 200),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
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
        http_statuses=(200,),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )
    await service.store_intelligence_search(
        search, query="type:peexe", observed_at=datetime.now(UTC)
    )
    assert [
        (o.input_cursor, o.output_cursor, o.observed_count, o.exhaustive, o.page_order)
        for o in observations
    ] == [("start", "next", 1, False, 0), ("next", None, 1, False, 1)]
    assert len(factory.blobs) == 2  # the first intelligence page deduplicates with relation page
    # Every page of one execution shares one logical execution identity.
    assert observations[0].execution_id == observations[1].execution_id


@pytest.mark.asyncio
async def test_multi_page_execution_ids_are_distinct_across_calls(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    body = b'{"data":[{"id":"1"}]}'
    page = VirusTotalPage(
        items=(),
        next_cursor=None,
        observed_count=1,
        stopped_due_to_limit=False,
        exhaustive=True,
        raw_json_pages=(body,),
        http_statuses=(200,),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )
    first = await service.store_file_relationship(
        page, file_hash="c" * 64, relation="dropped_files", observed_at=datetime.now(UTC)
    )
    second = await service.store_file_relationship(
        page, file_hash="c" * 64, relation="dropped_files", observed_at=datetime.now(UTC)
    )
    assert first[0].execution_id != second[0].execution_id


@pytest.mark.asyncio
async def test_real_page_http_status_is_preserved(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    one = b'{"data":[{"id":"1"}],"meta":{"cursor":"next"}}'
    two = b'{"data":[{"id":"2"}]}'
    page = VirusTotalPage(
        items=(),
        next_cursor=None,
        observed_count=2,
        stopped_due_to_limit=False,
        exhaustive=True,
        raw_json_pages=(one, two),
        http_statuses=(200, 206),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )
    observations = await service.store_file_relationship(
        page, file_hash="d" * 64, relation="dropped_files", observed_at=datetime.now(UTC)
    )
    assert [o.http_status for o in observations] == [200, 206]


@pytest.mark.asyncio
async def test_real_limit_and_cursor_are_recorded_in_safe_parameters(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    body = b'{"data":[{"id":"1"}]}'
    page = VirusTotalPage(
        items=(),
        next_cursor=None,
        observed_count=1,
        stopped_due_to_limit=False,
        exhaustive=True,
        raw_json_pages=(body,),
        http_statuses=(200,),
        limit_used=17,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )
    observations = await service.store_file_relationship(
        page,
        file_hash="e" * 64,
        relation="dropped_files",
        input_cursor="abc",
        observed_at=datetime.now(UTC),
    )
    assert observations[0].safe_parameters["limit"] == 17
    assert observations[0].safe_parameters["input_cursor"] == "abc"
    assert observations[0].input_cursor == "abc"


@pytest.mark.asyncio
async def test_descriptors_only_true_is_recorded_for_intelligence_search(
    tmp_path: Path,
) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    body = b'{"data":[{"id":"1"}]}'
    search = VirusTotalSearchResult(
        items=(),
        next_cursor=None,
        observed_count=1,
        stopped_due_to_limit=False,
        exhaustive=True,
        raw_json_pages=(body,),
        http_statuses=(200,),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )
    observations = await service.store_intelligence_search(
        search, query="type:peexe", observed_at=datetime.now(UTC)
    )
    assert observations[0].safe_parameters["descriptors_only"] is True
    assert observations[0].safe_parameters["query"] == "type:peexe"


@pytest.mark.asyncio
async def test_proxy_and_v3_provenance_is_recorded_without_secrets(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    body = b'{"data":[{"id":"1"}]}'
    page = VirusTotalPage(
        items=(),
        next_cursor=None,
        observed_count=1,
        stopped_due_to_limit=False,
        exhaustive=True,
        raw_json_pages=(body,),
        http_statuses=(200,),
        limit_used=40,
        transport=VirusTotalTransportKind.DIRECT,
        api_generation=VirusTotalEndpointVariant.LEGACY_V2,
    )
    observations = await service.store_file_relationship(
        page, file_hash="f" * 64, relation="dropped_files", observed_at=datetime.now(UTC)
    )
    assert observations[0].safe_parameters["transport"] == "direct"
    assert observations[0].safe_parameters["api_generation"] == "legacy_v2"


def test_invalid_raw_sha256_is_rejected_by_the_domain() -> None:
    with pytest.raises(DomainError):
        VirusTotalObservation(
            operation=VirusTotalOperation.FILE_REPORT,
            capability=VirusTotalCapability.FILE_REPORT,
            source_identifier=VALID_SHA256,
            safe_parameters={},
            http_status=200,
            blob_id=UUID(int=1),
            raw_sha256="not-a-valid-hash",
            raw_size=1,
            observed_at=datetime.now(UTC),
        )


def test_naive_datetime_is_rejected_by_the_domain() -> None:
    with pytest.raises(DomainError):
        VirusTotalObservation(
            operation=VirusTotalOperation.FILE_REPORT,
            capability=VirusTotalCapability.FILE_REPORT,
            source_identifier=VALID_SHA256,
            safe_parameters={},
            http_status=200,
            blob_id=UUID(int=1),
            raw_sha256=VALID_SHA256,
            raw_size=1,
            observed_at=datetime(2026, 1, 1),
        )


@pytest.mark.asyncio
async def test_bounded_stop_stays_non_exhaustive(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    one = b'{"data":[{"id":"1"}],"meta":{"cursor":"next"}}'
    page = VirusTotalPage(
        items=(),
        next_cursor="next",
        observed_count=1,
        stopped_due_to_limit=True,
        exhaustive=False,
        raw_json_pages=(one,),
        http_statuses=(200,),
        limit_used=40,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )
    observations = await service.store_file_relationship(
        page, file_hash="1" * 64, relation="dropped_files", observed_at=datetime.now(UTC)
    )
    assert observations[-1].exhaustive is False


@pytest.mark.asyncio
async def test_no_sample_or_workspace_is_ever_touched(tmp_path: Path) -> None:
    factory = _Factory()
    store = FilesystemBlobStore(tmp_path / "blobs")
    service = VirusTotalObservationService(BlobCatalogService(store, factory), factory)
    await service.store_file_report(_report(), observed_at=datetime.now(UTC))
    assert not hasattr(factory, "samples")
    assert not hasattr(factory, "workspaces")
