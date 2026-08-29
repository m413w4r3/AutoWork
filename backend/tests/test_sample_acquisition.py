from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
from typing import Any, BinaryIO
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.sample_acquisition import (
    SampleAcquisitionCapabilityDisabledError,
    SampleAcquisitionPolicy,
    SampleAcquisitionPolicyDeniedError,
    VirusTotalSampleAcquisitionService,
)
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalDownloadResult,
    VirusTotalHttpError,
    VirusTotalInvalidInputError,
)
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SampleOrigin, SampleState
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    SampleAcquisitionAttempt,
    SampleAcquisitionOutcome,
    SampleAcquisitionReason,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.virustotal import VirusTotalHttpAdapter

BASE = "https://www.virustotal.com/api/v3"

# ---------------------------------------------------------------------------
# In-memory fakes: no network, no database, matching the M2 discipline.
# ---------------------------------------------------------------------------


class _FakeDatabase:
    def __init__(self) -> None:
        self.blobs_by_address: dict[tuple[str, str], Any] = {}
        self.blobs_by_id: dict[UUID, Any] = {}
        self.samples: dict[UUID, Sample] = {}
        self.attempts: list[SampleAcquisitionAttempt] = []
        self.investigations: dict[UUID, AnalystInvestigation] = {}
        self.provenance: list[Any] = []


class _FakeBlobRepository:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db

    async def add(self, blob: Any) -> None:
        self._db.blobs_by_address[(blob.descriptor.logical_bucket, blob.descriptor.sha256)] = blob
        self._db.blobs_by_id[blob.id] = blob

    async def get(self, blob_id: UUID) -> Any:
        return self._db.blobs_by_id.get(blob_id)

    async def get_by_address(self, logical_bucket: str, sha256: str) -> Any:
        return self._db.blobs_by_address.get((logical_bucket, sha256))

    async def count_references(self, blob_id: UUID) -> int:
        return sum(1 for s in self._db.samples.values() if s.blob_id == blob_id)

    async def delete(self, blob_id: UUID) -> None:
        self._db.blobs_by_id.pop(blob_id, None)


class _FakeSampleRepository:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db

    async def add(self, sample: Sample) -> None:
        self._db.samples[sample.id] = sample

    async def get(self, sample_id: UUID) -> Sample | None:
        return self._db.samples.get(sample_id)

    async def list_for_subject(self, subject_id: UUID) -> list[Sample]:
        return [s for s in self._db.samples.values() if s.subject_id == subject_id]

    async def get_by_subject_and_blob(self, subject_id: UUID, blob_id: UUID) -> Sample | None:
        for sample in self._db.samples.values():
            if sample.subject_id == subject_id and sample.blob_id == blob_id:
                return sample
        return None


class _FakeSampleAcquisitionAttemptRepository:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db

    async def find_successful(
        self, investigation_id: UUID, requested_hash: str
    ) -> SampleAcquisitionAttempt | None:
        for attempt in self._db.attempts:
            if (
                attempt.investigation_id == investigation_id
                and attempt.requested_hash == requested_hash
                and attempt.outcome is SampleAcquisitionOutcome.SUCCESS
            ):
                return attempt
        return None

    async def append(self, attempt: SampleAcquisitionAttempt) -> None:
        # Emulates the DB's partial unique index: at most one SUCCESS row
        # per (investigation_id, requested_hash) pair.
        if attempt.outcome is SampleAcquisitionOutcome.SUCCESS:
            for existing in self._db.attempts:
                if (
                    existing.outcome is SampleAcquisitionOutcome.SUCCESS
                    and existing.investigation_id == attempt.investigation_id
                    and existing.requested_hash == attempt.requested_hash
                ):
                    raise RuntimeError("unique violation: duplicate successful acquisition")
        self._db.attempts.append(attempt)


class _FakeAnalystInvestigationRepository:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        stored = self._db.investigations.get(investigation_id)
        return copy.deepcopy(stored) if stored is not None else None

    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None:
        return None

    async def add(self, investigation: AnalystInvestigation) -> None:
        self._db.investigations[investigation.id] = copy.deepcopy(investigation)

    async def save(self, investigation: AnalystInvestigation) -> None:
        current = self._db.investigations.get(investigation.id)
        if current is None or current.version != investigation.version - 1:
            raise RuntimeError("stale analyst investigation write")
        self._db.investigations[investigation.id] = copy.deepcopy(investigation)


class _FakeProvenanceRepository:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db

    async def append(self, event: Any) -> None:
        self._db.provenance.append(event)

    async def list_for_aggregate(self, aggregate_type: str, aggregate_id: UUID) -> list[Any]:
        return [
            e
            for e in self._db.provenance
            if e.aggregate_type == aggregate_type and e.aggregate_id == aggregate_id
        ]


class _FakeUnitOfWork:
    def __init__(self, db: _FakeDatabase) -> None:
        self._db = db

    async def __aenter__(self) -> _FakeUnitOfWork:
        self.blobs = _FakeBlobRepository(self._db)
        self.samples = _FakeSampleRepository(self._db)
        self.sample_acquisition_attempts = _FakeSampleAcquisitionAttemptRepository(self._db)
        self.analyst_investigations = _FakeAnalystInvestigationRepository(self._db)
        self.provenance = _FakeProvenanceRepository(self._db)
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakePort:
    """Never touches the network: it is a plain in-memory stand-in for VirusTotalPort."""

    def __init__(self, *, content: bytes | None = None, error: Exception | None = None) -> None:
        self.content = content
        self.error = error
        self.calls = 0

    async def file_report(self, file_hash: str) -> Any:
        raise NotImplementedError

    async def file_relationship(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def intelligence_search(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError

    async def file_download(self, file_hash: str, *, sink: BinaryIO) -> VirusTotalDownloadResult:
        self.calls += 1
        if self.error is not None:
            raise self.error
        assert self.content is not None
        sink.write(self.content)
        return VirusTotalDownloadResult(
            md5=hashlib.md5(self.content).hexdigest(),
            sha1=hashlib.sha1(self.content).hexdigest(),
            sha256=hashlib.sha256(self.content).hexdigest(),
            size=len(self.content),
        )


def _investigation(
    *, max_vt_read_units: int = 2, max_hits_acquired: int = 1
) -> AnalystInvestigation:
    return AnalystInvestigation(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(max_vt_read_units=max_vt_read_units, max_hits_acquired=max_hits_acquired),
    )


def _service(
    tmp_path: Path,
    db: _FakeDatabase,
    port: _FakePort,
    *,
    capabilities: VirusTotalCapabilities | None = None,
) -> VirusTotalSampleAcquisitionService:
    store = FilesystemBlobStore(tmp_path / "blobs")
    catalog = BlobCatalogService(store, lambda: _FakeUnitOfWork(db))
    return VirusTotalSampleAcquisitionService(
        port,
        capabilities or VirusTotalCapabilities(file_download=True),
        catalog,
        lambda: _FakeUnitOfWork(db),
        LocalIdentityProvider(),
    )


_CLEAR_POLICY = SampleAcquisitionPolicy(
    tlp=TLP.CLEAR, do_not_submit=False, external_llm_allowed=True
)


# ---------------------------------------------------------------------------
# 1. Budget configured from Settings, never a domain default of all-zero.
# ---------------------------------------------------------------------------


def test_investigation_budget_settings_are_configurable_and_default_to_current_p04_shape() -> None:
    defaults = Settings(_env_file=None)
    assert defaults.investigation_max_cycles == 3
    assert defaults.investigation_max_pivot_runs == 0
    assert defaults.investigation_max_hits_acquired == 0
    assert defaults.investigation_max_new_samples == 0
    assert defaults.investigation_max_vt_read_units == 0

    configured = Settings(
        _env_file=None,
        investigation_max_cycles=5,
        investigation_max_pivot_runs=2,
        investigation_max_hits_acquired=3,
        investigation_max_new_samples=4,
        investigation_max_vt_read_units=6,
    )
    assert configured.investigation_max_cycles == 5
    assert configured.investigation_max_pivot_runs == 2
    assert configured.investigation_max_hits_acquired == 3
    assert configured.investigation_max_new_samples == 4
    assert configured.investigation_max_vt_read_units == 6


def test_loop_budget_from_settings_carries_configured_caps_without_a_domain_default() -> None:
    from cti_app.application.analyst_handoff import loop_budget_from_settings

    settings = Settings(
        _env_file=None, investigation_max_vt_read_units=7, investigation_max_cycles=4
    )
    budget = loop_budget_from_settings(settings)
    assert budget.max_vt_read_units == 7
    assert budget.max_cycles == 4
    assert budget.consumed_vt_read_units == 0


# ---------------------------------------------------------------------------
# 2 & 5. Success paths for seed and hit_review, and MD5/SHA1/SHA256 hashes.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_successful_seed_acquisition_creates_a_quarantined_sample(tmp_path: Path) -> None:
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    content = b"controlled-seed-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    port = _FakePort(content=content)
    service = _service(tmp_path, db, port)

    result = await service.acquire(
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        requested_hash=sha256,
        reason=SampleAcquisitionReason.SEED,
        policy=SampleAcquisitionPolicy(
            tlp=TLP.GREEN, do_not_submit=False, external_llm_allowed=False
        ),
    )

    assert result.outcome is SampleAcquisitionOutcome.SUCCESS
    assert result.reused_existing_sample is False
    sample = db.samples[result.sample_id]
    assert sample.origin_kind is SampleOrigin.VT_SEED
    assert sample.state is SampleState.QUARANTINED
    assert sample.tlp is TLP.GREEN
    assert sample.do_not_submit is True  # always locked down, regardless of policy
    assert sample.external_llm_allowed is False  # inherited
    assert sample.source_service == "virustotal"
    assert sample.source_object_id == sha256
    assert sample.expected_hash == sha256
    assert sample.original_name == sha256  # never an externally supplied filename

    stored_investigation = db.investigations[investigation.id]
    assert stored_investigation.budget.consumed_vt_read_units == 1
    assert stored_investigation.budget.consumed_hits_acquired == 0

    events = [e for e in db.provenance if e.aggregate_id == sample.id]
    assert len(events) == 1
    assert events[0].actor_id is not None
    assert "signed" not in json.dumps(events[0].payload)


@pytest.mark.asyncio
async def test_hit_review_consumes_hits_acquired_and_sets_review_candidate(tmp_path: Path) -> None:
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    content = b"hunt-hit-bytes"
    sha1 = hashlib.sha1(content).hexdigest()
    port = _FakePort(content=content)
    service = _service(tmp_path, db, port)

    result = await service.acquire(
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        requested_hash=sha1.upper(),
        reason=SampleAcquisitionReason.HIT_REVIEW,
        policy=_CLEAR_POLICY,
    )

    sample = db.samples[result.sample_id]
    assert sample.origin_kind is SampleOrigin.VT_HUNT_HIT
    assert sample.state is SampleState.REVIEW_CANDIDATE
    assert sample.expected_hash == sha1  # normalized to lowercase
    stored_investigation = db.investigations[investigation.id]
    assert stored_investigation.budget.consumed_vt_read_units == 1
    assert stored_investigation.budget.consumed_hits_acquired == 1
    attempt = next(a for a in db.attempts if a.sample_id == sample.id)
    assert attempt.hash_family == "sha1"
    assert attempt.reason is SampleAcquisitionReason.HIT_REVIEW


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content", "family_len"),
    [(b"md5-content", 32), (b"sha1-content", 40), (b"sha256-content", 64)],
)
async def test_md5_sha1_sha256_requested_hashes_are_all_accepted(
    tmp_path: Path, content: bytes, family_len: int
) -> None:
    digest_fn = {32: hashlib.md5, 40: hashlib.sha1, 64: hashlib.sha256}[family_len]
    digest = digest_fn(content).hexdigest()
    expected_family = {32: "md5", 40: "sha1", 64: "sha256"}[family_len]
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    port = _FakePort(content=content)
    service = _service(tmp_path, db, port)

    result = await service.acquire(
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        requested_hash=digest,
        reason=SampleAcquisitionReason.SEED,
        policy=_CLEAR_POLICY,
    )

    attempt = next(a for a in db.attempts if a.sample_id == result.sample_id)
    assert attempt.hash_family == expected_family
    assert attempt.requested_hash == digest


# ---------------------------------------------------------------------------
# 3. Idempotent replay: no budget, no network on a second identical call.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_replay_of_a_successful_acquisition_consumes_no_budget_and_makes_no_network_call(
    tmp_path: Path,
) -> None:
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    content = b"replay-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    port = _FakePort(content=content)
    service = _service(tmp_path, db, port)

    first = await service.acquire(
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        requested_hash=sha256,
        reason=SampleAcquisitionReason.SEED,
        policy=_CLEAR_POLICY,
    )
    assert port.calls == 1
    consumed_after_first = db.investigations[investigation.id].budget.consumed_vt_read_units

    second = await service.acquire(
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        requested_hash=sha256,
        reason=SampleAcquisitionReason.SEED,
        policy=_CLEAR_POLICY,
    )

    assert port.calls == 1  # no second network call
    assert second.sample_id == first.sample_id
    assert second.reused_existing_sample is True
    assert db.investigations[investigation.id].budget.consumed_vt_read_units == consumed_after_first


@pytest.mark.asyncio
async def test_same_bytes_under_a_different_hash_family_reuse_the_existing_sample(
    tmp_path: Path,
) -> None:
    db = _FakeDatabase()
    investigation = _investigation(max_vt_read_units=2)
    db.investigations[investigation.id] = investigation
    content = b"same-bytes-two-hash-families"
    sha256 = hashlib.sha256(content).hexdigest()
    md5 = hashlib.md5(content).hexdigest()
    service = _service(tmp_path, db, _FakePort(content=content))

    first = await service.acquire(
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        requested_hash=sha256,
        reason=SampleAcquisitionReason.SEED,
        policy=_CLEAR_POLICY,
    )
    second = await service.acquire(
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        requested_hash=md5,
        reason=SampleAcquisitionReason.SEED,
        policy=_CLEAR_POLICY,
    )

    assert second.sample_id == first.sample_id
    assert second.reused_existing_sample is True
    assert len(db.samples) == 1  # one Sample per blob per subject


# ---------------------------------------------------------------------------
# 4. Policy and capability gates fire before budget/network.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        SampleAcquisitionPolicy(tlp=TLP.AMBER, do_not_submit=False, external_llm_allowed=True),
        SampleAcquisitionPolicy(
            tlp=TLP.AMBER_STRICT, do_not_submit=False, external_llm_allowed=True
        ),
        SampleAcquisitionPolicy(tlp=TLP.RED, do_not_submit=False, external_llm_allowed=True),
        SampleAcquisitionPolicy(tlp=TLP.CLEAR, do_not_submit=True, external_llm_allowed=True),
    ],
)
async def test_policy_denies_disclosure_before_budget_or_network(
    tmp_path: Path, policy: SampleAcquisitionPolicy
) -> None:
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    port = _FakePort(content=b"unreachable")
    service = _service(tmp_path, db, port)

    with pytest.raises(SampleAcquisitionPolicyDeniedError):
        await service.acquire(
            investigation_id=investigation.id,
            subject_id=investigation.subject_id,
            requested_hash="a" * 64,
            reason=SampleAcquisitionReason.SEED,
            policy=policy,
        )

    assert port.calls == 0
    assert db.investigations[investigation.id].budget.consumed_vt_read_units == 0
    failure_events = [e for e in db.provenance if e.aggregate_id == investigation.id]
    assert len(failure_events) == 1
    assert failure_events[0].payload["error_code"] == "sample_acquisition_policy_denied"


@pytest.mark.asyncio
async def test_capability_disabled_is_checked_before_hash_validation(tmp_path: Path) -> None:
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    port = _FakePort(content=b"unreachable")
    service = _service(tmp_path, db, port, capabilities=VirusTotalCapabilities(file_download=False))

    with pytest.raises(SampleAcquisitionCapabilityDisabledError):
        await service.acquire(
            investigation_id=investigation.id,
            subject_id=investigation.subject_id,
            requested_hash="not-a-hash-at-all",
            reason=SampleAcquisitionReason.SEED,
            policy=_CLEAR_POLICY,
        )

    assert port.calls == 0
    assert db.investigations[investigation.id].budget.consumed_vt_read_units == 0


@pytest.mark.asyncio
async def test_invalid_hash_is_rejected_before_policy_budget_or_network(tmp_path: Path) -> None:
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    port = _FakePort(content=b"unreachable")
    service = _service(tmp_path, db, port)

    with pytest.raises(VirusTotalInvalidInputError):
        await service.acquire(
            investigation_id=investigation.id,
            subject_id=investigation.subject_id,
            requested_hash="not-a-hash",
            reason=SampleAcquisitionReason.SEED,
            policy=_CLEAR_POLICY,
        )

    assert port.calls == 0
    assert db.investigations[investigation.id].budget.consumed_vt_read_units == 0


# ---------------------------------------------------------------------------
# 6. Budget consumed and committed before the network call; never refunded.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_budget_is_not_refunded_when_the_vt_call_fails_after_being_consumed(
    tmp_path: Path,
) -> None:
    db = _FakeDatabase()
    investigation = _investigation()
    db.investigations[investigation.id] = investigation
    port = _FakePort(
        error=VirusTotalHttpError("not found", code="virustotal_not_found", status_code=404)
    )
    service = _service(tmp_path, db, port)

    with pytest.raises(VirusTotalHttpError):
        await service.acquire(
            investigation_id=investigation.id,
            subject_id=investigation.subject_id,
            requested_hash="b" * 64,
            reason=SampleAcquisitionReason.SEED,
            policy=_CLEAR_POLICY,
        )

    assert port.calls == 1
    assert db.investigations[investigation.id].budget.consumed_vt_read_units == 1
    assert len(db.samples) == 0
    error_attempt = next(a for a in db.attempts if a.outcome is SampleAcquisitionOutcome.ERROR)
    assert error_attempt.error_code == "virustotal_not_found"
    failure_events = [e for e in db.provenance if e.aggregate_id == investigation.id]
    assert len(failure_events) == 1


# ---------------------------------------------------------------------------
# 7. The signed download never carries VT client auth, honors the host
#    allowlist, refuses redirects, and verifies MD5/SHA1/SHA256 while
#    streaming — exercised against the real adapter, still with no network.
# ---------------------------------------------------------------------------


def _adapter(
    handler: Any, download_handler: Any, *, allowed_hosts: frozenset[str]
) -> VirusTotalHttpAdapter:
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        download_allowed_hosts=allowed_hosts,
        download_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(download_handler), follow_redirects=False
        ),
    )


@pytest.mark.asyncio
async def test_adapter_signed_download_carries_no_vt_auth_header_and_verifies_sha256() -> None:
    content = b"quarantined-bytes"
    sha256 = hashlib.sha256(content).hexdigest()
    seen_download_headers: list[httpx.Headers] = []

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == f"/api/v3/files/{sha256}/download_url"
        assert "x-apikey" not in request.headers
        return httpx.Response(
            200, json={"data": "https://signed.example.test/object"}, request=request
        )

    def download_handler(request: httpx.Request) -> httpx.Response:
        seen_download_headers.append(request.headers)
        return httpx.Response(200, content=content, request=request)

    adapter = _adapter(handler, download_handler, allowed_hosts=frozenset({"signed.example.test"}))
    sink = io_bytes_sink()
    result = await adapter.file_download(sha256, sink=sink)

    assert result.sha256 == sha256
    assert sink.getvalue() == content
    assert "x-apikey" not in seen_download_headers[0]
    assert "authorization" not in seen_download_headers[0]


@pytest.mark.asyncio
async def test_adapter_rejects_signed_host_outside_the_allowlist_before_downloading() -> None:
    calls = 0

    def download_handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, content=b"x", request=request)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": "https://not-allowed.example.test/object"}, request=request
        )

    from cti_app.application.virustotal import VirusTotalDownloadHostNotAllowedError

    adapter = _adapter(handler, download_handler, allowed_hosts=frozenset({"signed.example.test"}))
    with pytest.raises(VirusTotalDownloadHostNotAllowedError):
        await adapter.file_download("c" * 64, sink=io_bytes_sink())
    assert calls == 0


@pytest.mark.asyncio
async def test_adapter_refuses_a_redirect_from_the_signed_download() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": "https://signed.example.test/object"}, request=request
        )

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            302, headers={"location": "https://elsewhere.example.test/x"}, request=request
        )

    from cti_app.application.virustotal import VirusTotalUnexpectedRedirectError

    adapter = _adapter(handler, download_handler, allowed_hosts=frozenset({"signed.example.test"}))
    with pytest.raises(VirusTotalUnexpectedRedirectError):
        await adapter.file_download("d" * 64, sink=io_bytes_sink())


@pytest.mark.asyncio
async def test_adapter_rejects_wrong_bytes_for_the_requested_hash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": "https://signed.example.test/object"}, request=request
        )

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-the-expected-content", request=request)

    from cti_app.application.virustotal import VirusTotalDownloadHashMismatchError

    adapter = _adapter(handler, download_handler, allowed_hosts=frozenset({"signed.example.test"}))
    with pytest.raises(VirusTotalDownloadHashMismatchError):
        await adapter.file_download("e" * 64, sink=io_bytes_sink())


@pytest.mark.asyncio
async def test_adapter_enforces_the_download_size_limit_while_streaming() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200, json={"data": "https://signed.example.test/object"}, request=request
        )

    def download_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"0123456789", request=request)

    from cti_app.application.virustotal import VirusTotalDownloadTooLargeError

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    adapter = VirusTotalHttpAdapter(
        client=client,
        base_url=BASE,
        capabilities=VirusTotalCapabilities(file_download=True),
        file_download_enabled=True,
        download_allowed_hosts=frozenset({"signed.example.test"}),
        download_max_bytes=4,
        download_client_factory=lambda: httpx.AsyncClient(
            transport=httpx.MockTransport(download_handler), follow_redirects=False
        ),
    )
    with pytest.raises(VirusTotalDownloadTooLargeError):
        await adapter.file_download("f" * 64, sink=io_bytes_sink())


def io_bytes_sink() -> Any:
    import io

    return io.BytesIO()


# ---------------------------------------------------------------------------
# 8. Nothing in this batch anticipates the P14 endpoint or a Dramatiq actor.
# ---------------------------------------------------------------------------


def test_sample_acquisition_module_defines_no_endpoint_or_worker_wiring() -> None:
    source = Path("src/cti_app/application/sample_acquisition.py").read_text(encoding="utf-8")
    for forbidden in ("APIRouter", "@app.", "dramatiq", "fastapi"):
        assert forbidden not in source
