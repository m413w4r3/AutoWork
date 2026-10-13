from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4, uuid5

from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.virustotal import VirusTotalCapabilities
from cti_app.domain.entities import Sample
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    SampleAssociation,
    TLPLevel,
)
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    SampleAcquisitionAttempt,
    SampleAcquisitionOutcome,
)

_EVIDENCE_NAMESPACE = UUID("4244ce9f-6a7f-4b9b-b86d-454cc33f45a8")


@dataclass(frozen=True, slots=True)
class FakeBlobDescriptor:
    logical_bucket: str
    sha256: str
    size: int


@dataclass(frozen=True, slots=True)
class FakeBlob:
    id: UUID
    descriptor: FakeBlobDescriptor


class FakeDatabase:
    def __init__(self) -> None:
        self.investigations: dict[UUID, AnalystInvestigation] = {}
        self.envelopes: dict[UUID, InvestigationEnvelope] = {}
        self.associations: dict[UUID, SampleAssociation] = {}
        self.decisions: list[Any] = []
        self.observations: dict[UUID, EvidenceObservation] = {}
        self.attempts: list[SampleAcquisitionAttempt] = []
        self.samples: dict[UUID, Sample] = {}
        self.blobs: dict[UUID, FakeBlob] = {}
        self.provenance: list[Any] = []


class FakeUow:
    def __init__(self, database: FakeDatabase) -> None:
        self.db = database

    async def __aenter__(self) -> FakeUow:
        self.analyst_investigations = _Investigations(self.db)
        self.investigation_envelopes = _Envelopes(self.db)
        self.sample_associations = _Associations(self.db)
        self.analyst_decisions = _Decisions(self.db)
        self.evidence_observations = _Observations(self.db)
        self.sample_acquisition_attempts = _AcquisitionAttempts(self.db)
        self.samples = _Samples(self.db)
        self.blobs = _Blobs(self.db)
        self.provenance = _Provenance(self.db)
        self.investigation_goodware_baselines = _GoodwareBaselines()
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _Investigations:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        return self.db.investigations.get(investigation_id)

    async def save(self, investigation: AnalystInvestigation) -> None:
        self.db.investigations[investigation.id] = investigation


class _Envelopes:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def get(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        return self.db.envelopes.get(investigation_id)

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ) -> InvestigationEnvelope:
        current = self.db.envelopes.get(envelope.investigation_id)
        if current == envelope:
            return current
        if current is None:
            if expected_version is not None or envelope.version != 1:
                raise RuntimeError("envelope version conflict")
        elif current.version != expected_version or envelope.version != current.version + 1:
            raise RuntimeError("envelope version conflict")
        self.db.envelopes[envelope.investigation_id] = envelope
        return envelope


class _Associations:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def get(self, association_id: UUID) -> SampleAssociation | None:
        return self.db.associations.get(association_id)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return tuple(
            item
            for item in self.db.associations.values()
            if item.investigation_id == investigation_id
        )

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation:
        current = self.db.associations.get(association.id)
        if current is None:
            if expected_version != 0 or association.version != 1:
                raise RuntimeError("association version conflict")
        elif current.version != expected_version or association.version != current.version + 1:
            raise RuntimeError("association version conflict")
        self.db.associations[association.id] = association
        return association


class _Decisions:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def append(self, decision: Any) -> None:
        existing = next((item for item in self.db.decisions if item.id == decision.id), None)
        if existing is None:
            self.db.decisions.append(decision)
        elif existing != decision:
            raise ValueError("decision ID conflict")

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[Any, ...]:
        return tuple(
            item for item in self.db.decisions if item.investigation_id == investigation_id
        )


class _Observations:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def get(self, observation_id: UUID) -> EvidenceObservation | None:
        return self.db.observations.get(observation_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[EvidenceObservation, ...]:
        return tuple(
            item
            for item in self.db.observations.values()
            if item.investigation_id == investigation_id
        )

    async def append_if_absent(self, observation: EvidenceObservation) -> EvidenceObservation:
        existing = self.db.observations.get(observation.id)
        if existing is not None and existing != observation:
            raise ValueError("observation ID conflict")
        self.db.observations[observation.id] = observation
        return observation


class _AcquisitionAttempts:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def find_successful(
        self, investigation_id: UUID, requested_hash: str
    ) -> SampleAcquisitionAttempt | None:
        return next(
            (
                item
                for item in self.db.attempts
                if item.investigation_id == investigation_id
                and item.requested_hash == requested_hash
                and item.outcome is SampleAcquisitionOutcome.SUCCESS
            ),
            None,
        )

    async def append(self, attempt: SampleAcquisitionAttempt) -> None:
        if attempt.outcome is SampleAcquisitionOutcome.SUCCESS and any(
            current.outcome is SampleAcquisitionOutcome.SUCCESS
            and current.investigation_id == attempt.investigation_id
            and current.requested_hash == attempt.requested_hash
            for current in self.db.attempts
        ):
            raise RuntimeError("duplicate successful acquisition")
        self.db.attempts.append(attempt)


class _Samples:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def get(self, sample_id: UUID) -> Sample | None:
        return self.db.samples.get(sample_id)

    async def add(self, sample: Sample) -> None:
        self.db.samples[sample.id] = sample

    async def save(self, sample: Sample) -> None:
        self.db.samples[sample.id] = sample

    async def get_by_subject_and_blob(self, subject_id: UUID, blob_id: UUID) -> Sample | None:
        return next(
            (
                sample
                for sample in self.db.samples.values()
                if sample.subject_id == subject_id and sample.blob_id == blob_id
            ),
            None,
        )

    async def list_for_subject(self, subject_id: UUID) -> tuple[Sample, ...]:
        return tuple(item for item in self.db.samples.values() if item.subject_id == subject_id)


class _Blobs:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def get(self, blob_id: UUID) -> FakeBlob | None:
        return self.db.blobs.get(blob_id)


class _Provenance:
    def __init__(self, db: FakeDatabase) -> None:
        self.db = db

    async def append(self, event: Any) -> None:
        self.db.provenance.append(event)


class _GoodwareBaselines:
    async def get(self, investigation_id: UUID) -> UUID | None:
        return None


class FakeBlobCatalog:
    def __init__(self, database: FakeDatabase) -> None:
        self.db = database

    async def ingest(self, stream: BytesIO, *, logical_bucket: str, mime_type: str) -> FakeBlob:
        del mime_type
        content = stream.read()
        sha256 = hashlib.sha256(content).hexdigest()
        existing = next(
            (
                item
                for item in self.db.blobs.values()
                if item.descriptor.logical_bucket == logical_bucket
                and item.descriptor.sha256 == sha256
            ),
            None,
        )
        if existing is not None:
            return existing
        blob = FakeBlob(
            id=uuid4(),
            descriptor=FakeBlobDescriptor(logical_bucket, sha256, len(content)),
        )
        self.db.blobs[blob.id] = blob
        return blob


class FakeVirusTotalPort:
    def __init__(self, sample_bytes: bytes) -> None:
        self.sample_bytes = sample_bytes
        self.download_calls: list[str] = []

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        self.download_calls.append(file_hash)
        if hashlib.sha256(self.sample_bytes).hexdigest() != file_hash:
            raise ValueError("fake payload does not match requested SHA-256")
        sink.write(self.sample_bytes)


class FakeEvidenceRecorder:
    def __init__(self, observations: _Observations | None = None) -> None:
        self.observations = observations
        self.kinds: list[Any] = []

    async def record_existing_blob(self, **kwargs: Any) -> EvidenceObservation:
        return await self._record(**kwargs)

    async def record_json(self, **kwargs: Any) -> EvidenceObservation:
        payload = kwargs.pop("payload")
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        kwargs["sha256"] = hashlib.sha256(encoded).hexdigest()
        kwargs["blob_id"] = uuid4()
        return await self._record(**kwargs)

    async def _record(self, **kwargs: Any) -> EvidenceObservation:
        identity = str(kwargs.get("idempotency_key") or uuid4())
        observation = EvidenceObservation(
            id=uuid5(_EVIDENCE_NAMESPACE, identity),
            investigation_id=kwargs["investigation_id"],
            kind=kwargs["kind"],
            blob_id=kwargs["blob_id"],
            sha256=kwargs["sha256"],
            source=kwargs["source"],
            source_version=kwargs["source_version"],
            observed_at=kwargs.get("observed_at") or datetime.now(UTC),
            object_type=kwargs["object_type"],
            object_id=kwargs["object_id"],
            limitations=tuple(kwargs.get("limitations", ())),
            invocation_ref=kwargs.get("invocation_ref"),
        )
        self.kinds.append(observation.kind)
        if self.observations is not None:
            await self.observations.append_if_absent(observation)
        return observation


def make_investigation(
    *, subject_id: UUID | None = None, max_hits: int = 10, max_vt_units: int = 20
) -> AnalystInvestigation:
    return AnalystInvestigation(
        production_run_id=uuid4(),
        subject_id=subject_id or uuid4(),
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(max_vt_read_units=max_vt_units, max_hits_acquired=max_hits),
    )


def make_sample_acquisition_service(
    database: FakeDatabase,
    port: FakeVirusTotalPort,
) -> VirusTotalSampleAcquisitionService:
    def uow_factory() -> FakeUow:
        return FakeUow(database)

    return VirusTotalSampleAcquisitionService(
        port,
        VirusTotalCapabilities(file_download=True),
        FakeBlobCatalog(database),
        uow_factory,
        LocalIdentityProvider(),
    )


def make_evidence_recorder(database: FakeDatabase) -> FakeEvidenceRecorder:
    return FakeEvidenceRecorder(_Observations(database))


def make_approved_proposal(
    investigation: AnalystInvestigation,
    *,
    allowed_providers: tuple[str, ...] = ("vt",),
    acquire_hits_allowed: bool = True,
    tlp: TLPLevel | None = TLPLevel.CLEAR,
    do_not_submit: bool = False,
    vt_units: int = 20,
    downloads: int = 10,
) -> Any:
    from cti_app.application.investigation_governance import InvestigationEnvelopeProposal

    return InvestigationEnvelopeProposal(
        investigation_id=investigation.id,
        allowed_providers=allowed_providers,
        budget=InvestigationBudget(
            limits={
                BudgetDimension.VT_UNITS: vt_units,
                BudgetDimension.DOWNLOADS: downloads,
            }
        ),
        acquire_hits_allowed=acquire_hits_allowed,
        disclosure=DisclosureBounds(
            tlp=tlp,
            do_not_submit=do_not_submit,
            external_llm_allowed=False,
        ),
    )


def fake_article_snapshot(
    *, synthesis_artifact_id: UUID, documented_samples: tuple[str, ...] = ()
) -> Any:
    return SimpleNamespace(
        synthesis_artifact_id=synthesis_artifact_id,
        article=SimpleNamespace(
            short_summary="",
            documented_samples=documented_samples,
            open_questions=(),
            claims=(),
            known_chain=(),
        ),
    )


def make_article_novelty_service(
    database: FakeDatabase,
    investigation: AnalystInvestigation,
    *,
    documented_samples: tuple[str, ...] = (),
) -> Any:
    from cti_app.application.article_novelty import ArticleNoveltyService
    from cti_app.application.investigation_contracts import ArticleContextV1
    from cti_app.application.research_context_builder import ArticleSnapshot

    snapshot = ArticleSnapshot(
        subject_id=investigation.subject_id,
        production_run_id=investigation.production_run_id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        synthesis_sha256="a" * 64,
        article=ArticleContextV1(
            short_summary="Local C2 fixture.",
            documented_samples=documented_samples,
            open_questions=(),
            claims=(),
            known_chain=(),
        ),
    )

    class SnapshotPort:
        async def resolve(self, synthesis_artifact_id: UUID) -> Any:
            assert synthesis_artifact_id == snapshot.synthesis_artifact_id
            return snapshot

        async def get_claims(self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()) -> Any:
            del investigation_id
            return tuple(
                claim for claim in snapshot.article.claims if not claim_ids or claim.id in claim_ids
            )

    class InvestigationPort:
        async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
            return database.investigations.get(investigation_id)

    return ArticleNoveltyService(
        observations=_Observations(database),
        investigations=InvestigationPort(),
        article_snapshots=SnapshotPort(),
    )
