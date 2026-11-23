"""M24-03A oracles: VT bootstrap inventory, plan, snapshot and batch.

The offline harness wires the REAL VT tool adapters, the REAL acquisition
quarantine path, the REAL ``RegistryToolDispatcher`` and
``InvestigationService.execute_tool_call`` against the repository's existing
fake VT port and in-memory stores.  No test contacts a provider.
"""

from __future__ import annotations

import hashlib
import io
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.investigation_bootstrap import router as bootstrap_router
from cti_app.application.analyst_handoff import AnalystPostSynthesisService
from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.identity import (
    Identity,
    IdentityScope,
    LocalIdentityProvider,
)
from cti_app.application.investigation_acquisition import HitAcquisitionService
from cti_app.application.investigation_acquisition_tools import register_acquisition_tools
from cti_app.application.investigation_bootstrap import (
    MAX_INVENTORY_IOCS,
    BootstrapCoverageKind,
    BootstrapExportRefusedError,
    BootstrapHashAliasV1,
    BootstrapIocKind,
    BootstrapIocOrigin,
    BootstrapPlanState,
    BootstrapReason,
    BootstrapRouteStatus,
    BootstrapSnapshotV1,
    BootstrapSynthesisPinError,
    BootstrapVtCapabilities,
    InvestigationBootstrapService,
)
from cti_app.application.investigation_contracts import (
    ArticleClaimV1,
    ArticleContextV1,
    ConfirmedIOCContextV1,
)
from cti_app.application.investigation_governance import SampleQualificationService
from cti_app.application.investigation_persistence import (
    InMemoryAnalystOpinionRepository,
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_service import (
    InvestigationService,
    ProductionArtifactObservationWriter,
)
from cti_app.application.investigation_state import (
    InMemoryInvestigationContextSnapshotRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
)
from cti_app.application.investigation_vt_tools import (
    register_virustotal_tools,
    virustotal_tool_capabilities,
)
from cti_app.application.research_context_builder import (
    ArticlePassageSnapshot,
    ArticleSnapshot,
    ResearchContextBuilder,
)
from cti_app.application.sample_acquisition import VirusTotalSampleAcquisitionService
from cti_app.application.virustotal import (
    VirusTotalCapabilities,
    VirusTotalHttpError,
    VirusTotalObjectReport,
    VirusTotalQuotaExceededError,
    VirusTotalRouteUnavailableError,
)
from cti_app.application.workspace_agent import WorkspaceBindingV1
from cti_app.config import Settings
from cti_app.domain.classification import TLP
from cti_app.domain.entities import Sample, SampleState
from cti_app.domain.investigation import (
    AgendaItem,
    BudgetDimension,
    DisclosureBounds,
    EvidenceKind,
    InvestigationAgenda,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocationStatus,
    tool_operation_key,
)
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.domain.production import AnalystInvestigation, LoopBudget, ProductionRun
from cti_app.domain.production_routing import (
    ProductionRoutingDecisionV1,
    RoutingReasonCode,
    RoutingStatus,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.virustotal import (
    VirusTotalBehaviourSummary,
    VirusTotalEndpointVariant,
    VirusTotalTransportKind,
)
from tests.investigation_c2_fakes import FakeBlobCatalog, FakeDatabase
from tests.test_investigation_vt_tools import (
    FakeVirusTotalObservationService,
    FakeVirusTotalPort,
    _file_report,
    _page,
)

_SAMPLE_BYTES = b"synthetic-bootstrap-sample"
_DEFAULT_SEED = hashlib.sha256(_SAMPLE_BYTES).hexdigest()
_SEED_MD5 = "b" * 32
_IP = "203.0.113.10"
_DOMAIN = "malware.example"
_URL = "https://malware.example/payload.bin"
_DOCUMENT_ID = UUID("9f3f43a0-1c1b-4a37-8b4b-7a1f8b6c2d31")
_FIXED_NOW = datetime(2026, 11, 22, 9, 30, tzinfo=UTC)
_QUARANTINE_BUCKET = "samples-quarantine"


# ---------------------------------------------------------------------------
# Canonical stores (in-memory, same contracts as the SQLAlchemy repositories)
# ---------------------------------------------------------------------------


class _Investigations:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        return self._db.investigations.get(investigation_id)

    async def save(self, investigation: AnalystInvestigation) -> None:
        self._db.investigations[investigation.id] = investigation


class _Envelopes:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        return self._db.envelopes.get(investigation_id)

    async def get_for_update(self, investigation_id: UUID) -> InvestigationEnvelope | None:
        return await self.get(investigation_id)

    async def save_if_version(
        self, envelope: InvestigationEnvelope, *, expected_version: int | None
    ) -> InvestigationEnvelope:
        current = self._db.envelopes.get(envelope.investigation_id)
        if current == envelope:
            return current
        if current is None:
            if expected_version is not None or envelope.version != 1:
                raise RuntimeError("envelope version conflict")
        elif current.version != expected_version or envelope.version != current.version + 1:
            raise RuntimeError("envelope version conflict")
        self._db.envelopes[envelope.investigation_id] = envelope
        return envelope


class _States:
    def __init__(self) -> None:
        self.items: dict[UUID, InvestigationWorkingState] = {}

    async def get(self, investigation_id: UUID) -> InvestigationWorkingState | None:
        return self.items.get(investigation_id)

    async def save_if_version(
        self, state: InvestigationWorkingState, *, expected_version: int | None
    ) -> InvestigationWorkingState:
        current = self.items.get(state.investigation_id)
        if current is None and expected_version is not None:
            raise RuntimeError("state version conflict")
        if current is not None and current.version != expected_version:
            raise RuntimeError("state version conflict")
        self.items[state.investigation_id] = state
        return state


class _Associations:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, association_id: UUID) -> SampleAssociation | None:
        return self._db.associations.get(association_id)

    async def list_for_investigation(self, investigation_id: UUID) -> tuple[SampleAssociation, ...]:
        return tuple(
            item
            for item in self._db.associations.values()
            if item.investigation_id == investigation_id
        )

    async def append_version(
        self, association: SampleAssociation, *, expected_version: int
    ) -> SampleAssociation:
        current = self._db.associations.get(association.id)
        if current == association:
            return current
        if current is None:
            if expected_version != 0 or association.version != 1:
                raise RuntimeError("association version conflict")
        elif current.version != expected_version or association.version != current.version + 1:
            raise RuntimeError("association version conflict")
        self._db.associations[association.id] = association
        return association

    async def link_sample(
        self, association_id: UUID, sample_id: UUID, *, expected_sha256: str
    ) -> None:
        association = self._db.associations.get(association_id)
        sample = self._db.samples.get(sample_id)
        if association is None or association.sample_sha256 != expected_sha256 or sample is None:
            raise ValueError("sample association link does not match")
        blob = self._db.blobs.get(sample.blob_id)
        investigation = self._db.investigations.get(association.investigation_id)
        if (
            blob is None
            or blob.descriptor.sha256 != expected_sha256
            or investigation is None
            or sample.subject_id != investigation.subject_id
        ):
            raise ValueError("sample association link does not match its investigation")
        existing = self._db.association_sample_ids.get(association_id)
        if existing not in {None, sample_id}:
            raise ValueError("sample association is already linked to another sample")
        self._db.association_sample_ids[association_id] = sample_id


class _Samples:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, sample_id: UUID) -> Sample | None:
        return self._db.samples.get(sample_id)

    async def add(self, sample: Sample) -> None:
        self._db.samples[sample.id] = sample

    async def add_if_absent(self, sample: Sample) -> Sample:
        existing = await self.get_by_subject_and_blob(sample.subject_id, sample.blob_id)
        if existing is not None:
            return existing
        self._db.samples[sample.id] = sample
        return sample

    async def save(self, sample: Sample) -> None:
        self._db.samples[sample.id] = sample

    async def get_by_subject_and_blob(self, subject_id: UUID, blob_id: UUID) -> Sample | None:
        return next(
            (
                sample
                for sample in self._db.samples.values()
                if sample.subject_id == subject_id and sample.blob_id == blob_id
            ),
            None,
        )

    async def list_for_subject(self, subject_id: UUID) -> tuple[Sample, ...]:
        return tuple(item for item in self._db.samples.values() if item.subject_id == subject_id)

    async def list_by_subject_and_sha256(self, subject_id: UUID, sha256: str) -> tuple[Sample, ...]:
        matches: list[Sample] = []
        for sample in self._db.samples.values():
            blob = self._db.blobs.get(sample.blob_id)
            if sample.subject_id != subject_id:
                continue
            if sample.expected_hash == sha256 or (
                blob is not None and blob.descriptor.sha256 == sha256
            ):
                matches.append(sample)
        return tuple(matches)


class _Attempts:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def find_successful(self, investigation_id: UUID, requested_hash: str) -> Any | None:
        from cti_app.domain.production import SampleAcquisitionOutcome

        return next(
            (
                item
                for item in self._db.attempts
                if item.investigation_id == investigation_id
                and item.requested_hash == requested_hash
                and item.outcome is SampleAcquisitionOutcome.SUCCESS
            ),
            None,
        )

    async def find_latest_error(self, investigation_id: UUID, requested_hash: str) -> Any | None:
        from cti_app.domain.production import SampleAcquisitionOutcome

        return next(
            (
                item
                for item in reversed(self._db.attempts)
                if item.investigation_id == investigation_id
                and item.requested_hash == requested_hash
                and item.outcome is SampleAcquisitionOutcome.ERROR
            ),
            None,
        )

    async def append(self, attempt: Any) -> None:
        self._db.attempts.append(attempt)


class _Blobs:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, blob_id: UUID) -> Any | None:
        return self._db.blobs.get(blob_id)

    async def get_by_address(self, logical_bucket: str, sha256: str) -> Any | None:
        return next(
            (
                blob
                for blob in self._db.blobs.values()
                if blob.descriptor.logical_bucket == logical_bucket
                and blob.descriptor.sha256 == sha256
            ),
            None,
        )


class _Bindings:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, workspace_id: str) -> WorkspaceBindingV1 | None:
        return self._db.bindings.get(workspace_id)

    async def get_for_investigation(self, investigation_id: UUID) -> WorkspaceBindingV1 | None:
        return next(
            (
                item
                for item in self._db.bindings.values()
                if item.investigation_id == investigation_id
            ),
            None,
        )

    async def add_if_absent(self, binding: WorkspaceBindingV1) -> tuple[WorkspaceBindingV1, bool]:
        existing = await self.get_for_investigation(binding.investigation_id)
        if existing is not None:
            if existing != binding:
                raise ValueError("An investigation can bind only one workspace identity")
            return existing, False
        self._db.bindings[binding.workspace_id] = binding
        return binding, True


class _Runs:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self._db.runs.get(run_id)


class _Routing:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, decision_id: UUID) -> ProductionRoutingDecisionV1 | None:
        return self._db.routing.get(decision_id)


class _Provenance:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def append(self, event: Any) -> None:
        self._db.provenance.append(event)

    async def list_for_aggregate(self, aggregate_type: str, aggregate_id: UUID) -> tuple[Any, ...]:
        return tuple(
            item
            for item in self._db.provenance
            if item.aggregate_type == aggregate_type and item.aggregate_id == aggregate_id
        )


class _SourceDocuments:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self._db = db

    async def get(self, document_id: UUID) -> SimpleNamespace | None:
        return self._db.source_documents.get(document_id)

    async def add(self, document: Any) -> None:
        self._db.source_documents[document.id] = document

    async def save(self, document: Any) -> None:
        self._db.source_documents[document.id] = document

    async def list_for_subject(self, subject_id: UUID) -> tuple[Any, ...]:
        return tuple(
            item for item in self._db.source_documents.values() if item.subject_id == subject_id
        )


class _BootstrapDatabase(FakeDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.invocations = InMemoryToolInvocationRepository()
        self.evidence = InMemoryEvidenceObservationRepository()
        self.findings = InMemoryFindingRepository()
        self.opinions = InMemoryAnalystOpinionRepository()
        self.states = _States()
        self.context_snapshots = InMemoryInvestigationContextSnapshotRepository()
        self.bindings: dict[str, WorkspaceBindingV1] = {}
        self.runs: dict[UUID, ProductionRun] = {}
        self.routing: dict[UUID, ProductionRoutingDecisionV1] = {}
        self.source_documents: dict[UUID, SimpleNamespace] = {}


class _BootstrapUow:
    def __init__(self, db: _BootstrapDatabase) -> None:
        self.db = db

    async def __aenter__(self) -> _BootstrapUow:
        db = self.db
        self.analyst_investigations = _Investigations(db)
        self.investigation_envelopes = _Envelopes(db)
        self.investigation_states = db.states
        self.context_snapshots = db.context_snapshots
        self.sample_associations = _Associations(db)
        self.samples = _Samples(db)
        self.sample_acquisition_attempts = _Attempts(db)
        self.blobs = _Blobs(db)
        self.evidence_observations = db.evidence
        self.tool_invocations = db.invocations
        self.investigation_findings = db.findings
        self.analyst_opinions = db.opinions
        self.workspace_bindings = _Bindings(db)
        self.production_runs = _Runs(db)
        self.production_routing_decisions = _Routing(db)
        self.source_documents = _SourceDocuments(db)
        self.provenance = _Provenance(db)
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


class _FakeArtifactStore:
    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}

    async def put_canonical_json(self, payload: dict[str, Any], *, bucket: str) -> tuple[UUID, str]:
        del bucket
        encoded = json.dumps(
            payload, ensure_ascii=False, allow_nan=False, separators=(",", ":"), sort_keys=True
        ).encode()
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id, hashlib.sha256(encoded).hexdigest()

    async def read_json(self, blob_id: UUID) -> dict[str, Any]:
        return self.payloads[blob_id]


class _BootstrapVtPort(FakeVirusTotalPort):
    """Repository fake VT port extended with the existing download path."""

    def __init__(self, sample_bytes: bytes) -> None:
        super().__init__()
        self.sample_bytes = sample_bytes
        self.download_calls: list[str] = []

    async def file_download(self, file_hash: str, *, sink: Any) -> None:
        self.download_calls.append(file_hash)
        error = self.errors.get("file_download")
        if error is not None:
            raise error
        if hashlib.sha256(self.sample_bytes).hexdigest() != file_hash:
            raise ValueError("fake payload does not match the requested SHA-256")
        sink.write(self.sample_bytes)


class _ArticlePort:
    def __init__(self, snapshot: ArticleSnapshot) -> None:
        self.snapshot = snapshot
        self.resolve_calls = 0

    async def resolve(self, synthesis_artifact_id: UUID) -> ArticleSnapshot:
        self.resolve_calls += 1
        assert synthesis_artifact_id == self.snapshot.synthesis_artifact_id
        return self.snapshot

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()
    ) -> tuple[ArticleClaimV1, ...]:
        del investigation_id
        return tuple(
            item for item in self.snapshot.article.claims if not claim_ids or item.id in claim_ids
        )


@dataclass(frozen=True)
class _IocSpec:
    indicator_type: str
    value: str
    context: str = ""
    evidence_quote: str = ""
    source_document_ids: tuple[UUID, ...] = (_DOCUMENT_ID,)
    extraction_artifact_id: UUID = field(default_factory=uuid4)
    extraction_artifact_version: int = 1


@dataclass
class _Harness:
    db: _BootstrapDatabase
    investigation: AnalystInvestigation
    snapshot: ArticleSnapshot
    article_port: _ArticlePort
    artifact_store: _FakeArtifactStore
    bootstrap: InvestigationBootstrapService
    executor: InvestigationService
    vt: _BootstrapVtPort
    archive: FakeVirusTotalObservationService
    envelope: InvestigationEnvelope | None
    capabilities: BootstrapVtCapabilities
    now: datetime

    def uow(self) -> _BootstrapUow:
        return _BootstrapUow(self.db)

    def set_envelope(self, envelope: InvestigationEnvelope | None) -> None:
        if envelope is None:
            self.db.envelopes.pop(self.investigation.id, None)
        else:
            self.db.envelopes[self.investigation.id] = envelope
        self.envelope = envelope

    async def run(
        self,
        *,
        idempotency_key: str = "bootstrap-run-1",
        executor: Any | None = None,
        actor_id: str = "analyst-test",
    ) -> BootstrapSnapshotV1:
        return await self.bootstrap.execute_batch(
            self.investigation.id,
            idempotency_key=idempotency_key,
            actor_id=actor_id,
            executor_factory=lambda _uow: executor or self.executor,
        )


_CHAIN_TYPE = {
    "md5": "hash",
    "sha1": "hash",
    "sha256": "hash",
    "ipv4": "ip",
    "domain": "domain",
    "url": "url",
}


def _article_snapshot(
    *,
    investigation: AnalystInvestigation,
    specs: Sequence[_IocSpec],
    synthesis_sha256: str,
    passages: tuple[ArticlePassageSnapshot, ...] | None = None,
) -> ArticleSnapshot:
    known_chain: list[dict[str, Any]] = []
    confirmed: list[ConfirmedIOCContextV1] = []
    for spec in specs:
        known_chain.append(
            {
                "kind": "documented_ioc",
                "artifact_type": _CHAIN_TYPE[spec.indicator_type],
                "value": spec.value,
                "status": "confirmed_ioc",
                "context": spec.context,
                "evidence_quote": spec.evidence_quote,
                "source_refs": sorted(str(item) for item in spec.source_document_ids),
                "extraction_artifact_id": str(spec.extraction_artifact_id),
                "extraction_artifact_version": spec.extraction_artifact_version,
            }
        )
        confirmed.append(
            ConfirmedIOCContextV1(
                indicator_type=spec.indicator_type,  # type: ignore[arg-type]
                value=spec.value,
                extraction_artifact_id=spec.extraction_artifact_id,
                extraction_artifact_version=spec.extraction_artifact_version,
                source_document_ids=spec.source_document_ids,
            )
        )
    if passages is None:
        text = "L'article cite : " + ", ".join(spec.value for spec in specs)
        passages = (
            ArticlePassageSnapshot(
                claim_id="L1.1",
                section_index=None,
                heading="Introduction",
                text=text,
                source_document_ids=(_DOCUMENT_ID,),
            ),
        )
    documented = tuple(spec.value for spec in specs if spec.indicator_type == "sha256")
    article = ArticleContextV1(
        short_summary="Article synthétique M24-03A.",
        claims=(
            ArticleClaimV1(id="L1.1", statement="Le paragraphe cite les IOC.", source_refs=()),
        ),
        documented_samples=documented,
        known_chain=tuple(known_chain[:256]),
        open_questions=("Les échantillons cités sont-ils toujours joignables ?",),
    )
    return ArticleSnapshot(
        subject_id=investigation.subject_id,
        production_run_id=investigation.production_run_id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        synthesis_sha256=synthesis_sha256,
        article=article,
        tlp="GREEN",
        do_not_submit=False,
        external_llm_allowed=True,
        confirmed_iocs=tuple(confirmed),
        article_passages=passages,
        synthesis_version=1,
    )


def _route_decision(
    run: ProductionRun, *, kind: PublicationKind = PublicationKind.ARTICLE
) -> ProductionRoutingDecisionV1:
    return ProductionRoutingDecisionV1(
        decision_id=uuid4(),
        production_run_id=run.id,
        pipeline_generation=run.pipeline_generation,
        extraction_artifact_id=None,
        extraction_artifact_hash=None,
        relevance_artifact_id=None,
        relevance_artifact_hash=None,
        input_hash="1" * 64,
        policy_version="routing-policy-v1",
        status=RoutingStatus.DECIDED,
        publication_kind=kind,
        reason_code=RoutingReasonCode.SUBJECT_IOC_CONFIRMED,
        confirmed_ioc_count=1,
        contextual_ioc_count=0,
        ambiguous_ioc_count=0,
        critical_source_diagnostics=(),
        decided_at=_FIXED_NOW,
        decision_hash="2" * 64,
    )


def _object_report(kind: str, object_id: str) -> VirusTotalObjectReport:
    import base64

    expected_id = (
        base64.urlsafe_b64encode(object_id.encode("utf-8")).decode("ascii").rstrip("=")
        if kind == "urls"
        else object_id
    )
    expected_type = {"ip_addresses": "ip_address", "domains": "domain", "urls": "url"}[kind]
    raw = json.dumps(
        {
            "data": {
                "id": expected_id,
                "type": expected_type,
                "attributes": {
                    "reputation": -12,
                    "last_analysis_date": 1_781_300_000,
                    "last_modification_date": 1_781_400_000,
                    "country": "NL",
                },
            }
        }
    ).encode()
    return VirusTotalObjectReport(
        kind=kind,  # type: ignore[arg-type]
        object_id=expected_id,
        raw_json=raw,
        http_status=200,
        transport=VirusTotalTransportKind.PROXY,
        api_generation=VirusTotalEndpointVariant.V3,
    )


def _relationship_page(
    *,
    next_cursor: str | None = None,
    exhaustive: bool = True,
    item_id: str = "related-1",
    item_type: str = "domain",
) -> Any:
    return _page(
        (
            {
                "id": item_id,
                "type": item_type,
                "attributes": {"meaningful_name": "related.bin"},
            },
        ),
        next_cursor=next_cursor,
        exhaustive=exhaustive,
    )


def _behaviour_page() -> Any:
    return _page(
        (
            {
                "id": "sandbox-report-1",
                "type": "file_behaviour",
                "attributes": {"sandbox_name": "VirusTotal Jujubox"},
            },
        ),
        behaviour_summaries=(
            VirusTotalBehaviourSummary(
                sandbox="VirusTotal Jujubox",
                date=1_781_300_000,
                tags=("peexe",),
                has_pcap=True,
                has_html_report=True,
                available_sections=("summary", "network"),
            ),
        ),
    )


def _envelope(
    investigation: AnalystInvestigation,
    *,
    allowed_providers: tuple[str, ...] = ("vt",),
    acquire_hits_allowed: bool = True,
    tlp: TLPLevel = TLPLevel.GREEN,
    external_llm_allowed: bool = True,
    vt_units: int = 200,
    downloads: int = 10,
) -> InvestigationEnvelope:
    return InvestigationEnvelope(
        investigation_id=investigation.id,
        allowed_providers=allowed_providers,
        budget=InvestigationBudget(
            limits={
                BudgetDimension.VT_UNITS: vt_units,
                BudgetDimension.DOWNLOADS: downloads,
                BudgetDimension.TOOL_SECONDS: 3_600,
            }
        ),
        acquire_hits_allowed=acquire_hits_allowed,
        disclosure=DisclosureBounds(
            tlp=tlp,
            do_not_submit=False,
            external_llm_allowed=external_llm_allowed,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-test",
    )


def _harness(
    *,
    specs: Sequence[_IocSpec] | None = None,
    passages: tuple[ArticlePassageSnapshot, ...] | None = None,
    publication_kind: PublicationKind = PublicationKind.ARTICLE,
    with_envelope: bool = True,
    allowed_providers: tuple[str, ...] = ("vt",),
    acquire_hits_allowed: bool = True,
    tlp: TLPLevel = TLPLevel.GREEN,
    external_llm_allowed: bool = True,
    vt_units: int = 200,
    downloads: int = 10,
    capabilities: BootstrapVtCapabilities | None = None,
    sample_bytes: bytes = _SAMPLE_BYTES,
    now: datetime = _FIXED_NOW,
) -> _Harness:
    selected_specs = (
        tuple(specs)
        if specs is not None
        else (_IocSpec("sha256", hashlib.sha256(sample_bytes).hexdigest(), context="IOC row 1"),)
    )
    caps = capabilities or BootstrapVtCapabilities(
        file_report=True,
        relationships=True,
        behaviour=True,
        download=True,
    )
    db = _BootstrapDatabase()
    subject_id = uuid4()
    run = ProductionRun(subject_id=subject_id, edition_id=uuid4())
    decision = _route_decision(run, kind=publication_kind)
    run.routing_decision_id = decision.decision_id
    db.runs[run.id] = run
    db.routing[decision.decision_id] = decision
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject_id,
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(
            max_cycles=12,
            max_pivot_runs=20,
            max_hits_acquired=10,
            max_new_samples=10,
            max_vt_read_units=50,
        ),
    )
    db.investigations[investigation.id] = investigation
    snapshot = _article_snapshot(
        investigation=investigation,
        specs=selected_specs,
        synthesis_sha256="4" * 64,
        passages=passages,
    )
    article_port = _ArticlePort(snapshot)
    db.source_documents[_DOCUMENT_ID] = SimpleNamespace(
        id=_DOCUMENT_ID,
        subject_id=subject_id,
        tlp="GREEN",
        do_not_submit=False,
        external_llm_allowed=True,
        license_restriction=None,
    )
    envelope = None
    if with_envelope:
        envelope = _envelope(
            investigation,
            allowed_providers=allowed_providers,
            acquire_hits_allowed=acquire_hits_allowed,
            tlp=tlp,
            external_llm_allowed=external_llm_allowed,
            vt_units=vt_units,
            downloads=downloads,
        )
        db.envelopes[investigation.id] = envelope
    digest = hashlib.sha256(
        f"ws-{investigation.id}:{snapshot.synthesis_sha256}".encode()
    ).hexdigest()[:32]
    db.bindings[f"ws-{digest}"] = WorkspaceBindingV1(
        workspace_id=f"ws-{digest}",
        investigation_id=investigation.id,
        subject_id=subject_id,
        delivery_code="IR202611",
        article_slug="synthetic-article",
        synthesis_sha256=snapshot.synthesis_sha256,
        created_at=now,
    )
    db.states.items[investigation.id] = InvestigationWorkingState(
        investigation_id=investigation.id,
        agenda=InvestigationAgenda(
            items=(
                AgendaItem(question="Quels IOC sont encore joignables ?", potential=1.0, rank=1),
                AgendaItem(
                    question="Quelle chaîne d'infection est confirmée ?",
                    potential=0.9,
                    rank=2,
                ),
            )
        ),
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )

    artifact_store = _FakeArtifactStore()
    vt = _BootstrapVtPort(sample_bytes)
    archive = FakeVirusTotalObservationService()
    recorder = ProductionEvidenceRecorder(artifact_store, db.evidence)
    vt_capabilities = VirusTotalCapabilities(
        file_report=caps.file_report,
        file_relationships=caps.relationships,
        behaviour_reports=caps.behaviour,
        file_download=caps.download,
    )
    registry = InMemoryToolRegistry()

    async def resolve_vt_article(investigation_id: UUID) -> ArticleSnapshot:
        assert investigation_id == investigation.id
        return snapshot

    register_virustotal_tools(
        registry,
        virustotal=vt,
        observations=archive,
        recorder=recorder,
        evidence_observations=db.evidence,
        findings=db.findings,
        article_snapshot_resolver=resolve_vt_article,
        capabilities=vt_capabilities,
        evidence_payloads=artifact_store,
        max_pages=3,
        page_size=40,
        max_results=20,
        max_tool_seconds=120,
    )
    uow_factory = lambda: _BootstrapUow(db)  # noqa: E731
    acquisition = VirusTotalSampleAcquisitionService(
        vt,
        VirusTotalCapabilities(file_download=True),
        FakeBlobCatalog(db),
        uow_factory,
        LocalIdentityProvider(),
    )
    hits = HitAcquisitionService(uow_factory, acquisition=acquisition, recorder=recorder)
    register_acquisition_tools(
        registry,
        hits=hits,
        qualifications=SampleQualificationService(uow_factory),
    )
    available = set(virustotal_tool_capabilities(vt_capabilities))
    if caps.download:
        available.add("vt.file_download")
    executor = InvestigationService(
        post_synthesis=AnalystPostSynthesisService(uow_factory, artifact_store),
        investigations=_Investigations(db),
        states=db.states,
        context_snapshots=db.context_snapshots,
        envelopes=_Envelopes(db),
        context_builder=ResearchContextBuilder(
            article_snapshots=article_port,
            observations=db.evidence,
            findings=db.findings,
        ),
        bridge=BridgeInvestigator(ScriptedTurnTransport(())),
        registry=registry,
        dispatcher=RegistryToolDispatcher(registry),
        invocations=db.invocations,
        observations=db.evidence,
        findings=db.findings,
        opinions=db.opinions,
        sample_associations=_Associations(db),
        samples=_Samples(db),
        source_documents=_SourceDocuments(db),
        observation_writer=ProductionArtifactObservationWriter(artifact_store, db.evidence),
        artifact_store=artifact_store,
        available_capabilities=frozenset(available),
        no_progress_limit=3,
    )
    bootstrap = InvestigationBootstrapService(
        uow_factory=uow_factory,
        article_snapshots=article_port,
        artifact_store=artifact_store,
        capabilities=caps,
        clock=lambda: now,
    )
    vt.report = _file_report(
        next(spec.value for spec in selected_specs if spec.indicator_type == "sha256")
        if any(spec.indicator_type == "sha256" for spec in selected_specs)
        else hashlib.sha256(sample_bytes).hexdigest()
    )
    for spec in selected_specs:
        if spec.indicator_type == "ipv4":
            vt.object_reports[("ip_addresses", spec.value)] = _object_report(
                "ip_addresses", spec.value
            )
        elif spec.indicator_type == "domain":
            vt.object_reports[("domains", spec.value)] = _object_report("domains", spec.value)
        elif spec.indicator_type == "url":
            vt.object_reports[("urls", spec.value)] = _object_report("urls", spec.value)
    return _Harness(
        db=db,
        investigation=investigation,
        snapshot=snapshot,
        article_port=article_port,
        artifact_store=artifact_store,
        bootstrap=bootstrap,
        executor=executor,
        vt=vt,
        archive=archive,
        envelope=envelope,
        capabilities=caps,
        now=now,
    )


def _snapshot_operations(snapshot: BootstrapSnapshotV1) -> dict[str, Any]:
    return {operation.tool_name: operation for ioc in snapshot.iocs for operation in ioc.operations}


class _NoopExecutor:
    async def execute_tool_call(self, **_kwargs: Any) -> Any:
        raise AssertionError("no provider call expected")


# ---------------------------------------------------------------------------
# Oracles
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_b01_synthesis_pinned_before_workspace() -> None:
    harness = _harness()
    harness.db.bindings.clear()
    await _Bindings(harness.db).add_if_absent(
        WorkspaceBindingV1(
            workspace_id="ws-" + "a" * 32,
            investigation_id=harness.investigation.id,
            subject_id=harness.investigation.subject_id,
            delivery_code="IR202611",
            article_slug="synthetic-article",
            synthesis_sha256=harness.snapshot.synthesis_sha256,
            created_at=harness.now,
        )
    )
    inventory = await harness.bootstrap.inventory(harness.investigation.id)
    assert inventory.pinned_synthesis_sha256 == harness.snapshot.synthesis_sha256
    assert inventory.workspace_synthesis_sha256 == harness.snapshot.synthesis_sha256
    assert inventory.workspace_binding_id is not None
    assert harness.vt.calls["file_report"] == 0

    harness.db.bindings[inventory.workspace_binding_id or ""] = WorkspaceBindingV1(
        workspace_id=inventory.workspace_binding_id or "ws-missing",
        investigation_id=harness.investigation.id,
        subject_id=harness.investigation.subject_id,
        delivery_code="IR202611",
        article_slug="synthetic-article",
        synthesis_sha256="f" * 64,
        created_at=harness.now,
    )
    with pytest.raises(BootstrapSynthesisPinError) as refused:
        await harness.bootstrap.inventory(harness.investigation.id)
    assert refused.value.code == "workspace_synthesis_pin_mismatch"


@pytest.mark.asyncio
async def test_b02_ioc_inventory_separates_seeds_from_discovered() -> None:
    harness = _harness(
        specs=(
            _IocSpec("sha256", _DEFAULT_SEED, context="row 1"),
            _IocSpec("ipv4", _IP, context="row 2"),
            _IocSpec("domain", _DOMAIN, context="row 3"),
            _IocSpec("url", _URL, context="row 4"),
        )
    )
    inventory = await harness.bootstrap.inventory(harness.investigation.id)
    assert inventory.discovered == ()
    assert {ioc.kind for ioc in inventory.seeds} == {
        BootstrapIocKind.SAMPLE,
        BootstrapIocKind.IPV4,
        BootstrapIocKind.DOMAIN,
        BootstrapIocKind.URL,
    }
    assert all(ioc.origin is BootstrapIocOrigin.SEED for ioc in inventory.seeds)
    sample = next(ioc for ioc in inventory.seeds if ioc.kind is BootstrapIocKind.SAMPLE)
    assert sample.value == _DEFAULT_SEED and sample.hash_type == "sha256"
    assert sample.claims == ("L1.1",)
    assert sample.sources and _DOCUMENT_ID in sample.sources[0].source_document_ids
    assert harness.vt.calls["file_report"] == 0


@pytest.mark.asyncio
async def test_b03_quarantined_samples_keep_source_associations() -> None:
    harness = _harness()
    catalog = FakeBlobCatalog(harness.db)
    blob = await catalog.ingest(
        io.BytesIO(_SAMPLE_BYTES),
        logical_bucket=_QUARANTINE_BUCKET,
        mime_type="application/octet-stream",
    )
    assert blob.descriptor.sha256 == _DEFAULT_SEED
    sample = Sample(
        subject_id=harness.investigation.subject_id,
        blob_id=blob.id,
        original_name="seed.bin",
        origin="vt_download",
        acquired_at=harness.now,
        license_restriction=None,
        tlp=TLP.GREEN,
        do_not_submit=False,
        external_llm_allowed=True,
        state=SampleState.QUARANTINED,
        expected_hash=_DEFAULT_SEED,
    )
    harness.db.samples[sample.id] = sample
    association = SampleAssociation(
        investigation_id=harness.investigation.id,
        sample_sha256=_DEFAULT_SEED,
        origin=SampleOrigin.VT_PIVOT,
        qualification=SampleQualification.CANDIDATE,
    )
    harness.db.associations[association.id] = association
    harness.db.association_sample_ids[association.id] = sample.id

    inventory = await harness.bootstrap.inventory(harness.investigation.id)
    ioc = next(item for item in inventory.seeds if item.kind is BootstrapIocKind.SAMPLE)
    assert ioc.associations and ioc.associations[0].association_id == association.id
    assert ioc.quarantined and ioc.quarantined[0].sample_id == sample.id
    assert ioc.quarantined[0].association_id == association.id
    assert ioc.quarantined[0].state is SampleState.QUARANTINED
    assert ioc.sources[0].claim_ids == ("L1.1",)
    assert harness.vt.calls["file_report"] == 0


@pytest.mark.asyncio
async def test_b04_g2_absent_plans_awaiting_g2_without_provider_call() -> None:
    harness = _harness(with_envelope=False)
    plan = await harness.bootstrap.plan(harness.investigation.id)
    assert plan.state is BootstrapPlanState.AWAITING_G2
    assert plan.operations
    assert all(
        operation.status is ToolInvocationStatus.PENDING
        and operation.reason is BootstrapReason.AWAITING_G2
        for operation in plan.operations
    )
    snapshot = await harness.run(executor=_NoopExecutor())
    assert snapshot.plan_state is BootstrapPlanState.AWAITING_G2
    assert snapshot.limits.stopped_reason is BootstrapReason.AWAITING_G2
    assert sum(harness.vt.calls.values()) == 0
    assert harness.vt.download_calls == []


@pytest.mark.asyncio
async def test_b05_approved_envelope_plans_and_runs_without_per_hash_approval() -> None:
    harness = _harness()
    harness.vt.relationship_pages = [
        _relationship_page(item_id=f"rel-{index}") for index in range(4)
    ]
    plan = await harness.bootstrap.plan(harness.investigation.id)
    assert plan.state is BootstrapPlanState.READY
    assert plan.envelope_version == harness.envelope.version  # type: ignore[union-attr]
    tools = {operation.tool_name for operation in plan.operations}
    assert {"vt.file_report", "vt.relationships", "vt.behaviour", "vt.download_sample"} <= tools
    assert all(operation.status is ToolInvocationStatus.PENDING for operation in plan.operations)
    snapshot = await harness.run()
    assert snapshot.plan_state is BootstrapPlanState.READY
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.download_calls == [_DEFAULT_SEED]
    operations = _snapshot_operations(snapshot)
    assert operations["vt.file_report"].status is ToolInvocationStatus.SUCCEEDED
    assert operations["vt.download_sample"].status is ToolInvocationStatus.SUCCEEDED
    assert operations["vt.download_sample"].reason is BootstrapReason.DOWNLOADED
    assert snapshot.iocs[0].download is not None
    assert snapshot.iocs[0].download.hash_verified_quarantine is True


@pytest.mark.asyncio
async def test_b06_file_report_archived_normalized_once() -> None:
    harness = _harness()
    snapshot = await harness.run()
    report = _snapshot_operations(snapshot)["vt.file_report"]
    assert report.status is ToolInvocationStatus.SUCCEEDED
    assert report.pages == 1
    assert report.provider_routes == ("proxy:v3",)
    observations = await harness.db.evidence.list_for_investigation(harness.investigation.id)
    file_report_observations = [
        item for item in observations if item.kind is EvidenceKind.VT_FILE_REPORT
    ]
    # Raw archived page plus bounded normalized summary, exactly once.
    assert len(file_report_observations) == 2
    assert len({item.id for item in file_report_observations}) == 2
    assert all(item.object_id == _DEFAULT_SEED for item in file_report_observations)
    replay = await harness.run()
    assert replay.snapshot_id == snapshot.snapshot_id
    assert harness.vt.calls["file_report"] == 1
    after = await harness.db.evidence.list_for_investigation(harness.investigation.id)
    assert len([item for item in after if item.kind is EvidenceKind.VT_FILE_REPORT]) == 2


@pytest.mark.asyncio
async def test_b06_unavailable_download_recorded_without_blocking_or_inventing_sample() -> None:
    harness = _harness(
        specs=(
            _IocSpec("sha256", _DEFAULT_SEED, context="row 1"),
            _IocSpec("ipv4", _IP, context="row 2"),
        )
    )
    harness.vt.errors["file_download"] = VirusTotalHttpError(
        "download route not found", status_code=404
    )
    snapshot = await harness.run()
    operations = _snapshot_operations(snapshot)
    download_op = next(
        item
        for ioc in snapshot.iocs
        for item in ioc.operations
        if item.tool_name == "vt.download_sample"
    )
    assert download_op.status in {
        ToolInvocationStatus.FAILED,
        ToolInvocationStatus.NEEDS_REVIEW,
        ToolInvocationStatus.UNAVAILABLE,
    }
    sample_ioc = next(ioc for ioc in snapshot.iocs if ioc.kind is BootstrapIocKind.SAMPLE)
    assert sample_ioc.download is not None
    assert sample_ioc.download.status == "failed"
    # The plan is not blocked: the following IOC is still processed.  A
    # candidate association may be journaled before the attempt (governance
    # contract), but no Sample bytes are invented or linked on failure.
    assert operations["vt.object_report"].status is ToolInvocationStatus.SUCCEEDED
    assert harness.db.samples == {}
    assert harness.db.association_sample_ids == {}


@pytest.mark.asyncio
async def test_b07_behaviour_report_records_route_limits() -> None:
    harness = _harness()
    harness.vt.behaviour_pages = [_behaviour_page()]
    snapshot = await harness.run()
    behaviour = _snapshot_operations(snapshot)["vt.behaviour"]
    assert behaviour.status is ToolInvocationStatus.SUCCEEDED
    assert behaviour.pages == 1
    assert behaviour.observed_count == 1
    assert behaviour.next_cursor is None
    assert behaviour.provider_routes == ("proxy:v3",)
    assert behaviour.reason is BootstrapReason.AVAILABLE
    coverage = {entry.target: entry for entry in snapshot.coverage}
    assert coverage["vt.behaviour_pcap"].kind in {
        BootstrapCoverageKind.CAPABILITY_ABSENT,
        BootstrapCoverageKind.DEFERRED,
        BootstrapCoverageKind.ROUTE_UNAVAILABLE,
    }
    assert harness.vt.calls["behaviour_pcap"] == 0


@pytest.mark.asyncio
async def test_b08_ip_domain_reports_keep_dates_relations_cursors() -> None:
    harness = _harness(
        specs=(
            _IocSpec("ipv4", _IP, context="row 1"),
            _IocSpec("domain", _DOMAIN, context="row 2"),
            _IocSpec("url", _URL, context="row 3"),
        )
    )
    harness.vt.relationship_pages = [
        _relationship_page(next_cursor="cursor-next", exhaustive=False) for _ in range(40)
    ]
    snapshot = await harness.run()
    operations = [operation for ioc in snapshot.iocs for operation in ioc.operations]
    object_reports = [item for item in operations if item.tool_name == "vt.object_report"]
    assert object_reports and all(
        item.status is ToolInvocationStatus.SUCCEEDED for item in object_reports
    )
    relationships = [item for item in operations if item.tool_name == "vt.relationships"]
    assert relationships
    assert all(item.next_cursor == "cursor-next" for item in relationships)
    assert all(item.pagination_complete is False for item in relationships)
    assert all(item.reason is BootstrapReason.PARTIAL for item in relationships)
    assert all(item.provider_routes == ("proxy:v3",) for item in relationships)


@pytest.mark.asyncio
async def test_b09_download_is_bounded_hash_verified_and_quarantined() -> None:
    harness = _harness()
    snapshot = await harness.run()
    expected = _DEFAULT_SEED
    assert harness.vt.download_calls == [expected]
    stored = [
        blob
        for blob in harness.db.blobs.values()
        if blob.descriptor.logical_bucket == _QUARANTINE_BUCKET
        and blob.descriptor.sha256 == expected
    ]
    assert len(stored) == 1
    samples = [sample for sample in harness.db.samples.values() if sample.expected_hash == expected]
    assert len(samples) == 1
    assert samples[0].subject_id == harness.investigation.subject_id
    assert samples[0].state in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
    association = next(
        item
        for item in harness.db.associations.values()
        if item.investigation_id == harness.investigation.id and item.sample_sha256 == expected
    )
    download = _snapshot_operations(snapshot)["vt.download_sample"]
    assert download.status is ToolInvocationStatus.SUCCEEDED
    assert download.reason is BootstrapReason.DOWNLOADED
    outcome = snapshot.iocs[0].download
    assert outcome is not None
    assert outcome.status == "downloaded"
    assert outcome.hash_verified_quarantine is True
    assert outcome.sample_sha256 == expected
    assert outcome.association_id == association.id


@pytest.mark.asyncio
async def test_b09_batch_stops_on_quota_and_revocation_without_retry() -> None:
    harness = _harness()
    harness.vt.errors["file_report"] = VirusTotalQuotaExceededError("quota reached")
    snapshot = await harness.run(idempotency_key="quota-run")
    report = _snapshot_operations(snapshot)["vt.file_report"]
    assert report.status in {
        ToolInvocationStatus.UNAVAILABLE,
        ToolInvocationStatus.NEEDS_REVIEW,
    }
    assert report.reason is BootstrapReason.QUOTA
    assert snapshot.limits.stopped_reason is BootstrapReason.QUOTA
    assert harness.vt.calls["file_report"] == 1
    assert harness.vt.download_calls == []
    assert any(
        operation.reason is BootstrapReason.STOPPED
        for ioc in snapshot.iocs
        for operation in ioc.operations
    )

    harness2 = _harness()
    real = harness2.executor

    class _RevokingExecutor:
        def __init__(self) -> None:
            self.calls = 0

        async def execute_tool_call(self, **kwargs: Any) -> Any:
            self.calls += 1
            result = await real.execute_tool_call(**kwargs)
            harness2.set_envelope(None)
            return result

    revoking = _RevokingExecutor()
    snapshot2 = await harness2.run(idempotency_key="revocation-run", executor=revoking)
    assert revoking.calls == 1
    assert snapshot2.limits.stopped_reason is BootstrapReason.POLICY_DENIED
    denied = [
        operation
        for ioc in snapshot2.iocs
        for operation in ioc.operations
        if operation.status is ToolInvocationStatus.POLICY_DENIED
    ]
    assert denied and all(item.error_code == "envelope_revoked" for item in denied)
    assert harness2.vt.calls["file_report"] == 1


@pytest.mark.asyncio
async def test_b10_replay_does_not_download_twice_or_consume_budget_again() -> None:
    harness = _harness()
    first = await harness.run(idempotency_key="replay-run")
    assert harness.vt.download_calls == [_DEFAULT_SEED]
    first_envelope = harness.db.envelopes[harness.investigation.id]
    assert first_envelope.budget.consumed[BudgetDimension.DOWNLOADS] == 1

    second = await harness.run(idempotency_key="replay-run")
    assert second.snapshot_id == first.snapshot_id
    assert harness.vt.download_calls == [_DEFAULT_SEED]

    # A new snapshot key still reuses the durable invocations: identical
    # operation keys, no second download and no second budget consumption.
    third = await harness.run(idempotency_key="replay-run-2")
    assert third.snapshot_id != first.snapshot_id
    assert harness.vt.download_calls == [_DEFAULT_SEED]
    assert harness.vt.calls["file_report"] == 1
    current = harness.db.envelopes[harness.investigation.id]
    assert current.budget.consumed[BudgetDimension.DOWNLOADS] == 1
    download = _snapshot_operations(third)["vt.download_sample"]
    assert download.status is ToolInvocationStatus.SUCCEEDED
    assert download.idempotent_replay is True
    assert third.iocs[0].download is not None
    assert third.iocs[0].download.status == "reused"


@pytest.mark.asyncio
async def test_b10_no_vt_never_blocks_workspace_or_snapshot() -> None:
    harness = _harness(capabilities=BootstrapVtCapabilities(), with_envelope=False)
    snapshot = await harness.run(executor=_NoopExecutor())
    assert snapshot.iocs and snapshot.iocs[0].kind is BootstrapIocKind.SAMPLE
    assert snapshot.iocs[0].status is ToolInvocationStatus.PENDING
    assert snapshot.iocs[0].reason is BootstrapReason.AWAITING_G2
    assert sum(harness.vt.calls.values()) == 0
    stored = await harness.bootstrap.latest_snapshot(harness.investigation.id)
    assert stored is not None and stored.snapshot_id == snapshot.snapshot_id
    exported = harness.bootstrap.snapshot_export(stored)
    paths = {item.path for item in exported.files}
    assert "vt/manifest.json" in paths
    assert any(path.startswith("vt/files/") for path in paths)
    assert harness.db.bindings


@pytest.mark.asyncio
async def test_inventory_snapshot_is_dated_and_bounded() -> None:
    specs = tuple(
        _IocSpec("sha256", hashlib.sha256(f"seed-{index}".encode()).hexdigest())
        for index in range(MAX_INVENTORY_IOCS + 12)
    )
    harness = _harness(specs=specs, with_envelope=False)
    inventory = await harness.bootstrap.inventory(harness.investigation.id)
    assert inventory.generated_at == _FIXED_NOW
    assert len(inventory.seeds) == MAX_INVENTORY_IOCS
    assert inventory.limits.truncated is True
    assert inventory.limits.iocs_considered == MAX_INVENTORY_IOCS + 12
    assert inventory.limits.max_iocs == MAX_INVENTORY_IOCS
    assert inventory.limits.max_sources_per_ioc > 0
    assert inventory.limits.max_claims_per_ioc > 0

    snapshot = await harness.run(executor=_NoopExecutor())
    assert snapshot.generated_at == _FIXED_NOW
    assert len(snapshot.iocs) == MAX_INVENTORY_IOCS
    assert snapshot.limits.inventory_truncated is True
    assert snapshot.limits.iocs_included == MAX_INVENTORY_IOCS
    assert snapshot.limits.max_operations > 0
    assert snapshot.limits.operations_recorded <= snapshot.limits.max_operations


@pytest.mark.asyncio
async def test_inventory_hash_aliases_group_same_sample_different_hash_types() -> None:
    extraction_id = uuid4()
    context = "| MD5 | SHA-256 |\n| bbbb | aaaa |"
    quote = "MD5 bbbb / SHA-256 aaaa"
    specs = (
        _IocSpec(
            "md5",
            _SEED_MD5,
            context=context,
            evidence_quote=quote,
            extraction_artifact_id=extraction_id,
        ),
        _IocSpec(
            "sha256",
            _DEFAULT_SEED,
            context=context,
            evidence_quote=quote,
            extraction_artifact_id=extraction_id,
        ),
    )
    harness = _harness(specs=specs, with_envelope=False)
    inventory = await harness.bootstrap.inventory(harness.investigation.id)
    samples = [ioc for ioc in inventory.seeds if ioc.kind is BootstrapIocKind.SAMPLE]
    assert len(samples) == 1
    ioc = samples[0]
    assert ioc.value == _DEFAULT_SEED and ioc.hash_type == "sha256"
    assert {alias.hash_type for alias in ioc.hashes} == {"md5", "sha256"}
    assert BootstrapHashAliasV1(hash_type="md5", value=_SEED_MD5) in ioc.hashes
    assert ioc.claims == ("L1.1",)


@pytest.mark.asyncio
async def test_plan_records_object_report_capability_shared_and_comments_absent() -> None:
    harness = _harness(specs=(_IocSpec("ipv4", _IP), _IocSpec("domain", _DOMAIN)))
    plan = await harness.bootstrap.plan(harness.investigation.id)
    coverage = {(entry.kind, entry.target) for entry in plan.coverage}
    assert (BootstrapCoverageKind.CAPABILITY_SHARED, "vt.object_report") in coverage
    assert (BootstrapCoverageKind.CAPABILITY_ABSENT, "vt.comments") in coverage
    assert (BootstrapCoverageKind.ROUTE_UNVERIFIED, "vt.file_download") in coverage
    object_report = next(
        operation for operation in plan.operations if operation.tool_name == "vt.object_report"
    )
    assert object_report.arguments["object_type"] in {"ip_addresses", "domains"}
    assert object_report.operation_key == tool_operation_key(
        investigation_id=harness.investigation.id,
        tool_name="vt.object_report",
        schema_version="1",
        normalized_parameters=object_report.arguments,
        purpose=f"Bootstrap initial VT: vt.object_report pour {object_report.ioc_id}.",
    )


@pytest.mark.asyncio
async def test_bootstrap_requires_article_publication() -> None:
    harness = _harness(publication_kind=PublicationKind.BRIEF)
    with pytest.raises(Exception) as refused:
        await harness.bootstrap.inventory(harness.investigation.id)
    assert getattr(refused.value, "code", None) == "publication_kind_not_article"


@pytest.mark.asyncio
async def test_snapshot_export_builds_vt_layout_without_credentials() -> None:
    harness = _harness(
        specs=(
            _IocSpec("sha256", _DEFAULT_SEED, context="row 1"),
            _IocSpec("ipv4", _IP, context="row 2"),
            _IocSpec("domain", _DOMAIN, context="row 3"),
            _IocSpec("url", _URL, context="row 4"),
        )
    )
    harness.vt.relationship_pages = [_relationship_page() for _ in range(12)]
    harness.vt.behaviour_pages = [_behaviour_page()]
    snapshot = await harness.run()
    exported = harness.bootstrap.snapshot_export(snapshot)
    paths = {item.path for item in exported.files}
    assert "vt/bootstrap-snapshot.json" in paths
    assert "vt/manifest.json" in paths
    assert any(path.startswith("vt/files/") for path in paths)
    assert any(path.startswith("vt/ips/") for path in paths)
    assert any(path.startswith("vt/domains/") for path in paths)
    assert any(path.startswith("vt/urls/") for path in paths)
    assert any(path.startswith("vt/behaviours/") for path in paths)
    assert any(path.startswith("vt/relationships/") for path in paths)
    assert all(item.path.endswith(".json") for item in exported.files)
    for item in exported.files:
        assert hashlib.sha256(item.content.encode()).hexdigest() == item.sha256
        assert len(item.content.encode()) == item.size_bytes
        lowered = item.content.casefold()
        assert "x-amz-signature" not in lowered
        assert "api_key=" not in lowered
        assert "apikey=" not in lowered
        assert "bearer " not in lowered
    manifest = json.loads(
        next(item.content for item in exported.files if item.path == "vt/manifest.json")
    )
    assert manifest["snapshot_id"] == str(snapshot.snapshot_id)
    assert len(manifest["files"]) == len(exported.files) - 1


@pytest.mark.asyncio
async def test_export_refuses_credential_like_material() -> None:
    harness = _harness(with_envelope=False)
    snapshot = await harness.run(executor=_NoopExecutor())
    tampered = snapshot.model_copy(update={"limitations": ("api_key=very-secret-value",)})
    with pytest.raises(BootstrapExportRefusedError):
        harness.bootstrap.snapshot_export(tampered)


@pytest.mark.asyncio
async def test_bootstrap_routes_refuse_operator_client_and_serve_human() -> None:
    harness = _harness(with_envelope=False, capabilities=BootstrapVtCapabilities())
    app = FastAPI()
    app.include_router(bootstrap_router)
    app.state.uow_factory = harness.uow
    app.state.settings = Settings()
    app.state.investigation_composition_factory = SimpleNamespace(
        article_snapshots=harness.article_port,
        artifact_store=harness.artifact_store,
        build=lambda _uow, _investigation_id, operator_mode=False: SimpleNamespace(
            service=harness.executor
        ),
    )

    class _OperatorClient:
        async def current(self) -> Identity:
            return Identity(
                actor_id="operator:operator-client",
                scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
            )

    app.state.identity_provider = _OperatorClient()
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        refused_inventory = await client.get(
            f"/api/investigations/{harness.investigation.id}/bootstrap/inventory"
        )
        assert refused_inventory.status_code == 403
        refused_run = await client.post(
            f"/api/investigations/{harness.investigation.id}/bootstrap/run",
            headers={"Idempotency-Key": "api-run"},
        )
        assert refused_run.status_code == 403

    app.state.identity_provider = LocalIdentityProvider("dev-analyst")
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        inventory = await client.get(
            f"/api/investigations/{harness.investigation.id}/bootstrap/inventory"
        )
        assert inventory.status_code == 200
        assert inventory.json()["pinned_synthesis_sha256"] == harness.snapshot.synthesis_sha256
        plan = await client.get(f"/api/investigations/{harness.investigation.id}/bootstrap/plan")
        assert plan.status_code == 200
        assert plan.json()["state"] == BootstrapPlanState.AWAITING_G2.value
        run = await client.post(
            f"/api/investigations/{harness.investigation.id}/bootstrap/run",
            headers={"Idempotency-Key": "api-run"},
        )
        assert run.status_code == 200
        assert run.json()["plan_state"] == BootstrapPlanState.AWAITING_G2.value
        stored = await client.get(
            f"/api/investigations/{harness.investigation.id}/bootstrap/snapshot"
        )
        assert stored.status_code == 200
        exported = await client.get(
            f"/api/investigations/{harness.investigation.id}/bootstrap/snapshot/export"
        )
        assert exported.status_code == 200
        assert exported.json()["files"]
        unknown = await client.get(f"/api/investigations/{uuid4()}/bootstrap/inventory")
        assert unknown.status_code == 404


# ---------------------------------------------------------------------------
# M24.1 VT-FAKE: capability matrix with a fake provider only
# ---------------------------------------------------------------------------


async def _mixed_batch_harness() -> tuple[
    _Harness, dict[str, bytes], tuple[str, ...], tuple[str, ...], tuple[str, ...]
]:
    """Five hashes (3 downloadable, 2 absent downloads), two IPs, two domains.

    The fake port answers by value: exactly the acceptance-scenario shape
    (``docs/m24/acceptance-scenario.md`` steps 03/05), no network.
    """
    available = {
        hashlib.sha256(f"vt-fake-available-{index}".encode()).hexdigest(): (
            f"vt-fake-available-{index}".encode()
        )
        for index in range(3)
    }
    unavailable = tuple(
        hashlib.sha256(f"vt-fake-unavailable-{index}".encode()).hexdigest() for index in range(2)
    )
    ips = ("203.0.113.21", "198.51.100.22")
    domains = ("mixed-one.invalid", "mixed-two.invalid")
    specs = (
        tuple(
            _IocSpec("sha256", value, context=f"hash row {index}")
            for index, value in enumerate((*available, *unavailable))
        )
        + tuple(
            _IocSpec("ipv4", value, context=f"ip row {index}") for index, value in enumerate(ips)
        )
        + tuple(
            _IocSpec("domain", value, context=f"domain row {index}")
            for index, value in enumerate(domains)
        )
    )
    harness = _harness(specs=specs)

    async def file_report(file_hash: str) -> Any:
        harness.vt.calls["file_report"] += 1
        return _file_report(file_hash)

    async def file_download(file_hash: str, *, sink: Any) -> None:
        harness.vt.download_calls.append(file_hash)
        payload = available.get(file_hash)
        if payload is None:
            # The two unavailable samples: the proxy download route answers 404.
            raise VirusTotalHttpError("download route not found", status_code=404)
        sink.write(payload)

    harness.vt.file_report = file_report  # type: ignore[method-assign]
    harness.vt.file_download = file_download  # type: ignore[method-assign]
    # Every relationship page is non-exhaustive: the snapshot must expose the
    # partial report (cursor + pagination_complete=false), not claim completion.
    harness.vt.relationship_pages = [
        _relationship_page(next_cursor="cursor-next", exhaustive=False) for _ in range(400)
    ]
    return harness, available, unavailable, ips, domains


@pytest.mark.asyncio
async def test_b11_five_hashes_three_available_two_unavailable_with_domains_and_ips() -> None:
    harness, available, unavailable, ips, domains = await _mixed_batch_harness()
    snapshot = await harness.run(idempotency_key="vt-fake-mixed-batch")

    assert snapshot.plan_state is BootstrapPlanState.READY
    assert snapshot.limits.stopped_reason is None
    assert len(snapshot.iocs) == 5 + 2 + 2
    by_value = {ioc.value: ioc for ioc in snapshot.iocs}

    assert harness.vt.calls["file_report"] == 5
    assert harness.vt.calls["object_report"] == 4
    assert sorted(harness.vt.download_calls) == sorted((*available, *unavailable))

    for value in available:
        ioc = by_value[value]
        report = next(item for item in ioc.operations if item.tool_name == "vt.file_report")
        assert report.status is ToolInvocationStatus.SUCCEEDED
        assert report.reason is BootstrapReason.AVAILABLE
        download = next(item for item in ioc.operations if item.tool_name == "vt.download_sample")
        assert download.status is ToolInvocationStatus.SUCCEEDED
        assert download.reason is BootstrapReason.DOWNLOADED
        assert ioc.download is not None
        assert ioc.download.status == "downloaded"
        assert ioc.download.sample_sha256 == value
        assert ioc.download.hash_verified_quarantine is True
        assert ioc.download.sample_id is not None
        assert ioc.download.association_id is not None
        assert (
            harness.db.association_sample_ids.get(ioc.download.association_id)
            == ioc.download.sample_id
        )

    for value in unavailable:
        ioc = by_value[value]
        report = next(item for item in ioc.operations if item.tool_name == "vt.file_report")
        assert report.status is ToolInvocationStatus.SUCCEEDED
        download = next(item for item in ioc.operations if item.tool_name == "vt.download_sample")
        # The download route answers 404: the operation records a route failure
        # (never a "file not found" claim) while the other seeds continue.
        assert download.status is ToolInvocationStatus.FAILED
        assert download.error_code == "virustotal_download_route_not_found"
        assert download.reason is BootstrapReason.ROUTE_UNAVAILABLE
        assert ioc.download is not None
        assert ioc.download.status == "failed"
        assert ioc.download.reason == "route_unavailable"
        assert ioc.download.sample_id is None
        assert all(sample.expected_hash != value for sample in harness.db.samples.values())
        linked_hashes = {
            association.sample_sha256
            for association_id in harness.db.association_sample_ids
            if (association := harness.db.associations.get(association_id)) is not None
        }
        assert value not in linked_hashes

    for value in (*ips, *domains):
        ioc = by_value[value]
        report = next(item for item in ioc.operations if item.tool_name == "vt.object_report")
        assert report.status is ToolInvocationStatus.SUCCEEDED
        assert report.reason is BootstrapReason.AVAILABLE

    relationships = [
        item
        for ioc in snapshot.iocs
        for item in ioc.operations
        if item.tool_name == "vt.relationships"
    ]
    assert len(relationships) == 5 * 4 + 2 * 4 + 2 * 5
    assert all(item.status is ToolInvocationStatus.PARTIAL for item in relationships)
    assert all(item.reason is BootstrapReason.PARTIAL for item in relationships)
    assert all(item.next_cursor == "cursor-next" for item in relationships)
    assert all(item.pagination_complete is False for item in relationships)

    stopped = sum(
        1
        for ioc in snapshot.iocs
        for item in ioc.operations
        if item.reason is BootstrapReason.STOPPED
    )
    assert stopped == 0

    exported = harness.bootstrap.snapshot_export(snapshot)
    paths = {item.path for item in exported.files}
    assert "vt/bootstrap-snapshot.json" in paths
    assert "vt/manifest.json" in paths
    for value in (*available, *unavailable):
        assert f"vt/files/{value}.json" in paths
    assert len([path for path in paths if path.startswith("vt/behaviours/")]) == 5
    assert len([path for path in paths if path.startswith("vt/relationships/")]) == 9
    assert len([path for path in paths if path.startswith("vt/ips/")]) == 2
    assert len([path for path in paths if path.startswith("vt/domains/")]) == 2


@pytest.mark.asyncio
async def test_b12_download_route_404_is_route_unavailable_not_a_missing_file() -> None:
    harness = _harness(
        specs=(
            _IocSpec("sha256", _DEFAULT_SEED, context="row 1"),
            _IocSpec("ipv4", _IP, context="row 2"),
        )
    )
    harness.vt.errors["file_download"] = VirusTotalHttpError(
        "download route not found", status_code=404
    )

    snapshot = await harness.run(idempotency_key="download-route-404")

    download = next(
        item
        for ioc in snapshot.iocs
        for item in ioc.operations
        if item.tool_name == "vt.download_sample"
    )
    assert download.status is ToolInvocationStatus.FAILED
    assert download.error_code == "virustotal_download_route_not_found"
    # A proxy route 404 is a route failure, never proof that the file is absent
    # (the planned operation documents ``route_unavailable`` for this case).
    assert download.reason is BootstrapReason.ROUTE_UNAVAILABLE
    sample_ioc = next(ioc for ioc in snapshot.iocs if ioc.value == _DEFAULT_SEED)
    assert sample_ioc.download is not None
    assert sample_ioc.download.status == "failed"
    assert sample_ioc.download.reason == "route_unavailable"
    assert harness.db.samples == {}
    assert harness.db.association_sample_ids == {}
    # The batch continues: the next IOC is still collected.
    assert harness.vt.calls["object_report"] == 1


@pytest.mark.asyncio
async def test_b12_download_provider_404_stays_not_found_and_is_distinguished() -> None:
    harness = _harness()
    provider_error = VirusTotalHttpError("file not found", status_code=404)
    provider_error.response_origin = "provider"
    harness.vt.errors["file_download"] = provider_error

    snapshot = await harness.run(idempotency_key="download-provider-404")

    download = next(
        item
        for ioc in snapshot.iocs
        for item in ioc.operations
        if item.tool_name == "vt.download_sample"
    )
    assert download.status is ToolInvocationStatus.FAILED
    assert download.error_code == "virustotal_download_file_not_found"
    assert download.reason is BootstrapReason.NOT_FOUND
    assert snapshot.iocs[0].download is not None
    assert snapshot.iocs[0].download.reason == "not_found"
    assert harness.db.samples == {}


@pytest.mark.asyncio
async def test_b13_local_route_failure_on_a_read_is_unavailable_never_success() -> None:
    # The route is refused before any provider request (the dispatcher effect
    # counter is zero), so the invocation records an exact pre-send refusal:
    # unavailable, never a success and never an ambiguous ``needs_review``.
    harness = _harness()
    harness.vt.errors["file_report"] = VirusTotalRouteUnavailableError("no route configured")

    snapshot = await harness.run(idempotency_key="read-route-unavailable")

    report = _snapshot_operations(snapshot)["vt.file_report"]
    assert report.status is ToolInvocationStatus.UNAVAILABLE
    assert report.reason is BootstrapReason.CAPABILITY_ABSENT
    # A failed read never becomes evidence: the download stays pending.
    download = _snapshot_operations(snapshot)["vt.download_sample"]
    assert download.status is ToolInvocationStatus.PENDING
    assert download.reason is BootstrapReason.EVIDENCE_UNAVAILABLE
    assert harness.vt.download_calls == []


def test_b14_bootstrap_capabilities_from_settings_are_route_scoped() -> None:
    from cti_app.workers.tasks import virustotal_capabilities_from_settings

    defaults = Settings(_env_file=None)
    assert BootstrapVtCapabilities.from_settings(defaults) == BootstrapVtCapabilities()
    assert virustotal_capabilities_from_settings(defaults) == VirusTotalCapabilities()

    # Flags without a configured proxy route are never capabilities.
    flags_only = Settings(
        _env_file=None,
        virustotal_file_report_enabled=True,
        virustotal_object_relationships_enabled=True,
        virustotal_behaviour_reports_enabled=True,
        virustotal_file_download_enabled=True,
        virustotal_behaviour_pcap_enabled=True,
    )
    assert BootstrapVtCapabilities.from_settings(flags_only) == BootstrapVtCapabilities()
    assert virustotal_capabilities_from_settings(flags_only) == VirusTotalCapabilities()

    configured = Settings(
        _env_file=None,
        virustotal_proxy_url="http://vt-proxy.test:8080",
        virustotal_file_report_enabled=True,
        virustotal_object_relationships_enabled=True,
        virustotal_behaviour_reports_enabled=True,
        virustotal_file_download_enabled=True,
        virustotal_behaviour_pcap_enabled=True,
    )
    bootstrap_caps = BootstrapVtCapabilities.from_settings(configured)
    worker_caps = virustotal_capabilities_from_settings(configured)
    assert bootstrap_caps.file_report is worker_caps.file_report is True
    assert bootstrap_caps.relationships is worker_caps.file_relationships is True
    assert bootstrap_caps.behaviour is worker_caps.behaviour_reports is True
    assert bootstrap_caps.download is worker_caps.file_download is True
    assert bootstrap_caps.pcap is worker_caps.behaviour_pcap is True
    assert bootstrap_caps.pcap_route_configured is True

    # Download requires the signed-CDN host allowlist in addition to the flag.
    wrong_cdn = Settings(
        _env_file=None,
        virustotal_proxy_url="http://vt-proxy.test:8080",
        virustotal_file_download_enabled=True,
        virustotal_download_allowed_hosts="samples.example",
    )
    assert BootstrapVtCapabilities.from_settings(wrong_cdn).download is False
    assert virustotal_capabilities_from_settings(wrong_cdn).file_download is False


@pytest.mark.asyncio
async def test_b14_planned_coverage_exposes_capability_and_route_limits() -> None:
    harness = _harness(
        capabilities=BootstrapVtCapabilities(
            file_report=True, relationships=True, behaviour=True, download=True
        )
    )
    plan = await harness.bootstrap.plan(harness.investigation.id)
    download_operations = [
        item for item in plan.operations if item.tool_name == "vt.download_sample"
    ]
    assert download_operations
    assert all(item.route_status is BootstrapRouteStatus.UNVERIFIED for item in download_operations)
    coverage = {(entry.kind, entry.target) for entry in plan.coverage}
    assert (BootstrapCoverageKind.ROUTE_UNVERIFIED, "vt.file_download") in coverage
    assert (BootstrapCoverageKind.CAPABILITY_ABSENT, "vt.behaviour_pcap") in coverage
    assert plan.limits.operations_planned == len(plan.operations)

    pcap_without_route = _harness(
        capabilities=BootstrapVtCapabilities(
            file_report=True, pcap=True, pcap_route_configured=False
        ),
        with_envelope=False,
    )
    pcap_plan = await pcap_without_route.bootstrap.plan(pcap_without_route.investigation.id)
    assert (
        BootstrapCoverageKind.ROUTE_UNAVAILABLE,
        "vt.behaviour_pcap",
    ) in {(entry.kind, entry.target) for entry in pcap_plan.coverage}

    pcap_ready = _harness(
        capabilities=BootstrapVtCapabilities(
            file_report=True, pcap=True, pcap_route_configured=True
        ),
        with_envelope=False,
    )
    ready_plan = await pcap_ready.bootstrap.plan(pcap_ready.investigation.id)
    assert (BootstrapCoverageKind.DEFERRED, "vt.behaviour_pcap") in {
        (entry.kind, entry.target) for entry in ready_plan.coverage
    }

    no_capabilities = _harness(capabilities=BootstrapVtCapabilities(), with_envelope=False)
    empty_plan = await no_capabilities.bootstrap.plan(no_capabilities.investigation.id)
    assert (BootstrapCoverageKind.CAPABILITY_ABSENT, "vt") in {
        (entry.kind, entry.target) for entry in empty_plan.coverage
    }
    assert empty_plan.operations == ()
