from __future__ import annotations

import hashlib
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.investigation_persistence import InMemoryEvidenceObservationRepository
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.domain.investigation import EvidenceKind
from cti_app.domain.production import AnalystInvestigationStatus
from cti_app.domain.rule_workbench import RuleTestCorpusRole

_INVESTIGATION_ID = UUID("7c768c42-63c5-44a3-9e14-9a10c8712e7a")


class _BlobCatalog:
    def __init__(self) -> None:
        self.content: dict[tuple[str, str], bytes] = {}
        self.records: dict[UUID, object] = {}
        self.ingest_count = 0

    async def ingest(self, source, *, logical_bucket: str, mime_type: str):
        data = source.read()
        sha256 = hashlib.sha256(data).hexdigest()
        self.ingest_count += 1
        address = (logical_bucket, sha256)
        self.content.setdefault(address, data)
        record = SimpleNamespace(
            id=UUID(int=len(self.content)),
            descriptor=SimpleNamespace(
                sha256=sha256,
                size=len(data),
                logical_bucket=logical_bucket,
                mime_type=mime_type,
            ),
        )
        self.records[record.id] = record
        return record

    async def get(self, blob_id: UUID):
        return self.records.get(blob_id)


class _Investigations:
    def __init__(self, status: AnalystInvestigationStatus) -> None:
        self.status = status

    async def get(self, investigation_id):
        if investigation_id != _INVESTIGATION_ID:
            return None
        return SimpleNamespace(id=investigation_id, status=self.status)


class _UnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.analyst_investigations = _Investigations(harness.status)
        self.evidence_observations = harness.observations
        self.blobs = harness.blobs
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return None

    async def commit(self) -> None:
        self.commits += 1


class _UnitOfWorkFactory:
    def __init__(self, harness: _Harness) -> None:
        self.harness = harness

    def __call__(self):
        return _UnitOfWork(self.harness)


class _Harness:
    def __init__(self, *, max_bytes: int = 1024) -> None:
        self.status = AnalystInvestigationStatus.RUNNING
        self.observations = InMemoryEvidenceObservationRepository()
        self.blobs = _BlobCatalog()
        self.uow_factory = _UnitOfWorkFactory(self)
        self.settings = SimpleNamespace(rule_corpus_import_max_bytes=max_bytes)

        from cti_app.application.rule_corpus_import import ManualRuleCorpusImportService

        self.service = ManualRuleCorpusImportService(
            uow_factory=self.uow_factory,
            blob_catalog=self.blobs,
            artifact_store=object(),
            settings=self.settings,
        )


@pytest.mark.asyncio
async def test_import_accepts_non_pe_xml_and_is_idempotent_by_role_and_content() -> None:
    harness = _Harness()
    content = (
        b"<configuration><runtime><appDomainManagerAssembly>"
        b"Example, Version=1</appDomainManagerAssembly></runtime>"
        b'<etwEnable enabled="false"/></configuration>'
    )
    payload = {
        "investigation_id": _INVESTIGATION_ID,
        "content": content,
        "intended_role": RuleTestCorpusRole.CONFIRMED_POSITIVE,
        "provenance": "synthetic",
        "operator_id": "analyst:offline-operator",
        "reason": "Generated from a quoted configuration fragment.",
        "source_description": "Synthetic .NET RuntimeBroker configuration.",
    }

    imported = await harness.service.import_bytes(**payload)
    replay = await harness.service.import_bytes(**payload)

    assert imported.sha256 == hashlib.sha256(content).hexdigest()
    assert imported.size_bytes == len(content)
    assert imported.intended_role is RuleTestCorpusRole.CONFIRMED_POSITIVE
    assert imported.provenance.value == "synthetic"
    assert replay.observation_id == imported.observation_id
    assert replay.blob_id == imported.blob_id
    assert imported.logical_bucket == SAMPLES_QUARANTINE_BUCKET
    assert harness.blobs.ingest_count == 1
    observations = await harness.observations.list_for_investigation(_INVESTIGATION_ID)
    assert len(observations) == 1
    assert observations[0].kind is EvidenceKind.OTHER
    assert "analyst:offline-operator" in " ".join(observations[0].limitations)
    assert "jamais exécuté" in " ".join(observations[0].limitations)
    assert "aucun transfert vers un tiers" in " ".join(observations[0].limitations)


@pytest.mark.asyncio
async def test_import_rejects_hash_mismatch_size_overrun_and_terminal_investigation() -> None:
    from cti_app.application.rule_corpus_import import (
        ManualRuleCorpusImportRejected,
        RuleCorpusImportRejection,
    )

    harness = _Harness(max_bytes=16)
    args = {
        "investigation_id": _INVESTIGATION_ID,
        "content": b"small xml",
        "intended_role": RuleTestCorpusRole.GOODWARE,
        "provenance": "real",
        "operator_id": "analyst:offline-operator",
        "reason": "Known benign configuration.",
        "source_description": "Internal application configuration.",
    }

    with pytest.raises(ManualRuleCorpusImportRejected) as mismatch:
        await harness.service.import_bytes(**args, declared_sha256="0" * 64)
    assert mismatch.value.reason is RuleCorpusImportRejection.SHA256_MISMATCH

    with pytest.raises(ManualRuleCorpusImportRejected) as too_large:
        await harness.service.import_bytes(**{**args, "content": b"x" * 17})
    assert too_large.value.reason is RuleCorpusImportRejection.SIZE_LIMIT

    harness.status = AnalystInvestigationStatus.COMPLETED
    with pytest.raises(ManualRuleCorpusImportRejected) as terminal:
        await harness.service.import_bytes(**args)
    assert terminal.value.reason is RuleCorpusImportRejection.INVESTIGATION_TERMINAL
    assert harness.blobs.ingest_count == 0


def test_rule_corpus_import_route_is_mounted_and_uses_multipart() -> None:
    from cti_app.api.main import create_app

    spec = create_app().openapi()
    path = "/api/investigations/{investigation_id}/rule-corpus/imports"

    assert path in spec["paths"]
    request_body = spec["paths"][path]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in request_body


def test_authenticated_multipart_route_imports_xml_and_replays_the_same_record() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cti_app.api.investigation_rule_corpus import router

    class _IdentityProvider:
        async def current(self):
            return SimpleNamespace(actor_id="analyst:manual-import-test")

    harness = _Harness()
    app = FastAPI()
    app.state.rule_corpus_import_service = harness.service
    app.state.identity_provider = _IdentityProvider()
    app.include_router(router)
    content = b"<configuration><etwEnable enabled='false'/></configuration>"
    form = {
        "intended_role": "goodware",
        "provenance": "real",
        "reason": "A vendor configuration distributed with the application.",
        "source_description": ".NET test fixture from the operator.",
    }

    with TestClient(app) as client:
        first = client.post(
            f"/api/investigations/{_INVESTIGATION_ID}/rule-corpus/imports",
            data=form,
            files={"file": ("RuntimeBroker.exe.config", content, "application/xml")},
        )
        replay = client.post(
            f"/api/investigations/{_INVESTIGATION_ID}/rule-corpus/imports",
            data=form,
            files={"file": ("RuntimeBroker.exe.config", content, "application/xml")},
        )

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    first_body = first.json()
    assert first_body["sha256"] == hashlib.sha256(content).hexdigest()
    assert first_body["provenance"] == "real"
    assert first_body["local_only"] is True
    assert first_body["executed"] is False
    assert replay.json()["observation_id"] == first_body["observation_id"]
    assert replay.json()["idempotent_replay"] is True


def test_manual_rule_corpus_route_requires_operator_identity() -> None:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cti_app.api.investigation_rule_corpus import router

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        response = client.post(
            f"/api/investigations/{_INVESTIGATION_ID}/rule-corpus/imports",
            data={
                "intended_role": "confirmed_positive",
                "provenance": "synthetic",
                "reason": "Test import.",
            },
            files={"file": ("sample.xml", b"<config/>", "application/xml")},
        )
    assert response.status_code == 401
