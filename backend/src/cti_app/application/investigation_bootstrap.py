"""Deterministic VirusTotal bootstrap for an ARTICLE investigation (M24-03A).

The module has four responsibilities. None of them contacts a provider until an
approved G2 envelope exists:

* build a deduplicated IOC inventory from the pinned, verified synthesis
  (:class:`IOCInventoryV1`); every entry is a *seed* cited by the article, and
  ``discovered`` is always empty at bootstrap;
* project the operations that the *current* envelope, capabilities and budget
  would authorise (:class:`BootstrapPlanV1`); a missing envelope yields
  ``awaiting_g2`` and zero provider calls;
* execute an authorised batch through the real
  ``InvestigationService.execute_tool_call`` path with deterministic
  ``tool_operation_key`` identities, then persist a dated, bounded snapshot
  (:class:`BootstrapSnapshotV1`) as an immutable canonical evidence
  observation;
* render the snapshot as ``vt/`` manifest files for the host workspace agent
  (:class:`BootstrapSnapshotExportV1`); the actual host write belongs to the
  workspace lot, never to this module.

Statuses reuse the canonical ``ToolInvocationStatus`` enum.  The mandate's
``available/downloaded/reused/not_found/provider_denied/route_unavailable/
quota/failed/pending`` vocabulary is carried as a typed ``BootstrapReason``
detail next to the canonical status, never as a parallel state machine.

Code facts recorded as typed coverage instead of fake success: ``vt.object_report``
is gated by the ``file_report`` capability (no dedicated flag), no standalone
comments operation exists, the download proxy route is HOST-UNKNOWN after the
old 404 audit, and ``vt.behaviour_pcap`` needs a sandbox id from a persisted
behaviour report so it is deferred rather than planned as a concrete call.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Any, Literal, Protocol, Self, cast
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.evidence_recorder import ProductionEvidenceRecorder
from cti_app.application.external_effects import AdvisorDisclosurePolicy, advisor_disclosure_policy
from cti_app.application.investigation_contracts import ConfirmedIOCContextV1
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    InvestigationEnvelopeRepository,
    SampleAssociationRepository,
    WorkspaceBindingRepository,
)
from cti_app.application.investigation_state import InvestigationStateRepository
from cti_app.application.investigation_tools import ToolResult
from cti_app.application.persistence import (
    AnalystInvestigationRepository,
    ProductionRoutingDecisionRepository,
    ProductionRunRepository,
    ProductionUnitOfWork,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_routing import get_adopted_production_routing_decision
from cti_app.application.research_context_builder import ArticleSnapshot, ArticleSnapshotPort
from cti_app.application.workspace_agent import WorkspaceBindingV1
from cti_app.config import VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST, Settings
from cti_app.domain.entities import Sample, SampleState
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetExceeded,
    EvidenceKind,
    InvestigationBudget,
    InvestigationEnvelope,
    SampleAssociation,
    SampleOrigin,
    SampleQualification,
    TLPLevel,
    ToolInvocation,
    ToolInvocationStatus,
    tool_operation_key,
)
from cti_app.domain.investigation_state import InvestigationWorkingState
from cti_app.domain.production import AnalystInvestigation, ProductionRun
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.virustotal import (
    VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE,
    VirusTotalObjectType,
)

INVENTORY_SCHEMA_VERSION: Literal["vt-bootstrap-inventory.v1"] = "vt-bootstrap-inventory.v1"
PLAN_SCHEMA_VERSION: Literal["vt-bootstrap-plan.v1"] = "vt-bootstrap-plan.v1"
SNAPSHOT_SCHEMA_VERSION: Literal["vt-bootstrap-snapshot.v1"] = "vt-bootstrap-snapshot.v1"
EXPORT_SCHEMA_VERSION: Literal["vt-bootstrap-export.v1"] = "vt-bootstrap-export.v1"

BOOTSTRAP_SOURCE = "investigation.bootstrap"
BOOTSTRAP_SOURCE_VERSION = SNAPSHOT_SCHEMA_VERSION
# Closed object_type for bootstrap snapshots in the canonical evidence store.
BOOTSTRAP_SNAPSHOT_OBJECT_TYPE = "investigation_bootstrap_snapshot"

MAX_INVENTORY_IOCS = 256
MAX_SOURCES_PER_IOC = 16
MAX_CLAIMS_PER_IOC = 32
MAX_OPERATIONS = 2_048
MAX_OPERATIONS_PER_IOC = 24
MAX_LIMITATION_CHARS = 500
MAX_EXCERPT_CHARS = 512
MAX_EXPORT_FILES = MAX_INVENTORY_IOCS * 4 + 4

_HASH_FAMILY_BY_LENGTH = {32: "md5", 40: "sha1", 64: "sha256"}
_HASH_PRECEDENCE = ("sha256", "sha1", "md5")
_KIND_PRECEDENCE = {"sample": 0, "ipv4": 1, "domain": 2, "url": 3}
_EXPORT_FORBIDDEN = re.compile(
    r"(?i)(?:x-amz-signature|x-goog-signature)\s*[=:]|"
    r"(?:api[_-]?key|access[_-]?token|client[_-]?secret)\s*[=:]\s*(?!\[redacted\])|"
    r"bearer\s+[A-Za-z0-9._~+/-]{16,}"
)
_SAFE_COMPONENT = re.compile(r"[^a-z0-9._-]+")
_BOOTSTRAP_NAMESPACE = UUID("6c1f9f3e-5c86-4f8d-9a5f-1b7c2f4a8d10")

# Planning reservation identities for the default proxy route (one v3 step,
# three pages).  The dispatcher re-reserves from the registered definition and
# the adapter's ``reservation_cost``, so a deployment with extra fallback steps
# or more pages may reserve more; these values mirror
# ``register_virustotal_tools`` (investigation_vt_tools.py) plus the one
# passive-read retry unit, and the ``vt.download_sample`` cost model
# (investigation_acquisition_tools.py).
_FILE_REPORT_UNITS = 2
_OBJECT_REPORT_UNITS = 3
_RELATIONSHIP_UNITS = 4
_BEHAVIOUR_UNITS = 4
_DOWNLOAD_UNITS = 1
_DEFAULT_TOOL_SECONDS = 120

# Bounded default relationships per object type, all inside the domain
# allowlists verified at import time.  A bootstrap plan is a starting point,
# not an exhaustive crawl.
_FILE_RELATIONS = ("contacted_domains", "contacted_ips", "contacted_urls", "dropped_files")
_IP_RELATIONS = ("communicating_files", "downloaded_files", "resolutions", "urls")
_DOMAIN_RELATIONS = (
    "communicating_files",
    "downloaded_files",
    "resolutions",
    "subdomains",
    "urls",
)
_URL_RELATIONS = ("communicating_files", "downloaded_files", "redirecting_urls")
_RELATIONS_BY_KIND: Mapping[str, tuple[str, ...]] = {
    "sample": _FILE_RELATIONS,
    "ipv4": _IP_RELATIONS,
    "domain": _DOMAIN_RELATIONS,
    "url": _URL_RELATIONS,
}
_VT_OBJECT_TYPE_BY_KIND: Mapping[str, VirusTotalObjectType] = {
    "sample": VirusTotalObjectType.FILE,
    "ipv4": VirusTotalObjectType.IP_ADDRESS,
    "domain": VirusTotalObjectType.DOMAIN,
    "url": VirusTotalObjectType.URL,
}
_OBJECT_REPORT_KIND_BY_KIND = {"ipv4": "ip_addresses", "domain": "domains", "url": "urls"}
_CHAIN_ARTIFACT_TYPE_BY_KIND = {
    "sample": "hash",
    "ipv4": "ip",
    "domain": "domain",
    "url": "url",
}


class BootstrapError(RuntimeError):
    """Typed local refusal; the API maps ``code`` onto an HTTP status."""

    code = "bootstrap_error"
    status_code = 422

    def __init__(self, message: str, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class BootstrapInvestigationNotFoundError(BootstrapError):
    code = "investigation_not_found"
    status_code = 404


class BootstrapArticleKindError(BootstrapError):
    """The investigation is not a verified ARTICLE publication."""

    code = "publication_kind_not_article"


class BootstrapSynthesisPinError(BootstrapError):
    """The workspace binding does not pin the verified synthesis hash."""

    code = "workspace_synthesis_pin_mismatch"
    status_code = 409


class BootstrapStateUnavailableError(BootstrapError):
    code = "bootstrap_state_unavailable"
    status_code = 409


class BootstrapSnapshotError(BootstrapError):
    code = "bootstrap_snapshot_unavailable"
    status_code = 503


class BootstrapExportRefusedError(BootstrapError):
    code = "bootstrap_export_refused"
    status_code = 422


class BootstrapIocKind(StrEnum):
    SAMPLE = "sample"
    IPV4 = "ipv4"
    DOMAIN = "domain"
    URL = "url"


class BootstrapIocOrigin(StrEnum):
    SEED = "seed"
    DISCOVERED = "discovered"


class BootstrapPlanState(StrEnum):
    EMPTY = "empty"
    AWAITING_G2 = "awaiting_g2"
    READY = "ready"
    BLOCKED = "blocked"
    UNAVAILABLE = "unavailable"


class BootstrapRouteStatus(StrEnum):
    CONFIGURED = "configured"
    UNVERIFIED = "unverified"
    UNAVAILABLE = "unavailable"


class BootstrapCoverageKind(StrEnum):
    CAPABILITY_ABSENT = "capability_absent"
    CAPABILITY_SHARED = "capability_shared"
    ROUTE_UNAVAILABLE = "route_unavailable"
    ROUTE_UNVERIFIED = "route_unverified"
    DEFERRED = "deferred"
    LIMIT = "limit"


class BootstrapReason(StrEnum):
    PENDING = "pending"
    AVAILABLE = "available"
    DOWNLOADED = "downloaded"
    REUSED = "reused"
    EMPTY = "empty"
    NOT_FOUND = "not_found"
    PROVIDER_DENIED = "provider_denied"
    ROUTE_UNAVAILABLE = "route_unavailable"
    QUOTA = "quota"
    FAILED = "failed"
    PARTIAL = "partial"
    NEEDS_REVIEW = "needs_review"
    POLICY_DENIED = "policy_denied"
    CAPABILITY_ABSENT = "capability_absent"
    PROVIDER_NOT_ALLOWED = "provider_not_allowed"
    ACQUISITION_NOT_ALLOWED = "acquisition_not_allowed"
    DISCLOSURE_BOUNDS = "disclosure_bounds"
    BUDGET_EXCEEDED = "budget_exceeded"
    AWAITING_G2 = "awaiting_g2"
    EVIDENCE_UNAVAILABLE = "evidence_unavailable"
    NOT_ATTEMPTED = "not_attempted"
    STOPPED = "stopped"
    INVOCATION_FAILED = "invocation_failed"


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class BootstrapVtCapabilities(_StrictModel):
    """The VT operations the backend is willing to expose; no transport claim."""

    file_report: bool = False
    relationships: bool = False
    behaviour: bool = False
    download: bool = False
    pcap: bool = False
    pcap_route_configured: bool = False

    @property
    def object_report(self) -> bool:
        """``vt.object_report`` is gated by the ``file_report`` capability today."""
        return self.file_report

    @classmethod
    def from_settings(cls, settings: Settings) -> Self:
        """Mirror ``virustotal_capabilities_from_settings`` without importing workers."""
        proxy_ready = bool(settings.virustotal_proxy_url and settings.virustotal_base_url)
        download_ready = (
            settings.virustotal_file_download_enabled
            and proxy_ready
            and VIRUSTOTAL_DOWNLOAD_REQUIRED_CDN_HOST
            in settings.virustotal_download_allowed_hosts.split(",")
            and bool(settings.virustotal_download_allowed_ports.strip())
        )
        pcap_ready = settings.virustotal_behaviour_pcap_enabled and proxy_ready
        return cls(
            file_report=settings.virustotal_file_report_enabled and proxy_ready,
            relationships=settings.virustotal_object_relationships_enabled and proxy_ready,
            behaviour=settings.virustotal_behaviour_reports_enabled and proxy_ready,
            download=download_ready,
            pcap=pcap_ready,
            # ``_default_routing_policy`` adds the proxy BEHAVIOUR_PCAP route
            # exactly when the flag is set and proxy routing exists
            # (infrastructure/virustotal.py:2569-2581).
            pcap_route_configured=pcap_ready,
        )

    @property
    def any_read(self) -> bool:
        return self.file_report or self.relationships or self.behaviour

    @property
    def any(self) -> bool:
        return self.any_read or self.download or self.pcap


class BootstrapHashAliasV1(_StrictModel):
    hash_type: Literal["md5", "sha1", "sha256"]
    value: str = Field(min_length=32, max_length=64, pattern=r"^[0-9a-f]+$")


class BootstrapIocSourceV1(_StrictModel):
    source_document_ids: tuple[UUID, ...] = Field(default=(), max_length=16)
    source_refs: tuple[str, ...] = Field(default=(), max_length=64)
    extraction_artifact_id: UUID | None = None
    extraction_artifact_version: int | None = Field(default=None, ge=1)
    claim_ids: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_IOC)


class BootstrapSampleAssociationV1(_StrictModel):
    association_id: UUID
    sample_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    origin: SampleOrigin
    qualification: SampleQualification
    version: int = Field(ge=1)


class BootstrapQuarantinedSampleV1(_StrictModel):
    sample_id: UUID
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: SampleState
    association_id: UUID | None = None
    qualification: SampleQualification | None = None


class BootstrapIocV1(_StrictModel):
    ioc_id: str = Field(pattern=r"^ioc-[0-9a-f]{24}$")
    kind: BootstrapIocKind
    origin: BootstrapIocOrigin
    value: str = Field(min_length=1, max_length=2_048)
    hash_type: Literal["md5", "sha1", "sha256"] | None = None
    hashes: tuple[BootstrapHashAliasV1, ...] = Field(default=(), max_length=3)
    sources: tuple[BootstrapIocSourceV1, ...] = Field(default=(), max_length=MAX_SOURCES_PER_IOC)
    claims: tuple[str, ...] = Field(default=(), max_length=MAX_CLAIMS_PER_IOC)
    associations: tuple[BootstrapSampleAssociationV1, ...] = Field(default=(), max_length=32)
    quarantined: tuple[BootstrapQuarantinedSampleV1, ...] = Field(default=(), max_length=32)
    limitations: tuple[str, ...] = Field(default=(), max_length=8)


class BootstrapInventoryLimitsV1(_StrictModel):
    max_iocs: int = Field(ge=1)
    max_sources_per_ioc: int = Field(ge=1)
    max_claims_per_ioc: int = Field(ge=1)
    iocs_considered: int = Field(ge=0)
    truncated: bool = False


class IOCInventoryV1(_StrictModel):
    schema_version: Literal["vt-bootstrap-inventory.v1"] = INVENTORY_SCHEMA_VERSION
    generated_at: datetime
    investigation_id: UUID
    subject_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    pinned_synthesis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_binding_id: str | None = None
    workspace_synthesis_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    seeds: tuple[BootstrapIocV1, ...] = Field(default=(), max_length=MAX_INVENTORY_IOCS)
    discovered: tuple[BootstrapIocV1, ...] = Field(default=())
    limits: BootstrapInventoryLimitsV1
    limitations: tuple[str, ...] = Field(default=(), max_length=16)


class BootstrapPlannedOperationV1(_StrictModel):
    operation_key: str = Field(min_length=64, max_length=64)
    ioc_id: str
    tool_name: str
    schema_version: Literal["1"] = "1"
    arguments: dict[str, Any] = Field(default_factory=dict)
    status: ToolInvocationStatus
    reason: BootstrapReason | None = None
    reserved_vt_units: int = Field(default=0, ge=0)
    reserved_downloads: int = Field(default=0, ge=0)
    requires_batch_evidence: bool = False
    route_status: BootstrapRouteStatus = BootstrapRouteStatus.CONFIGURED
    limitations: tuple[str, ...] = Field(default=(), max_length=8)


class BootstrapCoverageEntryV1(_StrictModel):
    kind: BootstrapCoverageKind
    target: str = Field(min_length=1, max_length=128)
    detail: str = Field(min_length=1, max_length=MAX_LIMITATION_CHARS)
    blocking: bool = False


class BootstrapBudgetViewV1(_StrictModel):
    vt_units_remaining: int | None = None
    downloads_remaining: int | None = None


class BootstrapPlanLimitsV1(_StrictModel):
    max_operations: int = Field(ge=1)
    operations_planned: int = Field(ge=0)
    operations_truncated: bool = False
    max_relationships_per_object: int = Field(ge=0)


class BootstrapPlanV1(_StrictModel):
    schema_version: Literal["vt-bootstrap-plan.v1"] = PLAN_SCHEMA_VERSION
    generated_at: datetime
    investigation_id: UUID
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    state: BootstrapPlanState
    envelope_version: int | None = Field(default=None, ge=1)
    envelope_approval_decision_id: UUID | None = None
    disclosure_tlp: TLPLevel | None = None
    budget: BootstrapBudgetViewV1 = Field(default_factory=BootstrapBudgetViewV1)
    operations: tuple[BootstrapPlannedOperationV1, ...] = Field(
        default=(), max_length=MAX_OPERATIONS
    )
    coverage: tuple[BootstrapCoverageEntryV1, ...] = Field(default=(), max_length=64)
    limits: BootstrapPlanLimitsV1
    limitations: tuple[str, ...] = Field(default=(), max_length=16)


class BootstrapSnapshotOperationV1(_StrictModel):
    operation_key: str
    ioc_id: str
    tool_name: str
    status: ToolInvocationStatus
    reason: BootstrapReason | None = None
    invocation_id: UUID | None = None
    idempotent_replay: bool = False
    observation_refs: tuple[UUID, ...] = Field(default=(), max_length=128)
    budget_consumed: dict[str, int] = Field(default_factory=dict)
    pages: int | None = Field(default=None, ge=0)
    observed_count: int | None = Field(default=None, ge=0)
    pagination_complete: bool | None = None
    next_cursor: str | None = Field(default=None, max_length=2_048)
    provider_routes: tuple[str, ...] = Field(default=(), max_length=8)
    detail_code: str | None = Field(default=None, max_length=128)
    error_code: str | None = Field(default=None, max_length=128)
    limitations: tuple[str, ...] = Field(default=(), max_length=8)


class BootstrapDownloadOutcomeV1(_StrictModel):
    status: Literal["downloaded", "reused", "not_attempted", "failed"]
    association_id: UUID | None = None
    sample_id: UUID | None = None
    sample_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    qualification: SampleQualification | None = None
    hash_verified_quarantine: bool = False
    reason: str | None = Field(default=None, max_length=128)


class BootstrapSnapshotIocV1(_StrictModel):
    ioc_id: str
    kind: BootstrapIocKind
    value: str = Field(min_length=1, max_length=2_048)
    hash_type: Literal["md5", "sha1", "sha256"] | None = None
    origin: BootstrapIocOrigin
    status: ToolInvocationStatus
    reason: BootstrapReason | None = None
    operations: tuple[BootstrapSnapshotOperationV1, ...] = Field(default=(), max_length=32)
    download: BootstrapDownloadOutcomeV1 | None = None
    association_ids: tuple[UUID, ...] = Field(default=(), max_length=32)
    quarantined_sample_ids: tuple[UUID, ...] = Field(default=(), max_length=32)


class BootstrapSnapshotLimitsV1(_StrictModel):
    max_iocs: int = Field(ge=1)
    iocs_included: int = Field(ge=0)
    inventory_truncated: bool = False
    max_operations: int = Field(ge=1)
    operations_recorded: int = Field(ge=0)
    operations_truncated: bool = False
    stopped_reason: BootstrapReason | None = None


class BootstrapSnapshotV1(_StrictModel):
    schema_version: Literal["vt-bootstrap-snapshot.v1"] = SNAPSHOT_SCHEMA_VERSION
    snapshot_id: UUID
    investigation_id: UUID
    pinned_synthesis_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    workspace_id: str | None = None
    generated_at: datetime
    plan_state: BootstrapPlanState
    envelope_version: int | None = None
    inventory_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    plan_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    iocs: tuple[BootstrapSnapshotIocV1, ...] = Field(default=(), max_length=MAX_INVENTORY_IOCS)
    coverage: tuple[BootstrapCoverageEntryV1, ...] = Field(default=(), max_length=64)
    limits: BootstrapSnapshotLimitsV1
    limitations: tuple[str, ...] = Field(default=(), max_length=16)


class BootstrapExportFileV1(_StrictModel):
    path: str = Field(pattern=r"^vt/(?:[A-Za-z0-9._-]+/)*[A-Za-z0-9._-]+\.json$")
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    size_bytes: int = Field(ge=0)
    content: str


class BootstrapSnapshotExportV1(_StrictModel):
    schema_version: Literal["vt-bootstrap-export.v1"] = EXPORT_SCHEMA_VERSION
    snapshot_id: UUID
    files: tuple[BootstrapExportFileV1, ...] = Field(default=(), max_length=MAX_EXPORT_FILES)


class BootstrapSampleReader(Protocol):
    async def list_by_subject_and_sha256(
        self, subject_id: UUID, sha256: str
    ) -> Sequence[Sample]: ...


class BootstrapUnitOfWork(Protocol):
    """The canonical stores read or appended by bootstrap."""

    analyst_investigations: AnalystInvestigationRepository
    investigation_envelopes: InvestigationEnvelopeRepository
    investigation_states: InvestigationStateRepository
    sample_associations: SampleAssociationRepository
    samples: BootstrapSampleReader
    workspace_bindings: WorkspaceBindingRepository
    production_runs: ProductionRunRepository
    production_routing_decisions: ProductionRoutingDecisionRepository
    evidence_observations: EvidenceObservationRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...

    async def commit(self) -> None: ...


class BootstrapToolExecutor(Protocol):
    """The real ``InvestigationService.execute_tool_call`` surface."""

    async def execute_tool_call(
        self,
        *,
        investigation: AnalystInvestigation,
        envelope: InvestigationEnvelope | None,
        budget: InvestigationBudget,
        state: InvestigationWorkingState,
        tool_name: str,
        schema_version: str,
        arguments: Mapping[str, Any],
        purpose: str,
        operation_key: str,
        actor_id: str | None = None,
        check_stop_before_dispatch: bool = True,
    ) -> tuple[ToolInvocation, ToolResult, tuple[UUID, ...], bool]: ...


@dataclass(frozen=True, slots=True)
class _IocDeclaration:
    kind: BootstrapIocKind
    value: str
    hash_type: Literal["md5", "sha1", "sha256"] | None
    source_document_ids: tuple[UUID, ...] = ()
    source_refs: tuple[str, ...] = ()
    extraction_artifact_id: UUID | None = None
    extraction_artifact_version: int | None = None
    context: str = ""
    evidence_quote: str = ""
    pack_only: bool = False


@dataclass(frozen=True, slots=True)
class _BootstrapContext:
    investigation: AnalystInvestigation
    run: ProductionRun
    article: ArticleSnapshot
    binding: WorkspaceBindingV1 | None
    associations: tuple[SampleAssociation, ...]
    inventory: IOCInventoryV1


@dataclass(frozen=True, slots=True)
class _OperationOutcome:
    operation_key: str
    ioc_id: str
    tool_name: str
    status: ToolInvocationStatus
    reason: BootstrapReason | None
    invocation_id: UUID | None = None
    idempotent_replay: bool = False
    observation_refs: tuple[UUID, ...] = ()
    output: Mapping[str, Any] = field(default_factory=dict)
    budget_consumed: Mapping[str, int] = field(default_factory=dict)
    error_code: str | None = None
    limitations: tuple[str, ...] = ()


def bootstrap_tool_operation_key(
    *,
    investigation_id: UUID,
    tool_name: str,
    arguments: Mapping[str, Any],
    purpose: str,
) -> str:
    """Return the canonical operation identity used by plan and execution."""
    return tool_operation_key(
        investigation_id=investigation_id,
        tool_name=tool_name,
        schema_version="1",
        normalized_parameters=arguments,
        purpose=purpose,
    )


def _ioc_id(kind: BootstrapIocKind, identity: str) -> str:
    digest = hashlib.sha256(f"{kind.value}\x00{identity}".encode()).hexdigest()
    return f"ioc-{digest[:24]}"


def _canonical_model_bytes(model: BaseModel) -> bytes:
    return ProductionArtifactStore.canonical_json_bytes(model.model_dump(mode="json"))


def _model_sha256(model: BaseModel) -> str:
    return hashlib.sha256(_canonical_model_bytes(model)).hexdigest()


def _hash_family(value: str) -> Literal["md5", "sha1", "sha256"] | None:
    family = _HASH_FAMILY_BY_LENGTH.get(len(value))
    if family is None or re.fullmatch(r"[0-9a-f]+", value) is None:
        return None
    return cast(Literal["md5", "sha1", "sha256"], family)


def _hash_alias(hash_type: str, value: str) -> BootstrapHashAliasV1 | None:
    family = _hash_family(value)
    if family is None or family != hash_type:
        return None
    return BootstrapHashAliasV1(hash_type=family, value=value)


def _primary_hash(hashes: Sequence[BootstrapHashAliasV1]) -> BootstrapHashAliasV1 | None:
    by_family = {item.hash_type: item for item in hashes}
    for family in _HASH_PRECEDENCE:
        item = by_family.get(cast(Literal["md5", "sha1", "sha256"], family))
        if item is not None:
            return item
    return None


def _value_pattern(kind: BootstrapIocKind, value: str) -> re.Pattern[str]:
    escaped = re.escape(value)
    if kind is BootstrapIocKind.SAMPLE:
        return re.compile(rf"(?<![0-9a-f]){escaped}(?![0-9a-f])", re.IGNORECASE)
    if kind in {BootstrapIocKind.IPV4, BootstrapIocKind.DOMAIN}:
        return re.compile(rf"(?<![\w.-]){escaped}(?![\w.-])", re.IGNORECASE)
    return re.compile(escaped, re.IGNORECASE)


def _claims_for_value(
    article: ArticleSnapshot, kind: BootstrapIocKind, value: str
) -> tuple[str, ...]:
    """Return article claim ids whose exact passage cites this canonical IOC."""
    pattern = _value_pattern(kind, value)
    claims: list[str] = []
    for passage in article.article_passages:
        if pattern.search(passage.text) is None:
            continue
        if passage.claim_id in claims:
            continue
        claims.append(passage.claim_id)
        if len(claims) >= MAX_CLAIMS_PER_IOC:
            break
    return tuple(claims)


def _source_key(
    source_document_ids: tuple[UUID, ...],
    source_refs: tuple[str, ...],
    extraction_artifact_id: UUID | None,
    extraction_artifact_version: int | None,
) -> tuple[tuple[str, ...], tuple[str, ...], str | None, int | None]:
    return (
        tuple(sorted(str(item) for item in source_document_ids)),
        tuple(sorted(source_refs)),
        str(extraction_artifact_id) if extraction_artifact_id is not None else None,
        extraction_artifact_version,
    )


class InvestigationBootstrapService:
    """Build the IOC inventory/plan and run authorised VT reads for one article."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], BootstrapUnitOfWork],
        article_snapshots: ArticleSnapshotPort,
        artifact_store: ProductionArtifactStore,
        capabilities: BootstrapVtCapabilities,
        clock: Callable[[], datetime] | None = None,
        disclosure_policy: AdvisorDisclosurePolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._article_snapshots = article_snapshots
        self._artifact_store = artifact_store
        self._capabilities = capabilities
        self._clock = clock or (lambda: datetime.now(UTC))
        self._disclosure_policy = disclosure_policy

    # ------------------------------------------------------------------ reads

    async def inventory(self, investigation_id: UUID) -> IOCInventoryV1:
        """Return the deterministic inventory; never contacts a provider."""
        async with self._uow_factory() as uow:
            context = await self._load_context(uow, investigation_id)
            return context.inventory

    async def plan(self, investigation_id: UUID) -> BootstrapPlanV1:
        """Project the currently authorised operations; never contacts a provider."""
        async with self._uow_factory() as uow:
            context = await self._load_context(uow, investigation_id)
            envelope = await uow.investigation_envelopes.get(investigation_id)
            return self._build_plan(context, envelope)

    async def latest_snapshot(self, investigation_id: UUID) -> BootstrapSnapshotV1 | None:
        """Return the most recent persisted bootstrap snapshot, if any."""
        async with self._uow_factory() as uow:
            if await uow.analyst_investigations.get(investigation_id) is None:
                raise BootstrapInvestigationNotFoundError("Investigation does not exist")
            observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        candidates = sorted(
            (
                item
                for item in observations
                if item.source == BOOTSTRAP_SOURCE
                and item.source_version == BOOTSTRAP_SOURCE_VERSION
                and item.object_type == BOOTSTRAP_SNAPSHOT_OBJECT_TYPE
            ),
            key=lambda item: (item.observed_at, str(item.id)),
            reverse=True,
        )
        for candidate in candidates:
            payload = await self._artifact_store.read_json(candidate.blob_id)
            encoded = ProductionArtifactStore.canonical_json_bytes(payload)
            if hashlib.sha256(encoded).hexdigest() != candidate.sha256:
                raise BootstrapSnapshotError("Snapshot blob does not match its evidence SHA-256")
            return BootstrapSnapshotV1.model_validate_json(encoded)
        return None

    # -------------------------------------------------------------------- run

    async def execute_batch(
        self,
        investigation_id: UUID,
        *,
        idempotency_key: str,
        actor_id: str,
        executor_factory: Callable[[BootstrapUnitOfWork], BootstrapToolExecutor],
    ) -> BootstrapSnapshotV1:
        """Execute the authorised batch once and persist a dated snapshot.

        Replaying the same ``idempotency_key`` returns the stored snapshot
        before any provider call; per-operation keys additionally let the real
        dispatcher replay a completed invocation instead of calling VT again.
        """
        if not idempotency_key.strip():
            raise ValueError("Bootstrap idempotency key must not be empty")
        snapshot_id = self._snapshot_id(investigation_id, idempotency_key)
        async with self._uow_factory() as uow:
            context = await self._load_context(uow, investigation_id)
            existing = await self._find_snapshot(uow, investigation_id, snapshot_id)
            if existing is not None:
                return existing
            envelope = await uow.investigation_envelopes.get(investigation_id)
            plan = self._build_plan(context, envelope)
            generated_at = self._now()
            if plan.state is not BootstrapPlanState.READY or envelope is None:
                snapshot = self._snapshot_from_plan(
                    context,
                    plan,
                    snapshot_id=snapshot_id,
                    generated_at=generated_at,
                    outcomes=tuple(
                        self._planned_outcome(operation) for operation in plan.operations
                    ),
                    stopped_reason=(
                        BootstrapReason.AWAITING_G2
                        if plan.state is BootstrapPlanState.AWAITING_G2
                        else None
                    ),
                )
                await self._persist_snapshot(uow, snapshot, idempotency_key)
                return snapshot

            state = await uow.investigation_states.get(investigation_id)
            if state is None:
                raise BootstrapStateUnavailableError(
                    "The investigation working state must exist before a bootstrap batch"
                )
            executor = executor_factory(uow)
            prior_evidence = await self._prior_file_report_refs(uow, investigation_id)
            outcomes, stopped_reason = await self._dispatch_operations(
                uow=uow,
                context=context,
                plan=plan,
                state=state,
                actor_id=actor_id,
                executor=executor,
                prior_evidence=prior_evidence,
            )
            snapshot = self._snapshot_from_plan(
                context,
                plan,
                snapshot_id=snapshot_id,
                generated_at=generated_at,
                outcomes=outcomes,
                stopped_reason=stopped_reason,
            )
            await self._persist_snapshot(uow, snapshot, idempotency_key)
            return snapshot

    # ---------------------------------------------------------------- context

    async def _load_context(
        self, uow: BootstrapUnitOfWork, investigation_id: UUID
    ) -> _BootstrapContext:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise BootstrapInvestigationNotFoundError("Investigation does not exist")
        run = await uow.production_runs.get(investigation.production_run_id)
        if run is None:
            raise BootstrapArticleKindError(
                "The production run identity is unavailable", code="publication_kind_unavailable"
            )
        decision = await get_adopted_production_routing_decision(
            uow=cast(ProductionUnitOfWork, uow), run=run
        )
        if decision is None or decision.publication_kind is None:
            raise BootstrapArticleKindError(
                "The run has no adopted publication routing decision",
                code="publication_kind_unavailable",
            )
        if decision.publication_kind is not PublicationKind.ARTICLE:
            raise BootstrapArticleKindError(
                "VT bootstrap requires an ARTICLE publication", code="publication_kind_not_article"
            )
        article = await self._article_snapshots.resolve(investigation.synthesis_artifact_id)
        binding = await uow.workspace_bindings.get_for_investigation(investigation_id)
        if binding is not None and binding.synthesis_sha256 != article.synthesis_sha256:
            raise BootstrapSynthesisPinError(
                "The workspace binding does not pin the verified synthesis SHA-256"
            )
        associations = tuple(await uow.sample_associations.list_for_investigation(investigation_id))
        inventory = await self._build_inventory(
            uow,
            investigation=investigation,
            article=article,
            binding=binding,
            associations=associations,
        )
        return _BootstrapContext(
            investigation=investigation,
            run=run,
            article=article,
            binding=binding,
            associations=associations,
            inventory=inventory,
        )

    async def _build_inventory(
        self,
        uow: BootstrapUnitOfWork,
        *,
        investigation: AnalystInvestigation,
        article: ArticleSnapshot,
        binding: WorkspaceBindingV1 | None,
        associations: tuple[SampleAssociation, ...],
    ) -> IOCInventoryV1:
        declarations = self._article_declarations(article)
        grouped = self._group_declarations(declarations)
        limitations: list[str] = []
        considered = len(grouped)
        truncated = considered > MAX_INVENTORY_IOCS
        selected = grouped[:MAX_INVENTORY_IOCS]
        if truncated:
            limitations.append(
                f"L'inventaire est borné à {MAX_INVENTORY_IOCS} IOC ; {considered} ont été "
                "observés."
            )
        seeds: list[BootstrapIocV1] = []
        for kind, identity, items in selected:
            ioc = await self._build_ioc(
                uow,
                investigation=investigation,
                article=article,
                kind=kind,
                identity=identity,
                declarations=items,
                associations=associations,
            )
            seeds.append(ioc)
        if any(ioc.kind is BootstrapIocKind.SAMPLE and ioc.hash_type != "sha256" for ioc in seeds):
            limitations.append(
                "Un échantillon sans SHA-256 ne peut être ni associé ni acquis par le lot "
                "d'acquisition (identité d'association limitée au SHA-256)."
            )
        return IOCInventoryV1(
            generated_at=self._now(),
            investigation_id=investigation.id,
            subject_id=investigation.subject_id,
            production_run_id=investigation.production_run_id,
            synthesis_artifact_id=investigation.synthesis_artifact_id,
            pinned_synthesis_sha256=article.synthesis_sha256,
            workspace_binding_id=binding.workspace_id if binding is not None else None,
            workspace_synthesis_sha256=(binding.synthesis_sha256 if binding is not None else None),
            seeds=tuple(seeds),
            discovered=(),
            limits=BootstrapInventoryLimitsV1(
                max_iocs=MAX_INVENTORY_IOCS,
                max_sources_per_ioc=MAX_SOURCES_PER_IOC,
                max_claims_per_ioc=MAX_CLAIMS_PER_IOC,
                iocs_considered=considered,
                truncated=truncated,
            ),
            limitations=tuple(limitations[:16]),
        )

    def _article_declarations(self, article: ArticleSnapshot) -> list[_IocDeclaration]:
        declarations: list[_IocDeclaration] = []
        chain_by_value: dict[tuple[str, str], Mapping[str, Any]] = {}
        for row in article.article.known_chain:
            if row.get("kind") != "documented_ioc":
                continue
            artifact_type = row.get("artifact_type")
            value = row.get("value")
            if isinstance(artifact_type, str) and isinstance(value, str):
                chain_by_value[(artifact_type, value)] = row
        declared_values: set[str] = set()
        for indicator in article.confirmed_iocs:
            declaration = self._declaration_from_confirmed_ioc(indicator, chain_by_value)
            if declaration is None:
                continue
            declarations.append(declaration)
            declared_values.add(declaration.value)
        for value in article.article.documented_samples:
            family = _hash_family(value)
            if family is None or value in declared_values:
                continue
            declared_values.add(value)
            declarations.append(
                _IocDeclaration(
                    kind=BootstrapIocKind.SAMPLE,
                    value=value,
                    hash_type=family,
                    pack_only=True,
                )
            )
        return declarations

    def _declaration_from_confirmed_ioc(
        self,
        indicator: ConfirmedIOCContextV1,
        chain_by_value: Mapping[tuple[str, str], Mapping[str, Any]],
    ) -> _IocDeclaration | None:
        value = indicator.value
        context = ""
        evidence_quote = ""
        source_refs: tuple[str, ...] = ()
        kind = BootstrapIocKind.SAMPLE
        hash_type: Literal["md5", "sha1", "sha256"] | None = None
        if indicator.indicator_type in {"md5", "sha1", "sha256"}:
            hash_type = cast(Literal["md5", "sha1", "sha256"], indicator.indicator_type)
        elif indicator.indicator_type == "ipv4":
            kind = BootstrapIocKind.IPV4
        elif indicator.indicator_type == "domain":
            kind = BootstrapIocKind.DOMAIN
        else:
            kind = BootstrapIocKind.URL
        row = chain_by_value.get((_CHAIN_ARTIFACT_TYPE_BY_KIND[kind.value], value))
        if row is not None:
            raw_context = row.get("context")
            raw_quote = row.get("evidence_quote")
            context = raw_context[:MAX_EXCERPT_CHARS] if isinstance(raw_context, str) else ""
            evidence_quote = raw_quote[:MAX_EXCERPT_CHARS] if isinstance(raw_quote, str) else ""
            raw_refs = row.get("source_refs")
            if isinstance(raw_refs, list | tuple):
                source_refs = tuple(str(item) for item in raw_refs if isinstance(item, str))[:64]
        if hash_type is not None and _hash_family(value) != hash_type:
            return None
        return _IocDeclaration(
            kind=kind,
            value=value,
            hash_type=hash_type,
            source_document_ids=indicator.source_document_ids,
            source_refs=source_refs,
            extraction_artifact_id=indicator.extraction_artifact_id,
            extraction_artifact_version=indicator.extraction_artifact_version,
            context=context,
            evidence_quote=evidence_quote,
        )

    def _group_declarations(
        self, declarations: Sequence[_IocDeclaration]
    ) -> list[tuple[BootstrapIocKind, str, tuple[_IocDeclaration, ...]]]:
        """Deduplicate values and group hash families that cite one IOC table row.

        Two hash families are merged only when the article proves they describe
        the same sample: identical extraction artifact, source references,
        context and evidence quote (one cited row listing several hash types).
        Without that proof the values stay distinct: a bootstrap never guesses
        an md5<->sha256 equivalence.
        """
        groups: dict[str, list[_IocDeclaration]] = {}
        for declaration in declarations:
            if declaration.kind is BootstrapIocKind.SAMPLE:
                groups.setdefault(self._hash_group_key(declaration), []).append(declaration)
            else:
                groups.setdefault(f"{declaration.kind.value}\x00{declaration.value}", []).append(
                    declaration
                )
        result: list[tuple[BootstrapIocKind, str, tuple[_IocDeclaration, ...]]] = []
        for items in groups.values():
            kind = items[0].kind
            identity = self._group_identity(items)
            result.append((kind, identity, tuple(items)))
        result.sort(key=lambda item: (_KIND_PRECEDENCE[item[0].value], item[1]))
        return result

    @staticmethod
    def _hash_group_key(declaration: _IocDeclaration) -> str:
        if declaration.pack_only or not (declaration.context or declaration.evidence_quote):
            return f"sample\x00value\x00{declaration.value}"
        source_key = _source_key(
            declaration.source_document_ids,
            declaration.source_refs,
            declaration.extraction_artifact_id,
            declaration.extraction_artifact_version,
        )
        return "sample\x00row\x00" + json.dumps(
            [
                source_key[0],
                source_key[1],
                source_key[2],
                source_key[3],
                declaration.context,
                declaration.evidence_quote,
            ],
            separators=(",", ":"),
            sort_keys=True,
        )

    @staticmethod
    def _group_identity(items: Sequence[_IocDeclaration]) -> str:
        if items[0].kind is not BootstrapIocKind.SAMPLE:
            return items[0].value
        aliases: list[BootstrapHashAliasV1] = []
        for item in items:
            if item.hash_type is None:
                continue
            alias = _hash_alias(item.hash_type, item.value)
            if alias is not None:
                aliases.append(alias)
        primary = _primary_hash(aliases)
        return primary.value if primary is not None else items[0].value

    async def _build_ioc(
        self,
        uow: BootstrapUnitOfWork,
        *,
        investigation: AnalystInvestigation,
        article: ArticleSnapshot,
        kind: BootstrapIocKind,
        identity: str,
        declarations: tuple[_IocDeclaration, ...],
        associations: tuple[SampleAssociation, ...],
    ) -> BootstrapIocV1:
        hash_aliases: tuple[BootstrapHashAliasV1, ...] = ()
        hash_type: Literal["md5", "sha1", "sha256"] | None = None
        value = identity
        primary_sha256: str | None = None
        if kind is BootstrapIocKind.SAMPLE:
            aliases: list[BootstrapHashAliasV1] = []
            seen: set[str] = set()
            for item in declarations:
                if item.hash_type is None or item.value in seen:
                    continue
                alias = _hash_alias(item.hash_type, item.value)
                if alias is None:
                    continue
                seen.add(item.value)
                aliases.append(alias)
            aliases.sort(key=lambda item: (item.hash_type, item.value))
            primary = _primary_hash(aliases)
            if primary is None:
                raise BootstrapError("A sample IOC declaration has no hash family")
            hash_aliases = tuple(aliases)
            hash_type = primary.hash_type
            value = primary.value
            if hash_type == "sha256":
                primary_sha256 = primary.value
        claims = _claims_for_value(article, kind, value)
        if kind is BootstrapIocKind.SAMPLE:
            for alias in hash_aliases:
                if alias.value == value:
                    continue
                claims = tuple(
                    dict.fromkeys((*claims, *_claims_for_value(article, kind, alias.value)))
                )[:MAX_CLAIMS_PER_IOC]
        sources = self._merge_sources(declarations, claims)
        ioc_associations: tuple[BootstrapSampleAssociationV1, ...] = ()
        quarantined: tuple[BootstrapQuarantinedSampleV1, ...] = ()
        limitations: list[str] = []
        if kind is BootstrapIocKind.SAMPLE:
            ioc_associations = tuple(
                BootstrapSampleAssociationV1(
                    association_id=item.id,
                    sample_sha256=item.sample_sha256,
                    origin=item.origin,
                    qualification=item.qualification,
                    version=item.version,
                )
                for item in associations
                if item.sample_sha256 == primary_sha256
            )
            if primary_sha256 is not None:
                quarantined = await self._quarantined_samples(
                    uow, investigation.subject_id, primary_sha256, ioc_associations
                )
            if not ioc_associations and quarantined:
                limitations.append(
                    "Un échantillon en quarantaine existe pour le sujet mais n'est pas "
                    "associé à cette enquête ; l'acquisition exige une association explicite."
                )
        if len(declarations) > 1 and kind is BootstrapIocKind.SAMPLE:
            limitations.append(
                "Les familles d'empreintes de cet IOC sont regroupées uniquement parce que "
                "l'article les cite dans la même ligne de preuve."
            )
        return BootstrapIocV1(
            ioc_id=_ioc_id(kind, identity),
            kind=kind,
            origin=BootstrapIocOrigin.SEED,
            value=value,
            hash_type=hash_type,
            hashes=hash_aliases,
            sources=sources,
            claims=claims,
            associations=ioc_associations,
            quarantined=quarantined,
            limitations=tuple(limitations[:8]),
        )

    async def _quarantined_samples(
        self,
        uow: BootstrapUnitOfWork,
        subject_id: UUID,
        sha256: str,
        associations: tuple[BootstrapSampleAssociationV1, ...],
    ) -> tuple[BootstrapQuarantinedSampleV1, ...]:
        samples = await uow.samples.list_by_subject_and_sha256(subject_id, sha256)
        association = next((item for item in associations if item.sample_sha256 == sha256), None)
        return tuple(
            BootstrapQuarantinedSampleV1(
                sample_id=sample.id,
                sha256=sha256,
                state=sample.state,
                association_id=association.association_id if association is not None else None,
                qualification=association.qualification if association is not None else None,
            )
            for sample in samples
            if sample.state in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
        )[:32]

    @staticmethod
    def _merge_sources(
        declarations: Sequence[_IocDeclaration], claims: tuple[str, ...]
    ) -> tuple[BootstrapIocSourceV1, ...]:
        merged: dict[
            tuple[tuple[str, ...], tuple[str, ...], str | None, int | None],
            BootstrapIocSourceV1,
        ] = {}
        for declaration in declarations:
            key = _source_key(
                declaration.source_document_ids,
                declaration.source_refs,
                declaration.extraction_artifact_id,
                declaration.extraction_artifact_version,
            )
            existing = merged.get(key)
            if existing is None:
                merged[key] = BootstrapIocSourceV1(
                    source_document_ids=declaration.source_document_ids,
                    source_refs=declaration.source_refs,
                    extraction_artifact_id=declaration.extraction_artifact_id,
                    extraction_artifact_version=declaration.extraction_artifact_version,
                    claim_ids=claims,
                )
            elif not existing.claim_ids and claims:
                merged[key] = existing.model_copy(update={"claim_ids": claims})
        ordered = sorted(
            merged.values(),
            key=lambda item: (
                str(item.extraction_artifact_id),
                item.source_refs,
                tuple(str(value) for value in item.source_document_ids),
            ),
        )
        return tuple(ordered[:MAX_SOURCES_PER_IOC])

    # ------------------------------------------------------------------- plan

    def _build_plan(
        self, context: _BootstrapContext, envelope: InvestigationEnvelope | None
    ) -> BootstrapPlanV1:
        coverage = self._coverage_entries(context.inventory)
        if not context.inventory.seeds:
            return self._plan_envelopeless(
                context, envelope, BootstrapPlanState.EMPTY, coverage, limitations=()
            )
        if envelope is None:
            return self._plan_envelopeless(
                context,
                None,
                BootstrapPlanState.AWAITING_G2,
                coverage,
                limitations=(
                    "Aucune enveloppe G2 approuvée : aucune opération VirusTotal n'est "
                    "planifiée comme autorisée et aucun appel fournisseur n'est émis.",
                ),
            )
        simulated = self._simulated_budget(envelope.budget)
        operations: list[BootstrapPlannedOperationV1] = []
        truncated = False
        for ioc in context.inventory.seeds:
            if truncated:
                break
            for operation in self._ioc_operations(ioc, context.investigation.id):
                if len(operations) >= MAX_OPERATIONS:
                    truncated = True
                    break
                operations.append(self._authorize(operation, envelope, simulated))
        executable = [item for item in operations if item.status is ToolInvocationStatus.PENDING]
        if executable:
            state = BootstrapPlanState.READY
        elif not self._capabilities.any:
            state = BootstrapPlanState.UNAVAILABLE
        elif all(
            item.reason in {BootstrapReason.CAPABILITY_ABSENT, BootstrapReason.ROUTE_UNAVAILABLE}
            for item in operations
        ):
            state = BootstrapPlanState.UNAVAILABLE
        else:
            state = BootstrapPlanState.BLOCKED
        return BootstrapPlanV1(
            generated_at=self._now(),
            investigation_id=context.investigation.id,
            inventory_sha256=_model_sha256(context.inventory),
            state=state,
            envelope_version=envelope.version,
            envelope_approval_decision_id=envelope.approval_decision_id,
            disclosure_tlp=envelope.disclosure.tlp,
            budget=BootstrapBudgetViewV1(
                vt_units_remaining=envelope.budget.remaining.get(BudgetDimension.VT_UNITS),
                downloads_remaining=envelope.budget.remaining.get(BudgetDimension.DOWNLOADS),
            ),
            operations=tuple(operations),
            coverage=coverage,
            limits=BootstrapPlanLimitsV1(
                max_operations=MAX_OPERATIONS,
                operations_planned=len(operations),
                operations_truncated=truncated,
                max_relationships_per_object=max(
                    len(items) for items in _RELATIONS_BY_KIND.values()
                ),
            ),
            limitations=(
                "Les unités VT réservées supposent la route proxy par défaut ; le dispatcher "
                "re-vérifie et re-réserve le coût réel avant chaque appel.",
            ),
        )

    def _plan_envelopeless(
        self,
        context: _BootstrapContext,
        envelope: InvestigationEnvelope | None,
        state: BootstrapPlanState,
        coverage: tuple[BootstrapCoverageEntryV1, ...],
        *,
        limitations: tuple[str, ...],
    ) -> BootstrapPlanV1:
        operations: list[BootstrapPlannedOperationV1] = []
        for ioc in context.inventory.seeds:
            if len(operations) >= MAX_OPERATIONS:
                break
            for operation in self._ioc_operations(ioc, context.investigation.id):
                if len(operations) >= MAX_OPERATIONS:
                    break
                operations.append(
                    operation.model_copy(
                        update={
                            "status": ToolInvocationStatus.PENDING,
                            "reason": BootstrapReason.AWAITING_G2,
                        }
                    )
                )
        return BootstrapPlanV1(
            generated_at=self._now(),
            investigation_id=context.investigation.id,
            inventory_sha256=_model_sha256(context.inventory),
            state=state if context.inventory.seeds else BootstrapPlanState.EMPTY,
            envelope_version=envelope.version if envelope is not None else None,
            envelope_approval_decision_id=(
                envelope.approval_decision_id if envelope is not None else None
            ),
            disclosure_tlp=envelope.disclosure.tlp if envelope is not None else None,
            budget=BootstrapBudgetViewV1(
                vt_units_remaining=(
                    envelope.budget.remaining.get(BudgetDimension.VT_UNITS)
                    if envelope is not None
                    else None
                ),
                downloads_remaining=(
                    envelope.budget.remaining.get(BudgetDimension.DOWNLOADS)
                    if envelope is not None
                    else None
                ),
            ),
            operations=tuple(operations),
            coverage=coverage,
            limits=BootstrapPlanLimitsV1(
                max_operations=MAX_OPERATIONS,
                operations_planned=len(operations),
                operations_truncated=False,
                max_relationships_per_object=max(
                    len(items) for items in _RELATIONS_BY_KIND.values()
                ),
            ),
            limitations=limitations,
        )

    def _ioc_operations(
        self, ioc: BootstrapIocV1, investigation_id: UUID
    ) -> tuple[BootstrapPlannedOperationV1, ...]:
        operations: list[BootstrapPlannedOperationV1] = []
        kind = ioc.kind
        if kind is BootstrapIocKind.SAMPLE:
            if ioc.hash_type is None:
                return ()
            if self._capabilities.file_report:
                operations.append(
                    self._planned(
                        investigation_id,
                        ioc,
                        "vt.file_report",
                        {"file_hash": ioc.value},
                        reserved_vt_units=_FILE_REPORT_UNITS,
                        limitations=(
                            "Le rapport fichier est un instantané daté ; vérifier les dates "
                            "d'analyse et de modification.",
                        ),
                    )
                )
            if self._capabilities.behaviour:
                operations.append(
                    self._planned(
                        investigation_id,
                        ioc,
                        "vt.behaviour",
                        {"file_hash": ioc.value},
                        reserved_vt_units=_BEHAVIOUR_UNITS,
                        limitations=(
                            "Les rapports de comportement sont limités aux sandboxes VT "
                            "disponibles ; l'absence de rapport n'est pas une absence de "
                            "comportement.",
                        ),
                    )
                )
            if self._capabilities.relationships:
                for relation in _FILE_RELATIONS:
                    operations.append(
                        self._planned(
                            investigation_id,
                            ioc,
                            "vt.relationships",
                            {
                                "object_type": "file",
                                "object_id": ioc.value,
                                "relation": relation,
                            },
                            reserved_vt_units=_RELATIONSHIP_UNITS,
                        )
                    )
            if self._capabilities.download:
                if ioc.hash_type == "sha256":
                    operations.append(
                        self._planned(
                            investigation_id,
                            ioc,
                            "vt.download_sample",
                            {
                                "sha256": ioc.value,
                                "purpose": (
                                    "Bootstrap initial : acquérir l'échantillon cité par "
                                    "l'article si le G2 l'autorise."
                                ),
                            },
                            reserved_vt_units=_DOWNLOAD_UNITS,
                            reserved_downloads=1,
                            requires_batch_evidence=True,
                            route_status=BootstrapRouteStatus.UNVERIFIED,
                            limitations=(
                                "La route proxy de téléchargement est HOST-UNKNOWN (ancien "
                                "audit 404) ; un échec de route devient route_unavailable, "
                                "jamais un succès inventé.",
                            ),
                        )
                    )
        else:
            object_kind = _OBJECT_REPORT_KIND_BY_KIND[kind.value]
            if self._capabilities.object_report:
                operations.append(
                    self._planned(
                        investigation_id,
                        ioc,
                        "vt.object_report",
                        {"object_type": object_kind, "object_id": ioc.value},
                        reserved_vt_units=_OBJECT_REPORT_UNITS,
                        limitations=(
                            "Le rapport d'objet est gaté par la capability file_report "
                            "(aucun flag séparé aujourd'hui).",
                        ),
                    )
                )
            if self._capabilities.relationships:
                object_type = _VT_OBJECT_TYPE_BY_KIND[kind.value].value
                for relation in _RELATIONS_BY_KIND[kind.value]:
                    operations.append(
                        self._planned(
                            investigation_id,
                            ioc,
                            "vt.relationships",
                            {
                                "object_type": object_type,
                                "object_id": ioc.value,
                                "relation": relation,
                            },
                            reserved_vt_units=_RELATIONSHIP_UNITS,
                        )
                    )
        return tuple(operations[:MAX_OPERATIONS_PER_IOC])

    @staticmethod
    def _planned(
        investigation_id: UUID,
        ioc: BootstrapIocV1,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        reserved_vt_units: int,
        reserved_downloads: int = 0,
        requires_batch_evidence: bool = False,
        route_status: BootstrapRouteStatus = BootstrapRouteStatus.CONFIGURED,
        limitations: tuple[str, ...] = (),
    ) -> BootstrapPlannedOperationV1:
        purpose = f"Bootstrap initial VT: {tool_name} pour {ioc.ioc_id}."
        key = bootstrap_tool_operation_key(
            investigation_id=investigation_id,
            tool_name=tool_name,
            arguments=arguments,
            purpose=purpose,
        )
        return BootstrapPlannedOperationV1(
            operation_key=key,
            ioc_id=ioc.ioc_id,
            tool_name=tool_name,
            arguments=arguments,
            status=ToolInvocationStatus.PENDING,
            reason=BootstrapReason.PENDING,
            reserved_vt_units=reserved_vt_units,
            reserved_downloads=reserved_downloads,
            requires_batch_evidence=requires_batch_evidence,
            route_status=route_status,
            limitations=limitations,
        )

    def _authorize(
        self,
        operation: BootstrapPlannedOperationV1,
        envelope: InvestigationEnvelope,
        simulated: InvestigationBudget,
    ) -> BootstrapPlannedOperationV1:
        producer = operation.tool_name.partition(".")[0]
        if producer not in envelope.allowed_providers:
            return self._denied(
                operation,
                ToolInvocationStatus.POLICY_DENIED,
                BootstrapReason.PROVIDER_NOT_ALLOWED,
            )
        policy = self._disclosure_policy or advisor_disclosure_policy()
        if envelope.disclosure.tlp is TLPLevel.RED and not policy.ignores_restriction_labels(
            operation.tool_name
        ):
            return self._denied(
                operation,
                ToolInvocationStatus.POLICY_DENIED,
                BootstrapReason.DISCLOSURE_BOUNDS,
            )
        if operation.tool_name == "vt.download_sample" and not envelope.acquire_hits_allowed:
            return self._denied(
                operation,
                ToolInvocationStatus.POLICY_DENIED,
                BootstrapReason.ACQUISITION_NOT_ALLOWED,
            )
        amounts: dict[BudgetDimension, int] = {
            BudgetDimension.VT_UNITS: operation.reserved_vt_units,
            BudgetDimension.TOOL_SECONDS: _DEFAULT_TOOL_SECONDS,
        }
        if operation.reserved_downloads:
            amounts[BudgetDimension.DOWNLOADS] = operation.reserved_downloads
        try:
            simulated.reserve(operation.operation_key, amounts)
        except BudgetExceeded:
            return self._denied(
                operation,
                ToolInvocationStatus.NEEDS_REVIEW,
                BootstrapReason.BUDGET_EXCEEDED,
            )
        return operation.model_copy(
            update={"status": ToolInvocationStatus.PENDING, "reason": BootstrapReason.PENDING}
        )

    @staticmethod
    def _denied(
        operation: BootstrapPlannedOperationV1,
        status: ToolInvocationStatus,
        reason: BootstrapReason,
    ) -> BootstrapPlannedOperationV1:
        return operation.model_copy(update={"status": status, "reason": reason})

    @staticmethod
    def _simulated_budget(budget: InvestigationBudget) -> InvestigationBudget:
        simulated = InvestigationBudget(limits=dict(budget.limits))
        for reservation in budget.reservations:
            simulated.restore_reservation(reservation)
        return simulated

    def _coverage_entries(self, inventory: IOCInventoryV1) -> tuple[BootstrapCoverageEntryV1, ...]:
        entries: list[BootstrapCoverageEntryV1] = [
            BootstrapCoverageEntryV1(
                kind=BootstrapCoverageKind.CAPABILITY_SHARED,
                target="vt.object_report",
                detail=(
                    "vt.object_report est gaté par la capability file_report ; aucun flag "
                    "dédié IP/domaine/URL n'existe (inventaire backend §2)."
                ),
            )
        ]
        if any(
            ioc.kind in {BootstrapIocKind.IPV4, BootstrapIocKind.DOMAIN, BootstrapIocKind.URL}
            for ioc in inventory.seeds
        ):
            entries.append(
                BootstrapCoverageEntryV1(
                    kind=BootstrapCoverageKind.CAPABILITY_ABSENT,
                    target="vt.comments",
                    detail=(
                        "Aucune opération/port autonome de commentaires VT n'existe ; les "
                        "commentaires restent accessibles par les relations allowlistées "
                        "comments/related_comments des IP et domaines."
                    ),
                )
            )
        if self._capabilities.download:
            entries.append(
                BootstrapCoverageEntryV1(
                    kind=BootstrapCoverageKind.ROUTE_UNVERIFIED,
                    target="vt.file_download",
                    detail=(
                        "La route proxy de téléchargement est HOST-UNKNOWN (ancien audit "
                        "404) ; le lot ne prétend pas qu'elle fonctionne."
                    ),
                )
            )
        if not self._capabilities.pcap:
            entries.append(
                BootstrapCoverageEntryV1(
                    kind=BootstrapCoverageKind.CAPABILITY_ABSENT,
                    target="vt.behaviour_pcap",
                    detail=(
                        "La capability behaviour_pcap est désactivée ; aucun PCAP n'est planifié."
                    ),
                )
            )
        elif not self._capabilities.pcap_route_configured:
            entries.append(
                BootstrapCoverageEntryV1(
                    kind=BootstrapCoverageKind.ROUTE_UNAVAILABLE,
                    target="vt.behaviour_pcap",
                    detail=(
                        "La capability PCAP est activée mais aucune route proxy PCAP n'est "
                        "configurée ; aucun PCAP n'est planifié."
                    ),
                )
            )
        else:
            entries.append(
                BootstrapCoverageEntryV1(
                    kind=BootstrapCoverageKind.DEFERRED,
                    target="vt.behaviour_pcap",
                    detail=(
                        "Le PCAP exige un sandbox_id issu d'un rapport behaviour persisté ; "
                        "aucune opération PCAP concrète n'est planifiée par lot."
                    ),
                )
            )
        if not self._capabilities.any:
            entries.append(
                BootstrapCoverageEntryV1(
                    kind=BootstrapCoverageKind.CAPABILITY_ABSENT,
                    target="vt",
                    detail=(
                        "Aucune capability VirusTotal n'est activée : l'inventaire et le "
                        "snapshot restent disponibles, la collecte est indisponible."
                    ),
                )
            )
        if inventory.limits.truncated:
            entries.append(
                BootstrapCoverageEntryV1(
                    kind=BootstrapCoverageKind.LIMIT,
                    target="inventory",
                    detail="L'inventaire est tronqué au plafond configuré du lot.",
                )
            )
        return tuple(entries[:64])

    # --------------------------------------------------------------- snapshot

    async def _dispatch_operations(
        self,
        *,
        uow: BootstrapUnitOfWork,
        context: _BootstrapContext,
        plan: BootstrapPlanV1,
        state: InvestigationWorkingState,
        actor_id: str,
        executor: BootstrapToolExecutor,
        prior_evidence: Mapping[str, tuple[UUID, ...]],
    ) -> tuple[tuple[_OperationOutcome, ...], BootstrapReason | None]:
        batch_refs: dict[str, list[UUID]] = {}
        outcomes: list[_OperationOutcome] = []
        stopped_reason: BootstrapReason | None = None
        for planned in plan.operations:
            if planned.status is not ToolInvocationStatus.PENDING:
                outcomes.append(self._planned_outcome(planned))
                continue
            if stopped_reason is not None:
                outcomes.append(
                    _OperationOutcome(
                        operation_key=planned.operation_key,
                        ioc_id=planned.ioc_id,
                        tool_name=planned.tool_name,
                        status=ToolInvocationStatus.PENDING,
                        reason=BootstrapReason.STOPPED,
                    )
                )
                continue
            # Each tool call advances the envelope version; reload the current
            # canonical envelope so a later operation is admitted on the same
            # authority and budget the dispatcher will re-check.
            envelope = await uow.investigation_envelopes.get(context.investigation.id)
            if envelope is None:
                outcomes.append(
                    _OperationOutcome(
                        operation_key=planned.operation_key,
                        ioc_id=planned.ioc_id,
                        tool_name=planned.tool_name,
                        status=ToolInvocationStatus.POLICY_DENIED,
                        reason=BootstrapReason.POLICY_DENIED,
                        error_code="envelope_revoked",
                    )
                )
                stopped_reason = BootstrapReason.POLICY_DENIED
                continue
            arguments = dict(planned.arguments)
            operation_key = planned.operation_key
            purpose = f"Bootstrap initial VT: {planned.tool_name} ({planned.ioc_id})."
            if planned.requires_batch_evidence:
                raw_refs = tuple(batch_refs.get(planned.ioc_id, ())) or prior_evidence.get(
                    str(arguments.get("sha256", "")), ()
                )
                # A stable order keeps the download operation key identical
                # across replays, so the durable invocation is reused instead
                # of a second provider download.
                refs = tuple(sorted(dict.fromkeys(raw_refs), key=str))
                if not refs:
                    outcomes.append(
                        _OperationOutcome(
                            operation_key=planned.operation_key,
                            ioc_id=planned.ioc_id,
                            tool_name=planned.tool_name,
                            status=ToolInvocationStatus.PENDING,
                            reason=BootstrapReason.EVIDENCE_UNAVAILABLE,
                        )
                    )
                    continue
                arguments["evidence_refs"] = [str(item) for item in refs[:32]]
                operation_key = bootstrap_tool_operation_key(
                    investigation_id=context.investigation.id,
                    tool_name=planned.tool_name,
                    arguments=arguments,
                    purpose=purpose,
                )
            try:
                invocation, result, _refs, _external = await executor.execute_tool_call(
                    investigation=context.investigation,
                    envelope=envelope,
                    budget=envelope.budget,
                    state=state,
                    tool_name=planned.tool_name,
                    schema_version=planned.schema_version,
                    arguments=arguments,
                    purpose=purpose,
                    operation_key=operation_key,
                    actor_id=actor_id,
                )
            except Exception as exc:
                outcomes.append(
                    _OperationOutcome(
                        operation_key=operation_key,
                        ioc_id=planned.ioc_id,
                        tool_name=planned.tool_name,
                        status=ToolInvocationStatus.FAILED,
                        reason=BootstrapReason.INVOCATION_FAILED,
                        error_code=type(exc).__name__,
                    )
                )
                stopped_reason = BootstrapReason.INVOCATION_FAILED
                continue
            outcome = self._outcome_from_result(
                operation_key=operation_key,
                ioc_id=planned.ioc_id,
                tool_name=planned.tool_name,
                planned=planned,
                invocation=invocation,
                result=result,
            )
            outcomes.append(outcome)
            if outcome.status in {
                ToolInvocationStatus.SUCCEEDED,
                ToolInvocationStatus.PARTIAL,
            }:
                # A partial read still archived its pages, so its evidence can
                # ground the separately authorised download of the same file.
                batch_refs.setdefault(planned.ioc_id, []).extend(outcome.observation_refs)
            raw_reason = result.output.get("reason")
            if raw_reason == "quota":
                stopped_reason = BootstrapReason.QUOTA
            elif raw_reason == "provider_authorization_refused":
                stopped_reason = BootstrapReason.PROVIDER_DENIED
            elif outcome.status is ToolInvocationStatus.POLICY_DENIED:
                stopped_reason = BootstrapReason.POLICY_DENIED
        return tuple(outcomes), stopped_reason

    @staticmethod
    def _planned_outcome(planned: BootstrapPlannedOperationV1) -> _OperationOutcome:
        return _OperationOutcome(
            operation_key=planned.operation_key,
            ioc_id=planned.ioc_id,
            tool_name=planned.tool_name,
            status=planned.status,
            reason=planned.reason,
        )

    def _outcome_from_result(
        self,
        *,
        operation_key: str,
        ioc_id: str,
        tool_name: str,
        planned: BootstrapPlannedOperationV1,
        invocation: ToolInvocation,
        result: ToolResult,
    ) -> _OperationOutcome:
        reason = _reason_from_result(tool_name, result)
        limitations = _bounded_strings(result.output.get("limitations"), max_items=8)
        merged_limitations = tuple(dict.fromkeys((*planned.limitations, *limitations)))[:8]
        return _OperationOutcome(
            operation_key=operation_key,
            ioc_id=ioc_id,
            tool_name=tool_name,
            status=result.status,
            reason=reason,
            invocation_id=invocation.id,
            idempotent_replay=result.idempotent_replay,
            observation_refs=result.observation_refs,
            output=dict(result.output),
            budget_consumed={
                dimension.value: amount for dimension, amount in result.budget_consumed.items()
            },
            error_code=result.error_code,
            limitations=merged_limitations,
        )

    def _snapshot_from_plan(
        self,
        context: _BootstrapContext,
        plan: BootstrapPlanV1,
        *,
        snapshot_id: UUID,
        generated_at: datetime,
        outcomes: tuple[_OperationOutcome, ...],
        stopped_reason: BootstrapReason | None,
    ) -> BootstrapSnapshotV1:
        by_ioc: dict[str, list[_OperationOutcome]] = {}
        for outcome in outcomes:
            by_ioc.setdefault(outcome.ioc_id, []).append(outcome)
        iocs: list[BootstrapSnapshotIocV1] = []
        recorded = 0
        truncated = False
        for ioc in context.inventory.seeds:
            if recorded >= MAX_OPERATIONS:
                truncated = True
                break
            ioc_outcomes = tuple(by_ioc.get(ioc.ioc_id, ()))
            operations = tuple(
                self._snapshot_operation(outcome)
                for outcome in ioc_outcomes
                if recorded < MAX_OPERATIONS
            )
            recorded += len(operations)
            status, reason = _rollup_status(
                tuple(item.status for item in ioc_outcomes),
                tuple(item.reason for item in ioc_outcomes),
                awaiting_g2=plan.state is BootstrapPlanState.AWAITING_G2,
            )
            download = _download_outcome(ioc, ioc_outcomes)
            if download is not None and download.status in {"downloaded", "reused"}:
                reason = (
                    BootstrapReason.REUSED
                    if download.status == "reused"
                    else BootstrapReason.DOWNLOADED
                )
            iocs.append(
                BootstrapSnapshotIocV1(
                    ioc_id=ioc.ioc_id,
                    kind=ioc.kind,
                    value=ioc.value,
                    hash_type=ioc.hash_type,
                    origin=ioc.origin,
                    status=status,
                    reason=reason,
                    operations=operations,
                    download=download,
                    association_ids=tuple(item.association_id for item in ioc.associations),
                    quarantined_sample_ids=tuple(item.sample_id for item in ioc.quarantined),
                )
            )
        limits = BootstrapSnapshotLimitsV1(
            max_iocs=MAX_INVENTORY_IOCS,
            iocs_included=len(iocs),
            inventory_truncated=context.inventory.limits.truncated,
            max_operations=MAX_OPERATIONS,
            operations_recorded=recorded,
            operations_truncated=truncated,
            stopped_reason=stopped_reason,
        )
        return BootstrapSnapshotV1(
            snapshot_id=snapshot_id,
            investigation_id=context.investigation.id,
            pinned_synthesis_sha256=context.inventory.pinned_synthesis_sha256,
            workspace_id=context.inventory.workspace_binding_id,
            generated_at=generated_at,
            plan_state=plan.state,
            envelope_version=plan.envelope_version,
            inventory_sha256=_model_sha256(context.inventory),
            plan_sha256=_model_sha256(plan),
            iocs=tuple(iocs),
            coverage=plan.coverage,
            limits=limits,
            limitations=plan.limitations,
        )

    @staticmethod
    def _snapshot_operation(outcome: _OperationOutcome) -> BootstrapSnapshotOperationV1:
        output = outcome.output
        return BootstrapSnapshotOperationV1(
            operation_key=outcome.operation_key,
            ioc_id=outcome.ioc_id,
            tool_name=outcome.tool_name,
            status=outcome.status,
            reason=outcome.reason,
            invocation_id=outcome.invocation_id,
            idempotent_replay=outcome.idempotent_replay,
            observation_refs=outcome.observation_refs,
            budget_consumed=dict(outcome.budget_consumed),
            pages=_bounded_int(output.get("pages")),
            observed_count=_bounded_int(output.get("observed_count")),
            pagination_complete=_bounded_bool(output.get("pagination_complete")),
            next_cursor=_bounded_text(output.get("next_cursor"), 2_048),
            provider_routes=_bounded_strings(output.get("provider_routes"), max_items=8),
            detail_code=_bounded_text(output.get("detail_code"), 128),
            error_code=outcome.error_code[:128] if outcome.error_code else None,
            limitations=outcome.limitations,
        )

    async def _persist_snapshot(
        self, uow: BootstrapUnitOfWork, snapshot: BootstrapSnapshotV1, idempotency_key: str
    ) -> None:
        recorder = ProductionEvidenceRecorder(self._artifact_store, uow.evidence_observations)
        await recorder.record_json(
            investigation_id=snapshot.investigation_id,
            kind=EvidenceKind.OTHER,
            payload=snapshot.model_dump(mode="json"),
            source=BOOTSTRAP_SOURCE,
            source_version=BOOTSTRAP_SOURCE_VERSION,
            object_type=BOOTSTRAP_SNAPSHOT_OBJECT_TYPE,
            object_id=str(snapshot.snapshot_id),
            limitations=snapshot.limitations,
            observed_at=snapshot.generated_at,
            idempotency_key=f"bootstrap-snapshot:{snapshot.investigation_id}:{idempotency_key}",
        )
        await uow.commit()

    async def _find_snapshot(
        self, uow: BootstrapUnitOfWork, investigation_id: UUID, snapshot_id: UUID
    ) -> BootstrapSnapshotV1 | None:
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        candidate = next(
            (
                item
                for item in observations
                if item.source == BOOTSTRAP_SOURCE
                and item.source_version == BOOTSTRAP_SOURCE_VERSION
                and item.object_type == BOOTSTRAP_SNAPSHOT_OBJECT_TYPE
                and item.object_id == str(snapshot_id)
            ),
            None,
        )
        if candidate is None:
            return None
        payload = await self._artifact_store.read_json(candidate.blob_id)
        encoded = ProductionArtifactStore.canonical_json_bytes(payload)
        if hashlib.sha256(encoded).hexdigest() != candidate.sha256:
            raise BootstrapSnapshotError("Snapshot blob does not match its evidence SHA-256")
        return BootstrapSnapshotV1.model_validate_json(encoded)

    @staticmethod
    async def _prior_file_report_refs(
        uow: BootstrapUnitOfWork, investigation_id: UUID
    ) -> Mapping[str, tuple[UUID, ...]]:
        observations = await uow.evidence_observations.list_for_investigation(investigation_id)
        refs: dict[str, list[UUID]] = {}
        for observation in observations:
            if (
                observation.kind is EvidenceKind.VT_FILE_REPORT
                and observation.source == "VirusTotal"
                and observation.object_type == "file"
            ):
                refs.setdefault(observation.object_id, []).append(observation.id)
        return {key: tuple(sorted(values, key=str)) for key, values in refs.items()}

    @staticmethod
    def _snapshot_id(investigation_id: UUID, idempotency_key: str) -> UUID:
        return uuid5(_BOOTSTRAP_NAMESPACE, f"snapshot:{investigation_id}:{idempotency_key}")

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Bootstrap clock must return timezone-aware datetimes")
        return value

    # --------------------------------------------------------------- export

    def snapshot_export(self, snapshot: BootstrapSnapshotV1) -> BootstrapSnapshotExportV1:
        """Build the ``vt/`` manifest files; never writes to the host workspace."""
        documents: list[tuple[str, dict[str, Any]]] = [
            ("vt/bootstrap-snapshot.json", snapshot.model_dump(mode="json"))
        ]
        behaviour_files: list[tuple[str, dict[str, Any]]] = []
        relationship_files: list[tuple[str, dict[str, Any]]] = []
        for ioc in snapshot.iocs:
            name = _export_name(ioc)
            location = _export_directory(ioc.kind)
            documents.append((f"vt/{location}/{name}.json", ioc.model_dump(mode="json")))
            behaviour_ops = [item for item in ioc.operations if item.tool_name == "vt.behaviour"]
            if behaviour_ops:
                behaviour_files.append(
                    (
                        f"vt/behaviours/{name}.json",
                        {
                            "schema": "vt-bootstrap-behaviour.v1",
                            "ioc_id": ioc.ioc_id,
                            "operations": [item.model_dump(mode="json") for item in behaviour_ops],
                        },
                    )
                )
            relationship_ops = [
                item for item in ioc.operations if item.tool_name == "vt.relationships"
            ]
            if relationship_ops:
                relationship_files.append(
                    (
                        f"vt/relationships/{name}.json",
                        {
                            "schema": "vt-bootstrap-relationships.v1",
                            "ioc_id": ioc.ioc_id,
                            "operations": [
                                item.model_dump(mode="json") for item in relationship_ops
                            ],
                        },
                    )
                )
        documents.extend(behaviour_files)
        documents.extend(relationship_files)
        files: list[BootstrapExportFileV1] = []
        manifest_entries: list[dict[str, Any]] = []
        for path, payload in documents:
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            if _EXPORT_FORBIDDEN.search(encoded) is not None:
                raise BootstrapExportRefusedError(
                    "The bootstrap export contains credential-like material"
                )
            digest = hashlib.sha256(encoded.encode()).hexdigest()
            size = len(encoded.encode())
            files.append(
                BootstrapExportFileV1(path=path, sha256=digest, size_bytes=size, content=encoded)
            )
            manifest_entries.append({"path": path, "sha256": digest, "size_bytes": size})
        manifest = {
            "schema": "vt-workspace-export-manifest.v1",
            "snapshot_id": str(snapshot.snapshot_id),
            "investigation_id": str(snapshot.investigation_id),
            "generated_at": snapshot.generated_at.isoformat(),
            "inventory_sha256": snapshot.inventory_sha256,
            "files": manifest_entries,
        }
        encoded_manifest = json.dumps(
            manifest,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        files.append(
            BootstrapExportFileV1(
                path="vt/manifest.json",
                sha256=hashlib.sha256(encoded_manifest.encode()).hexdigest(),
                size_bytes=len(encoded_manifest.encode()),
                content=encoded_manifest,
            )
        )
        return BootstrapSnapshotExportV1(snapshot_id=snapshot.snapshot_id, files=tuple(files))


def _reason_from_result(tool_name: str, result: ToolResult) -> BootstrapReason | None:
    raw_reason = result.output.get("reason")
    reason = raw_reason if isinstance(raw_reason, str) else None
    # ``execute_tool_call`` rewrites an unproven external adapter failure to
    # NEEDS_REVIEW; the provider-typed reason stays in the output and is the
    # authoritative signal for the snapshot vocabulary.
    if result.status is not ToolInvocationStatus.SUCCEEDED and reason == "quota":
        return BootstrapReason.QUOTA
    if (
        result.status is not ToolInvocationStatus.SUCCEEDED
        and reason == "provider_authorization_refused"
    ):
        return BootstrapReason.PROVIDER_DENIED
    if result.status is ToolInvocationStatus.SUCCEEDED:
        if tool_name != "vt.download_sample":
            return BootstrapReason.AVAILABLE
        reused = (
            result.output.get("reused_existing_sample") is True
            or result.output.get("cached") is True
            or result.idempotent_replay
        )
        return BootstrapReason.REUSED if reused else BootstrapReason.DOWNLOADED
    if result.status is ToolInvocationStatus.PARTIAL:
        return BootstrapReason.PARTIAL
    if result.status is ToolInvocationStatus.EMPTY:
        return BootstrapReason.NOT_FOUND if reason == "not_found" else BootstrapReason.EMPTY
    if result.status is ToolInvocationStatus.NEEDS_REVIEW:
        if result.error_code == "budget_exceeded" or reason == "budget_exceeded":
            return BootstrapReason.BUDGET_EXCEEDED
        return BootstrapReason.NEEDS_REVIEW
    if result.status is ToolInvocationStatus.POLICY_DENIED:
        return BootstrapReason.POLICY_DENIED
    if result.status is ToolInvocationStatus.UNAVAILABLE:
        if result.framework_terminal_external_effect_failure is not None:
            return BootstrapReason.NOT_FOUND
        if reason == "quota":
            return BootstrapReason.QUOTA
        if reason == "provider_authorization_refused":
            return BootstrapReason.PROVIDER_DENIED
        if result.error_code and "route" in result.error_code:
            return BootstrapReason.ROUTE_UNAVAILABLE
        return BootstrapReason.CAPABILITY_ABSENT
    if result.status is ToolInvocationStatus.FAILED:
        if tool_name == "vt.download_sample" and result.error_code in {
            "virustotal_download_route_not_found",
            "provider_download_route_not_found",
        }:
            # The acquisition adapter attributes a proxy route 404 to the route,
            # not to the file: a broken route is not proof the sample is absent.
            return BootstrapReason.ROUTE_UNAVAILABLE
        if tool_name == "vt.download_sample" and result.error_code in {
            "virustotal_download_file_not_found",
            "provider_file_not_found",
        }:
            return BootstrapReason.NOT_FOUND
        return BootstrapReason.FAILED
    return BootstrapReason.PENDING


def _rollup_status(
    statuses: tuple[ToolInvocationStatus, ...],
    reasons: tuple[BootstrapReason | None, ...],
    *,
    awaiting_g2: bool,
) -> tuple[ToolInvocationStatus, BootstrapReason | None]:
    if not statuses:
        return (
            ToolInvocationStatus.PENDING,
            BootstrapReason.AWAITING_G2 if awaiting_g2 else BootstrapReason.NOT_ATTEMPTED,
        )
    if awaiting_g2:
        return ToolInvocationStatus.PENDING, BootstrapReason.AWAITING_G2
    if any(status is ToolInvocationStatus.PARTIAL for status in statuses):
        return ToolInvocationStatus.PARTIAL, BootstrapReason.PARTIAL
    if any(status is ToolInvocationStatus.SUCCEEDED for status in statuses):
        return ToolInvocationStatus.SUCCEEDED, _first_reason(
            statuses, reasons, ToolInvocationStatus.SUCCEEDED
        )
    if any(status is ToolInvocationStatus.NEEDS_REVIEW for status in statuses):
        return ToolInvocationStatus.NEEDS_REVIEW, BootstrapReason.NEEDS_REVIEW
    if any(status is ToolInvocationStatus.EMPTY for status in statuses):
        return ToolInvocationStatus.EMPTY, BootstrapReason.NOT_FOUND
    if any(status is ToolInvocationStatus.UNAVAILABLE for status in statuses):
        reason = next(
            (
                item
                for item in reasons
                if item
                in {
                    BootstrapReason.QUOTA,
                    BootstrapReason.PROVIDER_DENIED,
                    BootstrapReason.ROUTE_UNAVAILABLE,
                    BootstrapReason.CAPABILITY_ABSENT,
                }
            ),
            BootstrapReason.CAPABILITY_ABSENT,
        )
        return ToolInvocationStatus.UNAVAILABLE, reason
    if any(status is ToolInvocationStatus.POLICY_DENIED for status in statuses):
        return ToolInvocationStatus.POLICY_DENIED, BootstrapReason.POLICY_DENIED
    if any(status is ToolInvocationStatus.FAILED for status in statuses):
        return ToolInvocationStatus.FAILED, BootstrapReason.FAILED
    return ToolInvocationStatus.PENDING, BootstrapReason.STOPPED


def _first_reason(
    statuses: tuple[ToolInvocationStatus, ...],
    reasons: tuple[BootstrapReason | None, ...],
    status: ToolInvocationStatus,
) -> BootstrapReason | None:
    return next(
        (
            reason
            for item, reason in zip(statuses, reasons, strict=True)
            if item is status and reason is not None
        ),
        None,
    )


def _download_outcome(
    ioc: BootstrapIocV1, outcomes: Sequence[_OperationOutcome]
) -> BootstrapDownloadOutcomeV1 | None:
    download = next((item for item in outcomes if item.tool_name == "vt.download_sample"), None)
    if download is None:
        if ioc.quarantined:
            quarantined = ioc.quarantined[0]
            return BootstrapDownloadOutcomeV1(
                status="reused",
                association_id=quarantined.association_id,
                sample_id=quarantined.sample_id,
                sample_sha256=quarantined.sha256,
                qualification=quarantined.qualification,
                hash_verified_quarantine=True,
                reason="already_quarantined",
            )
        return None
    output = download.output
    if download.status is ToolInvocationStatus.SUCCEEDED:
        reused = (
            output.get("reused_existing_sample") is True
            or output.get("cached") is True
            or download.idempotent_replay
        )
        sha256 = output.get("sample_sha256")
        return BootstrapDownloadOutcomeV1(
            status="reused" if reused else "downloaded",
            association_id=_optional_uuid(output.get("association_id")),
            sample_id=_optional_uuid(output.get("sample_id")),
            sample_sha256=sha256 if isinstance(sha256, str) else None,
            qualification=_optional_qualification(output.get("qualification")),
            hash_verified_quarantine=True,
            reason=download.reason.value if download.reason is not None else None,
        )
    if download.status is ToolInvocationStatus.PENDING:
        return BootstrapDownloadOutcomeV1(
            status="not_attempted",
            reason=download.reason.value if download.reason is not None else None,
        )
    return BootstrapDownloadOutcomeV1(
        status="failed",
        reason=download.reason.value if download.reason is not None else None,
    )


def _optional_uuid(value: object) -> UUID | None:
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError:
            return None
    return None


def _optional_qualification(value: object) -> SampleQualification | None:
    if isinstance(value, SampleQualification):
        return value
    if isinstance(value, str):
        try:
            return SampleQualification(value)
        except ValueError:
            return None
    return None


def _bounded_text(value: object, max_chars: int) -> str | None:
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    return stripped[:max_chars] if stripped else None


def _bounded_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def _bounded_bool(value: object) -> bool | None:
    return value if isinstance(value, bool) else None


def _bounded_strings(value: object, *, max_items: int) -> tuple[str, ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(
        item.strip()[:MAX_LIMITATION_CHARS]
        for item in value[:max_items]
        if isinstance(item, str) and item.strip()
    )


def _export_directory(kind: BootstrapIocKind) -> str:
    return {
        BootstrapIocKind.SAMPLE: "files",
        BootstrapIocKind.IPV4: "ips",
        BootstrapIocKind.DOMAIN: "domains",
        BootstrapIocKind.URL: "urls",
    }[kind]


def _export_name(ioc: BootstrapSnapshotIocV1) -> str:
    if ioc.kind is BootstrapIocKind.SAMPLE:
        return ioc.value
    component = _SAFE_COMPONENT.sub("-", ioc.value.casefold()).strip("-._")[:48] or "ioc"
    return f"{component}-{ioc.ioc_id[4:12]}"


def _validate_relationship_allowlists() -> None:
    for kind, relations in _RELATIONS_BY_KIND.items():
        allowed = VIRUSTOTAL_RELATIONSHIPS_BY_OBJECT_TYPE[_VT_OBJECT_TYPE_BY_KIND[kind]]
        if not set(relations) <= allowed:
            raise RuntimeError("A bootstrap default relationship is outside the VT allowlist")


_validate_relationship_allowlists()

__all__ = [
    "BOOTSTRAP_SNAPSHOT_OBJECT_TYPE",
    "BOOTSTRAP_SOURCE",
    "BOOTSTRAP_SOURCE_VERSION",
    "EXPORT_SCHEMA_VERSION",
    "INVENTORY_SCHEMA_VERSION",
    "MAX_INVENTORY_IOCS",
    "MAX_OPERATIONS",
    "PLAN_SCHEMA_VERSION",
    "SNAPSHOT_SCHEMA_VERSION",
    "BootstrapArticleKindError",
    "BootstrapCoverageEntryV1",
    "BootstrapCoverageKind",
    "BootstrapDownloadOutcomeV1",
    "BootstrapError",
    "BootstrapExportFileV1",
    "BootstrapExportRefusedError",
    "BootstrapHashAliasV1",
    "BootstrapInventoryLimitsV1",
    "BootstrapInvestigationNotFoundError",
    "BootstrapIocKind",
    "BootstrapIocOrigin",
    "BootstrapIocSourceV1",
    "BootstrapIocV1",
    "BootstrapPlanLimitsV1",
    "BootstrapPlanState",
    "BootstrapPlanV1",
    "BootstrapPlannedOperationV1",
    "BootstrapQuarantinedSampleV1",
    "BootstrapReason",
    "BootstrapRouteStatus",
    "BootstrapSampleAssociationV1",
    "BootstrapSnapshotError",
    "BootstrapSnapshotExportV1",
    "BootstrapSnapshotIocV1",
    "BootstrapSnapshotLimitsV1",
    "BootstrapSnapshotOperationV1",
    "BootstrapSnapshotV1",
    "BootstrapStateUnavailableError",
    "BootstrapSynthesisPinError",
    "BootstrapToolExecutor",
    "BootstrapUnitOfWork",
    "BootstrapVtCapabilities",
    "IOCInventoryV1",
    "InvestigationBootstrapService",
    "bootstrap_tool_operation_key",
]
