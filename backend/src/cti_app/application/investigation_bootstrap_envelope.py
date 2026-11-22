"""Initial bounded G2 envelope proposal for an ARTICLE VT bootstrap (M24-03B).

The module turns the deterministic M24-03A inventory/plan into one typed
proposal that a verified human approves once.  It never approves on its own,
never contacts a provider and never widens the persisted disclosure policy.

Five authority dimensions are reported separately: VT reads, sample
acquisition (count and byte volume), costs/quotas, external model use and
disclosure bounds.  Approval delegates to the existing
``InvestigationEnvelopeService`` so the G2 decision audit, CAS version and
revocation semantics stay canonical: operations outside the approved envelope
still refuse, and an extension remains a separate human decision.
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from types import TracebackType
from typing import Literal, Protocol, Self, cast
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.external_effects import AdvisorDisclosurePolicy, advisor_disclosure_policy
from cti_app.application.investigation_bootstrap import (
    BootstrapError,
    BootstrapInvestigationNotFoundError,
    BootstrapPlannedOperationV1,
    BootstrapPlanState,
    BootstrapRouteStatus,
    InvestigationBootstrapService,
    IOCInventoryV1,
)
from cti_app.application.investigation_governance import (
    InvestigationEnvelopeProposal,
    InvestigationEnvelopeService,
)
from cti_app.application.investigation_persistence import InvestigationEnvelopeRepository
from cti_app.application.persistence import AnalystInvestigationRepository, UnitOfWorkFactory
from cti_app.application.research_context_builder import ArticleSnapshot, ArticleSnapshotPort
from cti_app.config import Settings
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetExceeded,
    DisclosureBounds,
    InvestigationBudget,
    TLPLevel,
    canonical_json_bytes,
)

PROPOSAL_SCHEMA_VERSION: Literal["vt-bootstrap-envelope-proposal.v1"] = (
    "vt-bootstrap-envelope-proposal.v1"
)
APPROVAL_SCHEMA_VERSION: Literal["vt-bootstrap-envelope-approval.v1"] = (
    "vt-bootstrap-envelope-approval.v1"
)

MAX_SUMMARY_CHARS = 500
MAX_SECTION_LIMITATIONS = 8
MAX_PROPOSAL_LIMITATIONS = 16
MAX_PROPOSAL_IOC_IDS = 256

_READ_TOOLS = (
    "vt.file_report",
    "vt.object_report",
    "vt.relationships",
    "vt.behaviour",
)
_ReadToolName = Literal[
    "vt.file_report",
    "vt.object_report",
    "vt.relationships",
    "vt.behaviour",
]
_DOWNLOAD_TOOL = "vt.download_sample"
# The VT adapter itself refuses AMBER and RED envelopes (investigation_vt_tools).
_VT_ALLOWED_TLP = frozenset({TLPLevel.WHITE, TLPLevel.CLEAR, TLPLevel.GREEN})
_VT_LABEL_FAMILY = "vt.file_report"
_BRIDGE_LABEL_FAMILY = "advisor.consult"


class BootstrapEnvelopeError(BootstrapError):
    """Typed local refusal for the initial envelope contract."""

    code = "bootstrap_envelope_error"


class BootstrapEnvelopeProposalStaleError(BootstrapEnvelopeError):
    """The approved proposal identity no longer matches the current inventory."""

    code = "bootstrap_proposal_stale"
    status_code = 409


class _StrictModel(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True)


class BootstrapToolOperationCountV1(_StrictModel):
    tool_name: Literal[
        "vt.file_report",
        "vt.object_report",
        "vt.relationships",
        "vt.behaviour",
    ]
    planned: int = Field(ge=0)
    covered: int = Field(ge=0)


class BootstrapVtReadsProposalV1(_StrictModel):
    """VT passive reads only; sample downloads belong to their own dimension."""

    allowed: bool = False
    provider: Literal["vt"] = "vt"
    operations_planned: int = Field(ge=0)
    operations_covered: int = Field(ge=0)
    units_planned: int = Field(ge=0)
    units_proposed: int = Field(ge=0)
    iocs_planned: int = Field(ge=0)
    iocs_covered: int = Field(ge=0)
    per_tool: tuple[BootstrapToolOperationCountV1, ...] = ()
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_SECTION_LIMITATIONS)


class BootstrapSampleAcquisitionProposalV1(_StrictModel):
    """Bounded hash-verified acquisition after G2; never a G3 membership claim."""

    allowed: bool = False
    acquire_hits_allowed: bool = False
    sample_count_planned: int = Field(ge=0)
    sample_count_covered: int = Field(ge=0)
    downloads_limit: int = Field(ge=0)
    per_sample_max_bytes: int = Field(ge=0)
    total_max_bytes: int = Field(ge=0)
    route_status: BootstrapRouteStatus = BootstrapRouteStatus.UNAVAILABLE
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_SECTION_LIMITATIONS)


class BootstrapBudgetLimitSource(StrEnum):
    INVENTORY = "inventory"
    SETTINGS_CAP = "settings_cap"
    SETTINGS_DEFAULT = "settings_default"


class BootstrapBudgetLimitV1(_StrictModel):
    dimension: BudgetDimension
    limit: int = Field(ge=0)
    source: BootstrapBudgetLimitSource


class BootstrapSettingsCapsV1(_StrictModel):
    """The configuration ceilings the proposal was computed under."""

    vt_units: int = Field(ge=0)
    downloads: int = Field(ge=0)
    per_sample_bytes: int = Field(ge=0)
    tool_seconds: int = Field(ge=1)
    context_bytes: int = Field(ge=1)
    context_tokens: int = Field(ge=1)


class BootstrapCostsProposalV1(_StrictModel):
    limits: tuple[BootstrapBudgetLimitV1, ...] = ()
    caps: BootstrapSettingsCapsV1
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)


class BootstrapExternalModelProposalV1(_StrictModel):
    """External model use mirrors the persisted policy; it can never widen it."""

    allowed: bool = False
    providers: tuple[str, ...] = ()
    policy_source: Literal["article_policy"] = "article_policy"
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_SECTION_LIMITATIONS)


class BootstrapDisclosureScope(StrEnum):
    ARTICLE_CITED_IOCS = "article_cited_iocs"


class BootstrapDisclosureProposalV1(_StrictModel):
    tlp: TLPLevel | None = None
    do_not_submit: bool = True
    external_llm_allowed: bool = False
    scope: BootstrapDisclosureScope = BootstrapDisclosureScope.ARTICLE_CITED_IOCS
    scope_statement: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    summary: str = Field(min_length=1, max_length=MAX_SUMMARY_CHARS)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_SECTION_LIMITATIONS)


class BootstrapInventoryCountsV1(_StrictModel):
    seeds: int = Field(ge=0)
    discovered: int = Field(ge=0)
    samples: int = Field(ge=0)
    ipv4: int = Field(ge=0)
    domains: int = Field(ge=0)
    urls: int = Field(ge=0)
    truncated: bool = False


class BootstrapEnvelopeProposalV1(_StrictModel):
    """One unapproved, bounded envelope proposal with its exact coverage."""

    schema_version: Literal["vt-bootstrap-envelope-proposal.v1"] = PROPOSAL_SCHEMA_VERSION
    generated_at: datetime
    investigation_id: UUID
    proposal_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    approved: Literal[False] = False
    already_approved: bool = False
    envelope_version: int | None = Field(default=None, ge=1)
    plan_state: BootstrapPlanState
    allowed_providers: tuple[str, ...] = ()
    acquire_hits_allowed: bool = False
    inventory: BootstrapInventoryCountsV1
    planned_operations: int = Field(ge=0)
    covered_operations: int = Field(ge=0)
    covered_ioc_ids: tuple[str, ...] = Field(default=(), max_length=MAX_PROPOSAL_IOC_IDS)
    vt_reads: BootstrapVtReadsProposalV1
    sample_acquisition: BootstrapSampleAcquisitionProposalV1
    costs: BootstrapCostsProposalV1
    external_model: BootstrapExternalModelProposalV1
    disclosure: BootstrapDisclosureProposalV1
    summary: tuple[str, ...] = Field(default=(), max_length=8)
    limitations: tuple[str, ...] = Field(default=(), max_length=MAX_PROPOSAL_LIMITATIONS)

    def to_service_proposal(self) -> InvestigationEnvelopeProposal:
        """Project the displayed bounds onto the canonical G2 proposal type."""
        return InvestigationEnvelopeProposal(
            investigation_id=self.investigation_id,
            allowed_providers=self.allowed_providers,
            budget=InvestigationBudget(
                limits={item.dimension: item.limit for item in self.costs.limits}
            ),
            acquire_hits_allowed=self.acquire_hits_allowed,
            disclosure=DisclosureBounds(
                tlp=self.disclosure.tlp,
                do_not_submit=self.disclosure.do_not_submit,
                external_llm_allowed=self.disclosure.external_llm_allowed,
            ),
        )


class BootstrapEnvelopeApprovalV1(_StrictModel):
    """The envelope actually persisted by the canonical G2 service."""

    schema_version: Literal["vt-bootstrap-envelope-approval.v1"] = APPROVAL_SCHEMA_VERSION
    approved: Literal[True] = True
    investigation_id: UUID
    proposal: BootstrapEnvelopeProposalV1
    envelope_version: int = Field(ge=1)
    approval_decision_id: UUID
    approval_actor_id: str = Field(min_length=1)
    approved_at: datetime
    allowed_providers: tuple[str, ...]
    acquire_hits_allowed: bool
    budget_limits: dict[str, int]
    disclosure_tlp: TLPLevel | None = None
    disclosure_do_not_submit: bool
    disclosure_external_llm_allowed: bool


class BootstrapEnvelopeUnitOfWork(Protocol):
    """The canonical stores read by the proposal and approval path."""

    analyst_investigations: AnalystInvestigationRepository
    investigation_envelopes: InvestigationEnvelopeRepository

    async def __aenter__(self) -> Self: ...

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _AuthorityCaps:
    vt_units: int
    downloads: int
    per_sample_bytes: int
    tool_seconds: int
    context_bytes: int
    context_tokens: int


@dataclass(frozen=True, slots=True)
class _PolicyView:
    tlp: TLPLevel | None
    tlp_parseable: bool
    do_not_submit: bool
    external_llm_allowed: bool
    vt_allowed: bool
    bridge_allowed: bool


@dataclass(frozen=True, slots=True)
class _Coverage:
    covered: tuple[BootstrapPlannedOperationV1, ...]


def _settings_caps(settings: Settings) -> _AuthorityCaps:
    context_bytes = max(1, int(settings.investigation_max_context_bytes))
    return _AuthorityCaps(
        vt_units=max(0, int(settings.investigation_max_vt_read_units)),
        downloads=max(0, int(settings.investigation_max_hits_acquired)),
        per_sample_bytes=max(0, int(settings.virustotal_download_max_bytes)),
        tool_seconds=max(1, int(settings.investigation_max_tool_seconds)),
        context_bytes=context_bytes,
        context_tokens=max(1, context_bytes // 4),
    )


def _parse_tlp(value: object) -> tuple[TLPLevel | None, bool]:
    if not isinstance(value, str) or not value.strip():
        return None, False
    normalized = value.strip().upper().removeprefix("TLP:")
    if normalized == "AMBER+STRICT":
        normalized = "AMBER"
    try:
        return TLPLevel(normalized.lower()), True
    except ValueError:
        return None, False


def _policy_view(
    article: ArticleSnapshot, disclosure_policy: AdvisorDisclosurePolicy
) -> _PolicyView:
    tlp, tlp_parseable = _parse_tlp(article.tlp)
    external_llm_allowed = bool(article.external_llm_allowed)
    vt_labels_ignored = disclosure_policy.ignores_restriction_labels(_VT_LABEL_FAMILY)
    bridge_labels_ignored = disclosure_policy.ignores_restriction_labels(_BRIDGE_LABEL_FAMILY)
    if not external_llm_allowed:
        vt_allowed = False
    elif vt_labels_ignored:
        # The VT adapter still refuses AMBER/RED envelopes.
        vt_allowed = tlp not in {TLPLevel.AMBER, TLPLevel.RED}
    else:
        vt_allowed = tlp_parseable and tlp in _VT_ALLOWED_TLP
    if not external_llm_allowed:
        bridge_allowed = False
    elif bridge_labels_ignored:
        bridge_allowed = True
    else:
        bridge_allowed = tlp_parseable and tlp is not TLPLevel.RED
    return _PolicyView(
        tlp=tlp,
        tlp_parseable=tlp_parseable,
        do_not_submit=bool(article.do_not_submit),
        external_llm_allowed=external_llm_allowed,
        vt_allowed=vt_allowed,
        bridge_allowed=bridge_allowed,
    )


def _covered_operations(
    operations: Sequence[BootstrapPlannedOperationV1],
    *,
    caps: _AuthorityCaps,
    vt_allowed: bool,
    acquisition_allowed: bool,
) -> _Coverage:
    """Replay the 03A admission order within the proposed finite caps."""
    if not vt_allowed:
        return _Coverage(covered=())
    budget = InvestigationBudget(
        limits={
            BudgetDimension.VT_UNITS: caps.vt_units,
            BudgetDimension.DOWNLOADS: caps.downloads,
        }
    )
    covered: list[BootstrapPlannedOperationV1] = []
    covered_reads_by_ioc: dict[str, int] = {}
    for operation in operations:
        amounts: dict[BudgetDimension, int]
        if operation.tool_name == _DOWNLOAD_TOOL:
            if not acquisition_allowed:
                continue
            if (
                operation.requires_batch_evidence
                and covered_reads_by_ioc.get(operation.ioc_id, 0) == 0
            ):
                # 03A needs a covered read (or prior evidence) to ground the
                # hash-verified download; never promise an ungrounded call.
                continue
            amounts = {
                BudgetDimension.VT_UNITS: operation.reserved_vt_units,
                BudgetDimension.DOWNLOADS: operation.reserved_downloads,
                BudgetDimension.TOOL_SECONDS: 120,
            }
        elif operation.tool_name in _READ_TOOLS:
            amounts = {
                BudgetDimension.VT_UNITS: operation.reserved_vt_units,
                BudgetDimension.TOOL_SECONDS: 120,
            }
        else:
            continue
        try:
            budget.reserve(operation.operation_key, amounts)
        except BudgetExceeded:
            continue
        covered.append(operation)
        if operation.tool_name in _READ_TOOLS:
            covered_reads_by_ioc[operation.ioc_id] = (
                covered_reads_by_ioc.get(operation.ioc_id, 0) + 1
            )
    return _Coverage(covered=tuple(covered))


def _inventory_counts(inventory: IOCInventoryV1) -> BootstrapInventoryCountsV1:
    counts = {name: 0 for name in ("sample", "ipv4", "domain", "url")}
    for ioc in inventory.seeds:
        counts[ioc.kind.value] = counts.get(ioc.kind.value, 0) + 1
    return BootstrapInventoryCountsV1(
        seeds=len(inventory.seeds),
        discovered=len(inventory.discovered),
        samples=counts["sample"],
        ipv4=counts["ipv4"],
        domains=counts["domain"],
        urls=counts["url"],
        truncated=inventory.limits.truncated,
    )


def _inventory_identity(inventory: IOCInventoryV1) -> list[dict[str, object]]:
    """Stable inventory content identity: excludes the per-call generation date."""
    return [
        {
            "ioc_id": ioc.ioc_id,
            "kind": ioc.kind.value,
            "origin": ioc.origin.value,
            "value": ioc.value,
            "hash_type": ioc.hash_type,
            "hashes": [
                {"hash_type": alias.hash_type, "value": alias.value} for alias in ioc.hashes
            ],
        }
        for ioc in inventory.seeds
    ]


def _limit_source(covered: int, planned: int) -> BootstrapBudgetLimitSource:
    return (
        BootstrapBudgetLimitSource.INVENTORY
        if covered == planned
        else BootstrapBudgetLimitSource.SETTINGS_CAP
    )


class InvestigationBootstrapEnvelopeService:
    """Compute and approve one bounded initial G2 envelope for a VT bootstrap."""

    def __init__(
        self,
        *,
        uow_factory: Callable[[], BootstrapEnvelopeUnitOfWork],
        bootstrap: InvestigationBootstrapService,
        article_snapshots: ArticleSnapshotPort,
        settings: Settings,
        clock: Callable[[], datetime] | None = None,
        disclosure_policy: AdvisorDisclosurePolicy | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._bootstrap = bootstrap
        self._article_snapshots = article_snapshots
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._disclosure_policy = disclosure_policy or advisor_disclosure_policy(
            settings.advisor_disclosure_profile
        )

    async def propose_for_bootstrap(self, investigation_id: UUID) -> BootstrapEnvelopeProposalV1:
        """Return the local-only proposal; never approves or contacts a provider."""
        plan = await self._bootstrap.plan(investigation_id)
        inventory = await self._bootstrap.inventory(investigation_id)
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise BootstrapInvestigationNotFoundError("Investigation does not exist")
            current = await uow.investigation_envelopes.get(investigation_id)
        article = await self._article_snapshots.resolve(investigation.synthesis_artifact_id)
        caps = _settings_caps(self._settings)
        policy = _policy_view(article, self._disclosure_policy)
        planned_downloads = tuple(
            operation for operation in plan.operations if operation.tool_name == _DOWNLOAD_TOOL
        )
        acquisition_allowed = policy.vt_allowed and caps.downloads > 0 and bool(planned_downloads)
        coverage = _covered_operations(
            plan.operations,
            caps=caps,
            vt_allowed=policy.vt_allowed,
            acquisition_allowed=acquisition_allowed,
        )
        covered = coverage.covered
        covered_reads = tuple(item for item in covered if item.tool_name in _READ_TOOLS)
        covered_downloads = tuple(item for item in covered if item.tool_name == _DOWNLOAD_TOOL)
        planned_reads = tuple(item for item in plan.operations if item.tool_name in _READ_TOOLS)
        planned_read_units = sum(item.reserved_vt_units for item in planned_reads)
        covered_read_units = sum(item.reserved_vt_units for item in covered_reads)
        planned_download_units = sum(item.reserved_vt_units for item in planned_downloads)
        covered_download_units = sum(item.reserved_vt_units for item in covered_downloads)
        planned_download_count = sum(item.reserved_downloads for item in planned_downloads)
        covered_download_count = sum(item.reserved_downloads for item in covered_downloads)
        vt_units_limit = covered_read_units + covered_download_units
        downloads_limit = covered_download_count
        vt_provider = policy.vt_allowed and bool(covered_reads or covered_downloads)
        acquire_hits_allowed = bool(covered_downloads)
        providers: list[str] = []
        if vt_provider:
            providers.append("vt")
        if policy.bridge_allowed:
            providers.append("bridge")
        limits = (
            BootstrapBudgetLimitV1(
                dimension=BudgetDimension.VT_UNITS,
                limit=vt_units_limit,
                source=_limit_source(vt_units_limit, planned_read_units + planned_download_units),
            ),
            BootstrapBudgetLimitV1(
                dimension=BudgetDimension.DOWNLOADS,
                limit=downloads_limit,
                source=_limit_source(downloads_limit, planned_download_count),
            ),
            BootstrapBudgetLimitV1(
                dimension=BudgetDimension.TOOL_SECONDS,
                limit=caps.tool_seconds,
                source=BootstrapBudgetLimitSource.SETTINGS_DEFAULT,
            ),
            BootstrapBudgetLimitV1(
                dimension=BudgetDimension.CONTEXT_BYTES,
                limit=caps.context_bytes,
                source=BootstrapBudgetLimitSource.SETTINGS_DEFAULT,
            ),
            BootstrapBudgetLimitV1(
                dimension=BudgetDimension.CONTEXT_TOKENS,
                limit=caps.context_tokens,
                source=BootstrapBudgetLimitSource.SETTINGS_DEFAULT,
            ),
        )
        budget_limits = {item.dimension: item.limit for item in limits}
        disclosure_limitations = self._disclosure_limitations(policy)
        coverage_limitations = self._coverage_limitations(
            policy=policy,
            caps=caps,
            planned_operations=len(plan.operations),
            covered_operations=len(covered),
            planned_downloads=len(planned_downloads),
            covered_downloads=len(covered_downloads),
            capability_download=bool(planned_downloads),
        )
        proposal_sha256 = self._proposal_sha256(
            investigation_id=investigation_id,
            inventory=inventory,
            caps=caps,
            providers=tuple(providers),
            budget_limits=budget_limits,
            acquire_hits_allowed=acquire_hits_allowed,
            policy=policy,
            planned_operations=plan.operations,
            covered_operations=covered,
        )
        limitations = self._proposal_limitations(
            plan_limitations=plan.limitations,
            inventory_limitations=inventory.limitations,
            disclosure_limitations=disclosure_limitations,
            coverage_limitations=coverage_limitations,
            already_approved=current is not None,
        )
        read_iocs_planned = {item.ioc_id for item in planned_reads}
        read_iocs_covered = {item.ioc_id for item in covered_reads}
        covered_ioc_ids = tuple(sorted({item.ioc_id for item in covered}))[:MAX_PROPOSAL_IOC_IDS]
        sample_count_planned = len({item.ioc_id for item in planned_downloads})
        sample_count_covered = len({item.ioc_id for item in covered_downloads})
        return BootstrapEnvelopeProposalV1(
            generated_at=self._now(),
            investigation_id=investigation_id,
            proposal_sha256=proposal_sha256,
            already_approved=current is not None,
            envelope_version=current.version if current is not None else None,
            plan_state=plan.state,
            allowed_providers=tuple(providers),
            acquire_hits_allowed=acquire_hits_allowed,
            inventory=_inventory_counts(inventory),
            planned_operations=len(plan.operations),
            covered_operations=len(covered),
            covered_ioc_ids=covered_ioc_ids,
            vt_reads=BootstrapVtReadsProposalV1(
                allowed=bool(covered_reads),
                operations_planned=len(planned_reads),
                operations_covered=len(covered_reads),
                units_planned=planned_read_units,
                units_proposed=covered_read_units,
                iocs_planned=len(read_iocs_planned),
                iocs_covered=len(read_iocs_covered),
                per_tool=tuple(
                    BootstrapToolOperationCountV1(
                        tool_name=cast(_ReadToolName, tool_name),
                        planned=sum(1 for item in planned_reads if item.tool_name == tool_name),
                        covered=sum(1 for item in covered_reads if item.tool_name == tool_name),
                    )
                    for tool_name in _READ_TOOLS
                ),
                summary=(
                    f"Lectures VT : {len(covered_reads)}/{len(planned_reads)} opérations "
                    f"couvertes pour {len(read_iocs_covered)}/{len(read_iocs_planned)} IOC ; "
                    f"{covered_read_units} unités VT."
                ),
                limitations=(
                    (
                        "Le rapport d'objet est gaté par la capability file_report "
                        "(aucun flag séparé).",
                    )
                    if any(item.tool_name == "vt.object_report" for item in planned_reads)
                    else ()
                ),
            ),
            sample_acquisition=BootstrapSampleAcquisitionProposalV1(
                allowed=acquire_hits_allowed,
                acquire_hits_allowed=acquire_hits_allowed,
                sample_count_planned=sample_count_planned,
                sample_count_covered=sample_count_covered,
                downloads_limit=downloads_limit,
                per_sample_max_bytes=caps.per_sample_bytes,
                total_max_bytes=sample_count_covered * caps.per_sample_bytes,
                route_status=(
                    BootstrapRouteStatus.UNVERIFIED
                    if planned_downloads
                    else BootstrapRouteStatus.UNAVAILABLE
                ),
                summary=(
                    "Acquisition d'échantillons : "
                    f"{sample_count_covered}/{sample_count_planned} échantillons autorisés, "
                    f"{sample_count_covered * caps.per_sample_bytes} octets au maximum "
                    f"({caps.per_sample_bytes} par échantillon)."
                ),
                limitations=(
                    (
                        "La route proxy de téléchargement reste HOST-UNKNOWN (ancien audit "
                        "404) ; un échec devient route_unavailable, jamais un succès inventé.",
                    )
                    if planned_downloads
                    else ()
                ),
            ),
            costs=BootstrapCostsProposalV1(
                limits=limits,
                caps=BootstrapSettingsCapsV1(
                    vt_units=caps.vt_units,
                    downloads=caps.downloads,
                    per_sample_bytes=caps.per_sample_bytes,
                    tool_seconds=caps.tool_seconds,
                    context_bytes=caps.context_bytes,
                    context_tokens=caps.context_tokens,
                ),
                summary=(
                    "Budgets : "
                    + ", ".join(f"{item.dimension.value}={item.limit}" for item in limits)
                    + " (plafonds de configuration)."
                ),
            ),
            external_model=BootstrapExternalModelProposalV1(
                allowed=policy.external_llm_allowed,
                providers=("bridge",) if policy.bridge_allowed else (),
                summary=(
                    "Modèle externe : autorisé par la politique persistée."
                    if policy.external_llm_allowed
                    else "Modèle externe : interdit par la politique persistée "
                    "(external_llm_allowed=false) ; aucune autorisation ne peut l'élargir."
                ),
                limitations=disclosure_limitations,
            ),
            disclosure=BootstrapDisclosureProposalV1(
                tlp=policy.tlp,
                do_not_submit=policy.do_not_submit,
                external_llm_allowed=policy.external_llm_allowed,
                scope_statement=(
                    "Seuls les IOC cités par l'article (empreintes, IP, domaines, URL) et "
                    "leurs relations VT directes peuvent être envoyés ; toute découverte "
                    "reste locale jusqu'à une décision humaine."
                ),
                summary=(
                    "Divulgation : TLP "
                    f"{policy.tlp.value if policy.tlp is not None else 'non spécifiée'} ; "
                    "seuls les IOC cités par l'article sont envoyés."
                ),
                limitations=disclosure_limitations,
            ),
            summary=(
                f"{len(covered)}/{len(plan.operations)} opérations couvertes par une seule "
                "enveloppe G2 bornée.",
                "L'approbation exige un humain vérifié ; les opérations hors enveloppe "
                "restent refusées et toute extension reste une décision humaine distincte.",
            ),
            limitations=limitations,
        )

    async def approve_for_bootstrap(
        self,
        investigation_id: UUID,
        *,
        actor_id: str,
        reason: str,
        correlation_id: str,
        expected_proposal_sha256: str | None = None,
        expected_version: int | None = None,
    ) -> BootstrapEnvelopeApprovalV1:
        """Approve the freshly computed bounds through the canonical G2 service."""
        if not actor_id.strip() or not reason.strip() or not correlation_id.strip():
            raise ValueError("Bootstrap envelope approval requires actor, reason and correlation")
        proposal = await self.propose_for_bootstrap(investigation_id)
        if (
            expected_proposal_sha256 is not None
            and expected_proposal_sha256 != proposal.proposal_sha256
        ):
            raise BootstrapEnvelopeProposalStaleError(
                "The displayed bootstrap proposal no longer matches the current inventory; "
                "review the refreshed proposal before approving it."
            )
        service = InvestigationEnvelopeService(
            cast(UnitOfWorkFactory, self._uow_factory), settings=self._settings
        )
        envelope = await service.approve(
            proposal.to_service_proposal(),
            actor_id=actor_id,
            reason=reason,
            correlation_id=correlation_id,
            expected_version=expected_version,
        )
        return BootstrapEnvelopeApprovalV1(
            investigation_id=envelope.investigation_id,
            proposal=proposal,
            envelope_version=envelope.version,
            approval_decision_id=envelope.approval_decision_id,
            approval_actor_id=envelope.approval_actor_id,
            approved_at=envelope.approved_at,
            allowed_providers=envelope.allowed_providers,
            acquire_hits_allowed=envelope.acquire_hits_allowed,
            budget_limits={
                dimension.value: limit for dimension, limit in envelope.budget.limits.items()
            },
            disclosure_tlp=envelope.disclosure.tlp,
            disclosure_do_not_submit=envelope.disclosure.do_not_submit,
            disclosure_external_llm_allowed=envelope.disclosure.external_llm_allowed,
        )

    def _proposal_sha256(
        self,
        *,
        investigation_id: UUID,
        inventory: IOCInventoryV1,
        caps: _AuthorityCaps,
        providers: tuple[str, ...],
        budget_limits: Mapping[BudgetDimension, int],
        acquire_hits_allowed: bool,
        policy: _PolicyView,
        planned_operations: Sequence[BootstrapPlannedOperationV1],
        covered_operations: Sequence[BootstrapPlannedOperationV1],
    ) -> str:
        payload = {
            "schema_version": PROPOSAL_SCHEMA_VERSION,
            "investigation_id": str(investigation_id),
            "inventory": _inventory_identity(inventory),
            "caps": {
                "vt_units": caps.vt_units,
                "downloads": caps.downloads,
                "per_sample_bytes": caps.per_sample_bytes,
                "tool_seconds": caps.tool_seconds,
                "context_bytes": caps.context_bytes,
                "context_tokens": caps.context_tokens,
            },
            "allowed_providers": list(providers),
            "budget_limits": {
                dimension.value: amount for dimension, amount in budget_limits.items()
            },
            "acquire_hits_allowed": acquire_hits_allowed,
            "disclosure": {
                "tlp": policy.tlp.value if policy.tlp is not None else None,
                "do_not_submit": policy.do_not_submit,
                "external_llm_allowed": policy.external_llm_allowed,
            },
            "planned_operation_keys": sorted(
                operation.operation_key for operation in planned_operations
            ),
            "covered_operation_keys": sorted(
                operation.operation_key for operation in covered_operations
            ),
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    @staticmethod
    def _disclosure_limitations(policy: _PolicyView) -> tuple[str, ...]:
        if not policy.external_llm_allowed:
            return (
                "La politique persistée interdit l'usage d'un modèle externe "
                "(external_llm_allowed=false) : aucune opération VT ou Bridge n'est proposée.",
            )
        if not policy.tlp_parseable:
            if policy.bridge_allowed:
                return (
                    "La TLP de l'article est absente ou invalide : VT est refusé, Bridge "
                    "reste autorisé par le profil de divulgation.",
                )
            return ("La TLP de l'article est absente ou invalide : aucun envoi externe.",)
        tlp = policy.tlp.value if policy.tlp is not None else "inconnue"
        if not policy.vt_allowed and policy.bridge_allowed:
            return (f"La TLP {tlp} interdit l'envoi VT ; Bridge reste autorisé.",)
        if policy.vt_allowed and not policy.bridge_allowed:
            return (f"La TLP {tlp} interdit l'envoi Bridge ; VT reste autorisé.",)
        if not policy.vt_allowed and not policy.bridge_allowed:
            return (f"La TLP {tlp} interdit tout envoi externe VT ou Bridge.",)
        return ()

    @staticmethod
    def _coverage_limitations(
        *,
        policy: _PolicyView,
        caps: _AuthorityCaps,
        planned_operations: int,
        covered_operations: int,
        planned_downloads: int,
        covered_downloads: int,
        capability_download: bool,
    ) -> tuple[str, ...]:
        notes: list[str] = []
        if policy.vt_allowed and caps.vt_units == 0 and planned_operations:
            notes.append(
                "Le plafond investigation_max_vt_read_units vaut 0 : aucune lecture VT "
                "n'est couverte."
            )
        if policy.vt_allowed and planned_operations > covered_operations:
            notes.append(
                f"{planned_operations - covered_operations} opérations sur "
                f"{planned_operations} restent hors enveloppe et exigeront une décision "
                "humaine distincte (extend)."
            )
        if capability_download and planned_downloads > covered_downloads:
            notes.append(
                f"{planned_downloads - covered_downloads} téléchargement(s) sur "
                f"{planned_downloads} restent hors enveloppe."
            )
        return tuple(notes)

    @staticmethod
    def _proposal_limitations(
        *,
        plan_limitations: Sequence[str],
        inventory_limitations: Sequence[str],
        disclosure_limitations: Sequence[str],
        coverage_limitations: Sequence[str],
        already_approved: bool,
    ) -> tuple[str, ...]:
        notes = [
            *disclosure_limitations,
            *coverage_limitations,
            *inventory_limitations,
            *plan_limitations,
        ]
        if already_approved:
            notes.append(
                "Une enveloppe G2 existe déjà ; toute extension reste une décision humaine "
                "distincte (extend) et ne peut que croître."
            )
        return tuple(dict.fromkeys(notes))[:MAX_PROPOSAL_LIMITATIONS]

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("Bootstrap envelope clock must return timezone-aware datetimes")
        return value


# Local alias keeps the literal tool-name cast explicit and searchable.
__all__ = [
    "APPROVAL_SCHEMA_VERSION",
    "PROPOSAL_SCHEMA_VERSION",
    "BootstrapBudgetLimitSource",
    "BootstrapBudgetLimitV1",
    "BootstrapCostsProposalV1",
    "BootstrapDisclosureProposalV1",
    "BootstrapDisclosureScope",
    "BootstrapEnvelopeApprovalV1",
    "BootstrapEnvelopeError",
    "BootstrapEnvelopeProposalStaleError",
    "BootstrapEnvelopeProposalV1",
    "BootstrapEnvelopeUnitOfWork",
    "BootstrapExternalModelProposalV1",
    "BootstrapInventoryCountsV1",
    "BootstrapSampleAcquisitionProposalV1",
    "BootstrapSettingsCapsV1",
    "BootstrapToolOperationCountV1",
    "BootstrapVtReadsProposalV1",
    "InvestigationBootstrapEnvelopeService",
]
