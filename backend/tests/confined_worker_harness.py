"""In-process adapter from production investigation ports to real worker handlers."""

from __future__ import annotations

import hashlib
import json
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.confined_analysis import (
    ConfinedAnalysisJobParameters,
    ConfinedAnalysisWorkerAttestation,
)
from cti_app.application.jobs import DuplicateJobError, JobExecutionContext
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.config import Settings
from cti_app.domain.jobs import JobStatus
from cti_app.infrastructure.capa import ruleset_manifest
from cti_app.infrastructure.confined_analysis_dispatch import (
    DramatiqConfinedAnalysisExecutionPort,
    _attestation_from_response,
)
from cti_app.workers.analysis_tasks import build_confined_analysis_job_handler
from cti_app.workers.confined_analysis import build_analysis_worker_probe_payload


@dataclass(slots=True)
class _HarnessJob:
    id: UUID
    parameters: ConfinedAnalysisJobParameters
    idempotency_key: str
    status: JobStatus = JobStatus.QUEUED
    output_reference: str | None = None


class _HarnessJobService:
    """Small durable-job double preserving the dispatch port's idempotency path."""

    def __init__(self) -> None:
        self.jobs: dict[UUID, _HarnessJob] = {}
        self.by_key: dict[str, UUID] = {}
        self.submissions = 0

    async def submit(self, **kwargs: Any) -> _HarnessJob:
        self.submissions += 1
        key = str(kwargs["idempotency_key"])
        if key in self.by_key:
            raise DuplicateJobError(self.by_key[key])
        parameters = ConfinedAnalysisJobParameters.model_validate(kwargs["input_parameters"])
        job = _HarnessJob(uuid4(), parameters, key)
        self.jobs[job.id] = job
        self.by_key[key] = job.id
        return job

    async def get(self, job_id: UUID) -> _HarnessJob:
        return self.jobs[job_id]


class _WorkerBlobCatalog:
    """Route worker result blobs through the test's canonical artifact store."""

    def __init__(
        self, artifact_store: ProductionArtifactStore, uow_factory: UnitOfWorkFactory
    ) -> None:
        self._artifact_store = artifact_store
        self._uow_factory = uow_factory

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        return await self._artifact_store.read_bytes(blob_id, max_bytes=max_bytes)

    async def ingest(self, source: Any, *, logical_bucket: str, mime_type: str) -> Any:
        blob_id = await self._artifact_store.put_bytes(
            source.read(), bucket=logical_bucket, mime_type=mime_type
        )
        async with self._uow_factory() as uow:
            record = await uow.blobs.get(blob_id)
        if record is None:
            raise AssertionError("analysis-worker result blob was not registered canonically")
        return record


class ConfinedRuleCorpusBytes:
    """Give test corpus bytes canonical blob identities before worker execution."""

    def __init__(self, delegate: Any, artifact_store: ProductionArtifactStore) -> None:
        self._delegate = delegate
        self._artifact_store = artifact_store
        self._artifact_ids: dict[str, UUID] = {}

    async def get_bytes_for_sample(self, sample_sha256: str) -> bytes:
        payload = await self._delegate.get_bytes_for_sample(sample_sha256)
        if not isinstance(payload, bytes) or hashlib.sha256(payload).hexdigest() != sample_sha256:
            raise ValueError("test rule corpus bytes do not match their canonical SHA-256")
        return payload

    async def get_artifact_id_for_sample(self, sample_sha256: str) -> UUID:
        artifact_id = self._artifact_ids.get(sample_sha256)
        if artifact_id is not None:
            return artifact_id
        payload = await self.get_bytes_for_sample(sample_sha256)
        artifact_id = await self._artifact_store.put_bytes(
            payload,
            bucket=SAMPLES_QUARANTINE_BUCKET,
            mime_type="application/octet-stream",
        )
        self._artifact_ids[sample_sha256] = artifact_id
        return artifact_id

    def __getattr__(self, name: str) -> Any:
        return getattr(self._delegate, name)


class InProcessAnalysisDispatcher:
    """Execute the same registered handler used by analysis-worker, in the test process."""

    def __init__(self, jobs: Any, handler: Any, uow_factory: UnitOfWorkFactory):
        self.jobs = jobs
        self._handler = handler
        self._uow_factory = uow_factory
        self.calls: list[UUID] = []

    async def dispatch(self, job_id: UUID) -> None:
        self.calls.append(job_id)
        job = self.jobs.jobs[job_id]
        job.status = JobStatus.RUNNING
        try:
            job.output_reference = await self._handler(
                job.parameters,
                JobExecutionContext(job.id, self._uow_factory),
            )
        except Exception:
            job.status = JobStatus.FAILED
            raise
        job.status = JobStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class ConfinedWorkerHarness:
    settings: Settings
    port: DramatiqConfinedAnalysisExecutionPort
    dispatcher: InProcessAnalysisDispatcher
    jobs: _HarnessJobService
    attestation: ConfinedAnalysisWorkerAttestation


def build_confined_worker_harness(
    *,
    settings: Settings,
    uow_factory: UnitOfWorkFactory,
    artifact_store: ProductionArtifactStore,
    required_families: tuple[str, ...] = (),
) -> ConfinedWorkerHarness:
    """Build a test-only port with real installed engines and the production handler.

    The synthetic isolation fields mark the routing fixture as trusted so that the
    application follows its production port path. They do not claim that pytest's
    process itself has the analysis-worker container's OS isolation.
    """

    scratch = tempfile.TemporaryDirectory(prefix="autowork-analysis-worker-harness-")
    rules_path = Path(scratch.name) / "capa-rules"
    rules_path.mkdir()
    (rules_path / "safe_fixture.yml").write_text(
        "rule:\n"
        "  meta:\n"
        "    name: Safe in-process worker fixture\n"
        "    namespace: autowork/test\n"
        "    authors:\n"
        "      - AutoWork\n"
        "    scopes:\n"
        "      static: file\n"
        "      dynamic: process\n"
        "  features:\n"
        "    - string: AUTOWORK_SAFE_WORKFLOW_FIXTURE\n",
        encoding="utf-8",
    )
    revision = "safe-test-rules-v1"
    rules_hash = ruleset_manifest(rules_path)
    if rules_hash is None:
        raise AssertionError("test analysis-worker capa fixture has no canonical ruleset")
    (rules_path / "AUTOWORK_RULESET_REVISION").write_text(revision, encoding="ascii")
    (rules_path / "AUTOWORK_RULESET_SHA256").write_text(rules_hash, encoding="ascii")
    base_settings = settings.model_copy(
        update={
            "analysis_worker_enabled": True,
            "analysis_worker_expected_isolation_profile": "test-in-process",
            "analysis_capa_rules_path": rules_path,
            "analysis_capa_ruleset_revision": revision,
        }
    )
    detected_versions = build_analysis_worker_probe_payload(base_settings)["versions"]
    if not isinstance(detected_versions, dict):
        raise AssertionError("test analysis-worker version probe is malformed")
    expected_versions = {
        "analysis_capa_expected_version": detected_versions.get("capa"),
        "analysis_smda_expected_version": detected_versions.get("smda"),
        "analysis_tshark_expected_version": detected_versions.get("tshark"),
        "analysis_capinfos_expected_version": detected_versions.get("capinfos"),
        "analysis_local_file_expected_version": detected_versions.get("local_file"),
        "analysis_local_binutils_expected_version": detected_versions.get("local_strings"),
        "analysis_local_xxd_expected_version": detected_versions.get("local_xxd"),
        "analysis_local_coreutils_expected_version": detected_versions.get("local_sha256sum"),
        "analysis_local_7z_expected_version": detected_versions.get("local_7z") or "16.02",
        "analysis_local_monodis_expected_version": detected_versions.get("local_monodis"),
    }
    worker_settings = base_settings.model_copy(
        update={key: value for key, value in expected_versions.items() if value is not None}
    )
    probe = build_analysis_worker_probe_payload(worker_settings)
    probe.update(
        {
            "uid": 10001,
            "no_new_privileges": True,
            "effective_capabilities": "0000000000000000",
            "root_read_only": True,
            "isolation_profile": "test-in-process",
            "postgres_access": True,
            "minio_access": True,
        }
    )
    attestation = _attestation_from_response(
        json.dumps(probe, sort_keys=True, separators=(",", ":")).encode(), worker_settings
    )
    if not attestation.reachable or not attestation.confined or not attestation.dependencies_ready:
        raise AssertionError("test analysis-worker attestation was not accepted")
    unavailable = {
        family: (
            attestation.families[family].available_reason
            if family in attestation.families
            else "analysis_family_not_attested"
        )
        for family in required_families
        if family not in attestation.families or not attestation.families[family].available
    }
    if unavailable:
        raise AssertionError(f"required local analysis engines are unavailable: {unavailable}")

    jobs = _HarnessJobService()
    blobs = _WorkerBlobCatalog(artifact_store, uow_factory)
    handler = build_confined_analysis_job_handler(
        blobs=blobs,  # type: ignore[arg-type]
        uow_factory=uow_factory,
        settings=worker_settings,
    )
    dispatcher = InProcessAnalysisDispatcher(jobs, handler, uow_factory)
    port = DramatiqConfinedAnalysisExecutionPort(
        settings=worker_settings,
        uow_factory=uow_factory,
        blob_catalog=blobs,  # type: ignore[arg-type]
        attestation=attestation,
        dispatcher=dispatcher,
        job_service=jobs,  # type: ignore[arg-type]
    )
    # Keep the temporary pinned ruleset alive for the complete lifetime of this port.
    port._test_ruleset_owner = scratch  # type: ignore[attr-defined]
    return ConfinedWorkerHarness(worker_settings, port, dispatcher, jobs, attestation)
