from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_persistence import InMemoryEvidenceObservationRepository
from cti_app.application.rule_corpus_import import (
    ManualRuleCorpusImportRejected,
    RuleCorpusImportRejection,
)
from cti_app.application.sample_acquisition import SAMPLES_QUARANTINE_BUCKET
from cti_app.domain.classification import TLP
from cti_app.domain.entities import ProvenanceEvent, Sample, SampleOrigin, SampleState
from cti_app.domain.investigation import (
    EvidenceKind,
    SampleAssociation,
    SampleQualification,
)
from cti_app.domain.investigation import (
    SampleOrigin as AssociationSampleOrigin,
)
from cti_app.domain.production import AnalystInvestigationStatus
from cti_app.domain.rule_workbench import RuleTestCorpusRole

_INVESTIGATION_ID = UUID("7c768c42-63c5-44a3-9e14-9a10c8712e7a")
_SUBJECT_ID = UUID("7a7b2fa5-a5f1-427a-8f07-6fdaabbb1a39")


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

    async def get_by_address(self, logical_bucket: str, sha256: str):
        return next(
            (
                record
                for record in self.records.values()
                if record.descriptor.logical_bucket == logical_bucket
                and record.descriptor.sha256 == sha256
            ),
            None,
        )


class _Investigations:
    def __init__(self, status: AnalystInvestigationStatus) -> None:
        self.status = status

    async def get(self, investigation_id):
        if investigation_id != _INVESTIGATION_ID:
            return None
        return SimpleNamespace(id=investigation_id, subject_id=_SUBJECT_ID, status=self.status)


class _Subjects:
    async def get(self, subject_id):
        if subject_id != _SUBJECT_ID:
            return None
        return SimpleNamespace(id=subject_id, tlp=TLP.AMBER)


class _Samples:
    def __init__(self) -> None:
        self.items: dict[UUID, Sample] = {}

    async def get(self, sample_id):
        return self.items.get(sample_id)

    async def add(self, sample):
        self.items[sample.id] = sample

    async def get_by_subject_and_blob(self, subject_id, blob_id):
        return next(
            (
                sample
                for sample in self.items.values()
                if sample.subject_id == subject_id and sample.blob_id == blob_id
            ),
            None,
        )

    async def list_for_subject(self, subject_id):
        return tuple(sample for sample in self.items.values() if sample.subject_id == subject_id)


class _SampleAssociations:
    def __init__(self) -> None:
        self.items: dict[UUID, SampleAssociation] = {}

    async def get(self, association_id):
        return self.items.get(association_id)

    async def list_for_investigation(self, investigation_id):
        return tuple(
            item for item in self.items.values() if item.investigation_id == investigation_id
        )

    async def append_version(self, association, *, expected_version):
        assert expected_version == 0
        self.items[association.id] = association
        return association


class _SampleAcquisitionAttempts:
    async def find_successful(self, investigation_id, requested_hash):
        del investigation_id, requested_hash
        return None


class _Provenance:
    def __init__(self) -> None:
        self.items: list[ProvenanceEvent] = []

    async def append(self, event):
        self.items.append(event)

    async def list_for_aggregate(self, aggregate_type, aggregate_id):
        return [
            event
            for event in self.items
            if event.aggregate_type == aggregate_type and event.aggregate_id == aggregate_id
        ]


class _UnitOfWork:
    def __init__(self, harness: _Harness) -> None:
        self.analyst_investigations = _Investigations(harness.status)
        self.subjects = _Subjects()
        self.evidence_observations = harness.observations
        self.blobs = harness.blobs
        self.samples = harness.samples
        self.sample_associations = harness.sample_associations
        self.sample_acquisition_attempts = _SampleAcquisitionAttempts()
        self.provenance = harness.provenance
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
        self.samples = _Samples()
        self.sample_associations = _SampleAssociations()
        self.provenance = _Provenance()
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
        "intended_role": RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED,
        "provenance": "synthetic",
        "operator_id": "analyst:offline-operator",
        "reason": "Generated from a quoted configuration fragment.",
        "source_description": "Synthetic .NET RuntimeBroker configuration.",
    }

    imported = await harness.service.import_bytes(**payload)
    replay = await harness.service.import_bytes(**payload)

    assert imported.sha256 == hashlib.sha256(content).hexdigest()
    assert imported.size_bytes == len(content)
    assert imported.intended_role is RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED
    assert imported.format_magic == "xml_or_text"
    assert imported.provenance.value == "synthetic"
    assert replay.observation_id == imported.observation_id
    assert replay.blob_id == imported.blob_id
    assert imported.sample_id is not None
    assert imported.association_id is not None
    sample = harness.samples.items[imported.sample_id]
    assert sample.origin == "operator_import"
    assert sample.source_service == "operator_import"
    assert sample.origin_kind is SampleOrigin.MANUAL
    assert sample.state is SampleState.QUARANTINED
    assert sample.expected_hash == imported.sha256
    association = harness.sample_associations.items[imported.association_id]
    assert association.origin is AssociationSampleOrigin.MANUAL
    assert association.qualification is SampleQualification.CANDIDATE
    assert not association.changes
    assert any(
        event.aggregate_id == sample.id
        and event.event_type == "sample_imported_by_operator"
        and event.payload["provenance"] == "operator_import"
        for event in harness.provenance.items
    )
    resolved = await HitAcquisitionService(
        harness.uow_factory, acquisition=None, recorder=object()
    ).resolve_quarantined_sample(imported.association_id)
    assert resolved.sample_id == imported.sample_id
    assert resolved.sha256 == imported.sha256
    assert harness.blobs.content[(SAMPLES_QUARANTINE_BUCKET, imported.sha256)] == content
    assert replay.sample_id == imported.sample_id
    assert replay.association_id == imported.association_id
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


@pytest.mark.asyncio
async def test_import_persists_one_explicit_role_and_rejects_role_relabeling() -> None:
    harness = _Harness()
    content = b"ordinary benign fixture with a documented role"
    first = await harness.service.import_bytes(
        investigation_id=_INVESTIGATION_ID,
        content=content,
        intended_role=RuleTestCorpusRole.GOODWARE,
        provenance="real",
        operator_id="analyst:offline-operator",
        reason="Documented local benign baseline member.",
    )
    assert first.format_magic == "text"
    assert first.designated_at is None

    with pytest.raises(ManualRuleCorpusImportRejected) as conflict:
        await harness.service.import_bytes(
            investigation_id=_INVESTIGATION_ID,
            content=content,
            intended_role=RuleTestCorpusRole.HARD_NEGATIVE,
            provenance="real",
            operator_id="analyst:offline-operator",
            reason="Attempted conflicting label for the same bytes.",
        )
    assert conflict.value.reason is RuleCorpusImportRejection.ROLE_CONFLICT


@pytest.mark.asyncio
async def test_import_accepts_legacy_candidate_form_value_and_persists_explicit_role() -> None:
    harness = _Harness()
    imported = await harness.service.import_bytes(
        investigation_id=_INVESTIGATION_ID,
        content=b"candidate supplied through the legacy multipart form value",
        intended_role="out_of_cluster_candidate",
        provenance="synthetic",
        operator_id="analyst:offline-operator",
        reason="Compatibility with the current multipart schema.",
    )
    assert imported.intended_role is RuleTestCorpusRole.NEW_CANDIDATE_UNCONFIRMED


@pytest.mark.asyncio
async def test_holdout_role_is_timestamped_and_disjoint_by_sha() -> None:
    harness = _Harness()
    content = b"independent holdout bytes with a measured marker"
    sha256 = hashlib.sha256(content).hexdigest()
    designation = await harness.service.designate_holdout(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256=sha256,
        intended_role=RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        operator_id="analyst:offline-operator",
        reason="Reserved before drafting the candidate rule.",
    )
    holdout = await harness.service.import_bytes(
        investigation_id=_INVESTIGATION_ID,
        content=content,
        intended_role=RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        provenance="real",
        operator_id="analyst:offline-operator",
        reason="Reserved before drafting the candidate rule.",
    )
    assert holdout.designated_at is not None
    assert holdout.format_magic == "text"
    assert holdout.designated_at == designation.designated_at
    event = next(
        event
        for event in harness.provenance.items
        if event.event_type == "rule_corpus_holdout_designated"
    )
    assert event.actor_id == "analyst:offline-operator"
    assert event.payload["reason"] == "Reserved before drafting the candidate rule."
    assert event.occurred_at == designation.designated_at

    with pytest.raises(ManualRuleCorpusImportRejected) as overlap:
        await harness.service.import_bytes(
            investigation_id=_INVESTIGATION_ID,
            content=content,
            intended_role=RuleTestCorpusRole.GOODWARE,
            provenance="real",
            operator_id="analyst:offline-operator",
            reason="The same bytes cannot also be development goodware.",
        )
    assert overlap.value.reason is RuleCorpusImportRejection.HOLDOUT_OVERLAP


@pytest.mark.asyncio
async def test_holdout_upload_requires_prior_append_only_designation() -> None:
    harness = _Harness()
    content = b"holdout cannot self-designate while being imported"

    with pytest.raises(ManualRuleCorpusImportRejected) as rejected:
        await harness.service.import_bytes(
            investigation_id=_INVESTIGATION_ID,
            content=content,
            intended_role=RuleTestCorpusRole.HOLDOUT_NEGATIVE,
            provenance="real",
            operator_id="analyst:offline-operator",
            reason="A reason on upload does not replace designation.",
        )
    assert rejected.value.reason is RuleCorpusImportRejection.HOLDOUT_NOT_DESIGNATED
    assert harness.blobs.ingest_count == 0


@pytest.mark.parametrize("identity_field", ["sample_id", "sample_sha256"])
@pytest.mark.parametrize("expected_hash_kind", ["sha256", "sha1"])
@pytest.mark.asyncio
async def test_existing_quarantine_sample_gets_audited_role_without_upload_or_qualification(
    identity_field,
    expected_hash_kind,
) -> None:
    harness = _Harness()
    sample_bytes = b"already quarantined seed bytes"
    blob = await harness.blobs.ingest(
        BytesIO(sample_bytes),
        logical_bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    sha256 = hashlib.sha256(sample_bytes).hexdigest()
    sample = Sample(
        subject_id=_SUBJECT_ID,
        blob_id=blob.id,
        original_name="seed.bin",
        origin="source_seed",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=True,
        external_llm_allowed=False,
        origin_kind=SampleOrigin.SOURCE_SEED,
        state=SampleState.QUARANTINED,
        source_service="seed",
        source_object_id=sha256,
        expected_hash=(
            sha256 if expected_hash_kind == "sha256" else hashlib.sha1(sample_bytes).hexdigest()
        ),
    )
    harness.samples.items[sample.id] = sample
    ingests_before_assignment = harness.blobs.ingest_count
    identity = {identity_field: sample.id if identity_field == "sample_id" else sha256}

    assigned = await harness.service.assign_existing_sample(
        investigation_id=_INVESTIGATION_ID,
        **identity,
        intended_role=RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED,
        provenance="real",
        operator_id="analyst:offline-operator",
        reason="This existing seed belongs to the documented training corpus.",
        source_description="Seed already present in quarantine.",
    )

    assert assigned.sample_id == sample.id
    assert assigned.sha256 == sha256
    assert assigned.intended_role is RuleTestCorpusRole.DOCUMENTED_SEED_UNCONFIRMED
    assert assigned.format_magic == "quarantined_uninspected"
    assert harness.blobs.ingest_count == ingests_before_assignment
    association = harness.sample_associations.items[assigned.association_id]
    assert association.qualification is SampleQualification.CANDIDATE
    assert not association.changes
    event = next(
        event
        for event in harness.provenance.items
        if event.event_type == "sample_corpus_role_assigned_by_operator"
    )
    assert event.actor_id == "analyst:offline-operator"
    assert event.payload["sample_sha256"] == sha256
    assert event.payload["reason"] == (
        "This existing seed belongs to the documented training corpus."
    )


@pytest.mark.asyncio
async def test_existing_sample_role_cannot_create_human_confirmation() -> None:
    harness = _Harness()
    blob = await harness.blobs.ingest(
        BytesIO(b"unconfirmed quarantined bytes"),
        logical_bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    sha256 = blob.descriptor.sha256
    sample = Sample(
        subject_id=_SUBJECT_ID,
        blob_id=blob.id,
        original_name="unconfirmed.bin",
        origin="source_seed",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=True,
        external_llm_allowed=False,
        state=SampleState.QUARANTINED,
        expected_hash=sha256,
    )
    harness.samples.items[sample.id] = sample

    with pytest.raises(ManualRuleCorpusImportRejected) as rejected:
        await harness.service.assign_existing_sample(
            investigation_id=_INVESTIGATION_ID,
            sample_id=sample.id,
            intended_role=RuleTestCorpusRole.HUMAN_CONFIRMED,
            provenance="real",
            operator_id="analyst:offline-operator",
            reason="An operator corpus label cannot qualify the sample.",
        )
    assert rejected.value.reason is RuleCorpusImportRejection.HUMAN_CONFIRMATION_REQUIRED
    assert harness.sample_associations.items == {}


@pytest.mark.asyncio
async def test_existing_sample_holdout_role_requires_and_uses_prior_designation() -> None:
    harness = _Harness()
    sample_bytes = b"existing quarantine holdout bytes"
    blob = await harness.blobs.ingest(
        BytesIO(sample_bytes),
        logical_bucket=SAMPLES_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    sample = Sample(
        subject_id=_SUBJECT_ID,
        blob_id=blob.id,
        original_name="holdout.bin",
        origin="source_seed",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=True,
        external_llm_allowed=False,
        state=SampleState.QUARANTINED,
        expected_hash=blob.descriptor.sha256,
    )
    harness.samples.items[sample.id] = sample

    async def assign_existing_holdout():
        return await harness.service.assign_existing_sample(
            investigation_id=_INVESTIGATION_ID,
            sample_id=sample.id,
            intended_role=RuleTestCorpusRole.HOLDOUT_NEGATIVE,
            provenance="real",
            operator_id="analyst:offline-operator",
            reason="Reserve this existing sample as an independent holdout.",
        )

    with pytest.raises(ManualRuleCorpusImportRejected) as rejected:
        await assign_existing_holdout()
    assert rejected.value.reason is RuleCorpusImportRejection.HOLDOUT_NOT_DESIGNATED
    assert harness.sample_associations.items == {}

    designation = await harness.service.designate_holdout(
        investigation_id=_INVESTIGATION_ID,
        sample_sha256=blob.descriptor.sha256,
        intended_role=RuleTestCorpusRole.HOLDOUT_NEGATIVE,
        operator_id="analyst:offline-operator",
        reason="Reserve this existing sample as an independent holdout.",
    )
    assigned = await assign_existing_holdout()

    assert assigned.intended_role is RuleTestCorpusRole.HOLDOUT_NEGATIVE
    assert assigned.designated_at == designation.designated_at


@pytest.mark.asyncio
async def test_human_confirmed_import_requires_existing_human_qualification() -> None:
    harness = _Harness()
    with pytest.raises(ManualRuleCorpusImportRejected) as rejected:
        await harness.service.import_bytes(
            investigation_id=_INVESTIGATION_ID,
            content=b"bytes without a human membership decision",
            intended_role=RuleTestCorpusRole.HUMAN_CONFIRMED,
            provenance="real",
            operator_id="analyst:offline-operator",
            reason="The import itself cannot create a truth label.",
        )
    assert rejected.value.reason is RuleCorpusImportRejection.HUMAN_CONFIRMATION_REQUIRED


def test_rule_corpus_import_route_is_mounted_and_uses_multipart() -> None:
    from cti_app.api.main import create_app

    spec = create_app().openapi()
    path = "/api/investigations/{investigation_id}/rule-corpus/imports"

    assert path in spec["paths"]
    assert "get" in spec["paths"][path]
    request_body = spec["paths"][path]["post"]["requestBody"]["content"]
    assert "multipart/form-data" in request_body
    role_enum = request_body["multipart/form-data"]["schema"]["properties"]["intended_role"]["enum"]
    assert role_enum == [
        role.value for role in RuleTestCorpusRole if role is not RuleTestCorpusRole.BYTES_MISSING
    ]
    assert (
        "/api/investigations/{investigation_id}/rule-corpus/holdout-designations" in spec["paths"]
    )
    assert "/api/investigations/{investigation_id}/rule-corpus/roles" in spec["paths"]


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
        listing = client.get(f"/api/investigations/{_INVESTIGATION_ID}/rule-corpus/imports")

    assert first.status_code == 200, first.text
    assert replay.status_code == 200, replay.text
    first_body = first.json()
    assert first_body["sha256"] == hashlib.sha256(content).hexdigest()
    assert first_body["provenance"] == "real"
    assert first_body["source"] == "operator_import"
    assert first_body["sample_id"]
    assert first_body["association_id"]
    assert first_body["local_only"] is True
    assert first_body["executed"] is False
    assert replay.json()["observation_id"] == first_body["observation_id"]
    assert replay.json()["sample_id"] == first_body["sample_id"]
    assert replay.json()["association_id"] == first_body["association_id"]
    assert replay.json()["idempotent_replay"] is True
    assert listing.status_code == 200, listing.text
    listed = listing.json()
    assert listed["schema_version"] == "manual-rule-corpus-import-inventory.v1"
    assert len(listed["imports"]) == 1
    assert listed["imports"][0]["source_reference"] == f"sha256:{first_body['sha256']}"
    assert listed["imports"][0]["operator_id"] == "analyst:manual-import-test"
    assert "bytes" not in listed["imports"][0]

    with TestClient(app) as client:
        assigned = client.post(
            f"/api/investigations/{_INVESTIGATION_ID}/rule-corpus/roles",
            json={
                "sha256": first_body["sha256"],
                "intended_role": "goodware",
                "provenance": "real",
                "reason": "Confirm the existing quarantine sample's corpus role.",
                "source_description": "Existing operator import.",
            },
        )
        holdout_content = b"holdout designated before its upload"
        holdout_sha256 = hashlib.sha256(holdout_content).hexdigest()
        designation = client.post(
            f"/api/investigations/{_INVESTIGATION_ID}/rule-corpus/holdout-designations",
            json={
                "sha256": holdout_sha256,
                "intended_role": "holdout_negative",
                "reason": "Reserve this independent sample before rule creation.",
            },
        )
        imported_holdout = client.post(
            f"/api/investigations/{_INVESTIGATION_ID}/rule-corpus/imports",
            data={
                "intended_role": "holdout_negative",
                "provenance": "real",
                "reason": "Import the previously designated holdout.",
                "sha256": holdout_sha256,
            },
            files={"file": ("holdout.bin", holdout_content, "application/octet-stream")},
        )

    assert assigned.status_code == 200, assigned.text
    assert assigned.json()["bytes_exposed"] is False
    assert assigned.json()["executed"] is False
    assert assigned.json()["idempotent_replay"] is True
    assert designation.status_code == 200, designation.text
    assert designation.json()["append_only"] is True
    assert designation.json()["operator_id"] == "analyst:manual-import-test"
    assert imported_holdout.status_code == 200, imported_holdout.text
    assert imported_holdout.json()["sha256"] == holdout_sha256


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
