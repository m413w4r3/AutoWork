"""Read-only Shodan tools for evidence-led investigations."""

from __future__ import annotations

import hashlib
import ipaddress
import math
import re
from collections.abc import Awaitable, Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from uuid import UUID

from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
)
from cti_app.application.investigation_tools import (
    ToolAdapter,
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.research_context_builder import ArticleSnapshot, ArticleSnapshotPort
from cti_app.application.shodan import (
    ShodanBudgetEstimate,
    ShodanCapabilities,
    ShodanCapabilityDisabledError,
    ShodanCountPort,
    ShodanError,
    ShodanHttpError,
    ShodanNotConfiguredError,
    ShodanNotFoundError,
    ShodanPort,
    ShodanQuotaError,
    ShodanResponseTooLargeError,
    ShodanTransportError,
    validate_shodan_domain,
    validate_shodan_ip,
    validate_shodan_page,
    validate_shodan_search_query,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    BudgetExceeded,
    EvidenceKind,
    ToolInvocationStatus,
)
from cti_app.domain.shodan import (
    ShodanCapability,
    ShodanHostView,
    ShodanObservationMeta,
    ShodanService,
)

SCHEMA_VERSION = "1"
SCHEMA_DRAFT = "https://json-schema.org/draft/2020-12/schema"
SHODAN_SOURCE_VERSION = "passive-api.v1"
MAX_BANNER_SUMMARY_CHARS = 1_024
MAX_MATCH_SUMMARIES = 32
MAX_DOMAIN_RECORDS = 128
MAX_EVIDENCE_REFS = 32
MAX_PUBLIC_OBJECT_ITEMS = 256
MAX_PUBLIC_ARRAY_ITEMS = 128
MAX_PUBLIC_TEXT_CHARS = 4_000
DEFAULT_SHODAN_MAX_RESPONSE_BYTES = 10 * 1024 * 1024
SHODAN_RAW_BUCKET = "shodan-raw"
SHODAN_RAW_MIME_TYPE = "application/json"

SHODAN_TOOL_CAPABILITIES: Mapping[ShodanCapability, str] = {
    ShodanCapability.HOST: "shodan.host",
    ShodanCapability.SEARCH: "shodan.search",
    ShodanCapability.DOMAIN: "shodan.domain",
    ShodanCapability.COUNT: "shodan.count",
}

_SHARED_CERTIFICATE_LIMITATION = (
    "Un certificat ou une bannière partagés n'établissent pas l'attribution d'une IP à une famille."
)
_INDEX_LIMITATION = "L'index Shodan ne constitue pas un scan en temps réel."
_TIME_WINDOW_LIMITATION = (
    "Un service observé à la date indiquée ne démontre pas qu'il était actif "
    "pendant la période de l'attaque."
)
_UUID_PATTERN = (
    "^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[1-8][0-9a-fA-F]{3}-[89aAbB][0-9a-fA-F]{3}-[0-9a-fA-F]{12}$"
)
_URL_PATTERN = re.compile(r"https?://", re.IGNORECASE)
_BEARER_HEADER = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_FIELD = re.compile(
    r"(?:api[_-]?key|private[_-]?key|client[_-]?secret|access[_-]?token|"
    r"bearer|token|secret|password|authorization|credential|cookie|signature)",
    re.IGNORECASE,
)
_BINARY_FIELD = re.compile(
    r"(?:raw[_-]?bytes|file[_-]?bytes|sample[_-]?bytes|payload[_-]?bytes)", re.I
)
_QUERY_IPV4 = re.compile(r"(?<![0-9.])(?:[0-9]{1,3}\.){3}[0-9]{1,3}(?![0-9./])")
_QUERY_DOMAIN = re.compile(r"(?<![A-Za-z0-9_-])(?:[A-Za-z0-9-]+\.)+[A-Za-z]{2,63}\.?")

ArticleSnapshotResolver = Callable[[UUID], Awaitable[ArticleSnapshot]]


class RawResponseArchive(Protocol):
    """Content-addressed byte store, implemented by ``ProductionArtifactStore``."""

    async def put_bytes(self, content: bytes, *, bucket: str, mime_type: str) -> UUID: ...


def _shodan_response_limit(shodan: ShodanPort | None, requested_limit: int | None) -> int:
    limit = requested_limit
    if limit is None:
        # The E1 HTTP adapter enforces this configured ceiling before producing a view.
        # Its storage remains private; callers with a wrapped port can pass the setting
        # explicitly through ``max_response_bytes``.
        configured = getattr(shodan, "_max_response_bytes", None)
        limit = configured if isinstance(configured, int) else DEFAULT_SHODAN_MAX_RESPONSE_BYTES
    if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
        raise ValueError("max_response_bytes must be a positive integer")
    return limit


def shodan_tool_capabilities(capabilities: ShodanCapabilities) -> frozenset[str]:
    """Project configured Shodan switches to the per-tool registry keys."""
    if not isinstance(capabilities, ShodanCapabilities):
        raise TypeError("Shodan capabilities must use ShodanCapabilities")
    return frozenset(
        SHODAN_TOOL_CAPABILITIES[capability]
        for capability in ShodanCapability
        if capabilities.is_enabled(capability)
    )


def register_shodan_tools(
    registry: ToolRegistry,
    dependencies: Any | None = None,
    *,
    shodan: ShodanPort | None = None,
    recorder: EvidenceRecorder | None = None,
    article_snapshots: ArticleSnapshotPort | ArticleSnapshot | None = None,
    observations: EvidenceObservationRepository | None = None,
    findings: FindingRepository | None = None,
    capabilities: ShodanCapabilities | None = None,
    budget_estimate: ShodanBudgetEstimate | None = None,
    article_snapshot_resolver: ArticleSnapshotResolver | None = None,
    synthesis_artifact_id: UUID | None = None,
    shodan_count: ShodanCountPort | None = None,
    raw_archive: RawResponseArchive | None = None,
    max_response_bytes: int | None = None,
) -> None:
    """Register Shodan tools directly or from K1's ``ToolRegistrar`` dependencies.

    ``dependencies`` is the positional K1 bundle. Direct callers may instead
    pass the dependencies as keyword arguments. K1's snapshot port resolves by
    synthesis ID, so production registrars should provide either
    ``article_snapshot_resolver`` or ``synthesis_artifact_id`` to add the
    article-IOC relationship to each observation.
    """
    if dependencies is not None:
        shodan = shodan or getattr(dependencies, "shodan", None)
        article_snapshots = article_snapshots or getattr(dependencies, "article_snapshots", None)
        observations = observations or getattr(dependencies, "observations", None)
        findings = findings or getattr(dependencies, "findings", None)
        artifact_store = getattr(dependencies, "artifact_store", None)
        if raw_archive is None and callable(getattr(artifact_store, "put_bytes", None)):
            raw_archive = cast(RawResponseArchive, artifact_store)
        if recorder is None:
            if artifact_store is not None and observations is not None:
                from cti_app.application.evidence_recorder import ProductionEvidenceRecorder

                recorder = ProductionEvidenceRecorder(artifact_store, observations)
        if shodan_count is None:
            candidate = getattr(dependencies, "shodan", None)
            if callable(getattr(candidate, "count", None)):
                shodan_count = cast(ShodanCountPort, candidate)

    if recorder is None or article_snapshots is None or observations is None or findings is None:
        raise ValueError(
            "Shodan tools require a recorder, article snapshots, "
            "observation repository, and finding repository"
        )
    estimate = budget_estimate or ShodanBudgetEstimate()
    if shodan_count is None and callable(getattr(shodan, "count", None)):
        shodan_count = cast(ShodanCountPort, shodan)
    response_limit = _shodan_response_limit(shodan, max_response_bytes)

    adapters: tuple[tuple[ShodanCapability, ToolAdapter], ...] = (
        (
            ShodanCapability.HOST,
            ShodanHostToolAdapter(
                shodan=shodan,
                recorder=recorder,
                article_snapshots=article_snapshots,
                observations=observations,
                findings=findings,
                capabilities=capabilities,
                estimate=estimate,
                article_snapshot_resolver=article_snapshot_resolver,
                synthesis_artifact_id=synthesis_artifact_id,
                raw_archive=raw_archive,
                max_response_bytes=response_limit,
            ),
        ),
        (
            ShodanCapability.SEARCH,
            ShodanSearchToolAdapter(
                shodan=shodan,
                recorder=recorder,
                article_snapshots=article_snapshots,
                observations=observations,
                findings=findings,
                capabilities=capabilities,
                estimate=estimate,
                article_snapshot_resolver=article_snapshot_resolver,
                synthesis_artifact_id=synthesis_artifact_id,
                raw_archive=raw_archive,
                max_response_bytes=response_limit,
            ),
        ),
        (
            ShodanCapability.DOMAIN,
            ShodanDomainToolAdapter(
                shodan=shodan,
                recorder=recorder,
                article_snapshots=article_snapshots,
                observations=observations,
                findings=findings,
                capabilities=capabilities,
                estimate=estimate,
                article_snapshot_resolver=article_snapshot_resolver,
                synthesis_artifact_id=synthesis_artifact_id,
                raw_archive=raw_archive,
                max_response_bytes=response_limit,
            ),
        ),
        (
            ShodanCapability.COUNT,
            ShodanCountToolAdapter(
                shodan_count=shodan_count,
                recorder=recorder,
                article_snapshots=article_snapshots,
                observations=observations,
                findings=findings,
                capabilities=capabilities,
                estimate=estimate,
                article_snapshot_resolver=article_snapshot_resolver,
                synthesis_artifact_id=synthesis_artifact_id,
                raw_archive=raw_archive,
                max_response_bytes=response_limit,
            ),
        ),
    )
    for capability, adapter in adapters:
        registry.register(_tool_definition(capability, estimate), adapter)


class _BaseShodanToolAdapter:
    capability: ShodanCapability

    def __init__(
        self,
        *,
        recorder: EvidenceRecorder,
        article_snapshots: ArticleSnapshotPort | ArticleSnapshot,
        observations: EvidenceObservationRepository,
        findings: FindingRepository,
        capabilities: ShodanCapabilities | None,
        estimate: ShodanBudgetEstimate,
        article_snapshot_resolver: ArticleSnapshotResolver | None,
        synthesis_artifact_id: UUID | None,
        raw_archive: RawResponseArchive | None,
        max_response_bytes: int,
    ) -> None:
        self._recorder = recorder
        self._article_snapshots = article_snapshots
        self._observations = observations
        self._findings = findings
        self._capabilities = capabilities
        self._estimate = estimate
        self._article_snapshot_resolver = article_snapshot_resolver
        self._synthesis_artifact_id = synthesis_artifact_id
        self._raw_archive = raw_archive
        self._max_response_bytes = max_response_bytes

    @property
    def _estimated_credits(self) -> int:
        return self._estimate.credits_for(self.capability)

    async def _preflight(
        self, context: ToolExecutionContext, *, port_available: bool
    ) -> ToolResult | None:
        capability_key = SHODAN_TOOL_CAPABILITIES[self.capability]
        if capability_key not in context.available_capabilities:
            return _local_result(
                ToolInvocationStatus.UNAVAILABLE,
                "capability_disabled",
                "La capacité Shodan sélectionnée n'est pas disponible dans cette enquête.",
            )
        if self._capabilities is not None and not self._capabilities.is_enabled(self.capability):
            return _local_result(
                ToolInvocationStatus.UNAVAILABLE,
                "capability_disabled",
                "La capacité Shodan sélectionnée n'est pas activée.",
            )
        if not port_available:
            return _local_result(
                ToolInvocationStatus.UNAVAILABLE,
                "not_configured",
                "La capacité Shodan sélectionnée n'est pas configurée.",
            )
        envelope = context.envelope
        if (
            envelope is None
            or "shodan" not in envelope.allowed_providers
            or envelope.disclosure.do_not_submit
            or getattr(envelope.disclosure.tlp, "value", None) == "RED"
        ):
            return _local_result(
                ToolInvocationStatus.POLICY_DENIED,
                "outside_envelope",
                "L'enveloppe n'autorise pas cette lecture externe Shodan.",
                alternatives=("article.get_claims", "evidence.get"),
            )
        try:
            context.budget.reserve(
                context.idempotency_key,
                {BudgetDimension.SHODAN_CREDITS: self._estimated_credits},
            )
        except BudgetExceeded:
            return _local_result(
                ToolInvocationStatus.POLICY_DENIED,
                "credits_not_allocated",
                "L'enveloppe n'alloue pas assez de crédits Shodan à cette opération.",
                alternatives=("article.get_claims", "evidence.get"),
            )
        return None

    async def _validate_evidence_refs(
        self, values: object, investigation_id: UUID
    ) -> tuple[UUID, ...]:
        if not isinstance(values, list | tuple):
            raise ValueError("evidence_refs must be an array")
        if len(values) > MAX_EVIDENCE_REFS:
            raise ValueError("Too many evidence references")
        validated: list[UUID] = []
        for value in values:
            try:
                ref = UUID(str(value))
            except (ValueError, TypeError, AttributeError):
                raise ValueError("An evidence reference is not a UUID") from None
            observation = await self._observations.get(ref)
            if observation is not None:
                if observation.investigation_id != investigation_id:
                    raise ValueError("An evidence reference belongs to another investigation")
                if ref not in validated:
                    validated.append(ref)
                continue
            finding = await self._findings.get(ref)
            if finding is None or finding.investigation_id != investigation_id:
                raise ValueError("An evidence reference is unknown to this investigation")
            if ref not in validated:
                validated.append(ref)
        return tuple(validated)

    async def _article_relation(
        self,
        context: ToolExecutionContext,
        object_values: tuple[str, ...],
    ) -> dict[str, Any]:
        snapshot: ArticleSnapshot | None = None
        if isinstance(self._article_snapshots, ArticleSnapshot):
            snapshot = self._article_snapshots
        else:
            try:
                if self._article_snapshot_resolver is not None:
                    snapshot = await self._article_snapshot_resolver(context.investigation_id)
                elif self._synthesis_artifact_id is not None:
                    snapshot = await self._article_snapshots.resolve(self._synthesis_artifact_id)
                else:
                    resolve_for_investigation = getattr(
                        self._article_snapshots, "resolve_for_investigation", None
                    )
                    if callable(resolve_for_investigation):
                        snapshot = await resolve_for_investigation(context.investigation_id)
                    else:
                        get_iocs = getattr(self._article_snapshots, "get_documented_iocs", None)
                        if callable(get_iocs):
                            rows = await get_iocs(context.investigation_id)
                            return _match_iocs(rows, object_values, resolution="resolved")
            except Exception:
                # A local snapshot failure must not discard an already captured provider result.
                snapshot = None
        if snapshot is None:
            return {
                "resolution": "unavailable",
                "present": None,
                "matches": [],
                "note": "ArticleSnapshotPort does not resolve full snapshots by investigation ID.",
            }
        return _match_iocs(snapshot.article.known_chain, object_values, resolution="resolved")

    async def _vt_relationships(
        self, investigation_id: UUID, object_values: tuple[str, ...]
    ) -> list[dict[str, str]]:
        targets = {_normalize_ioc(value) for value in object_values}
        targets.discard(None)
        if not targets:
            return []
        related: list[dict[str, str]] = []
        for observation in await self._observations.list_for_investigation(investigation_id):
            if observation.kind is not EvidenceKind.VT_RELATIONSHIP:
                continue
            normalized = _normalize_ioc(observation.object_id)
            if normalized in targets:
                related.append(
                    {
                        "observation_id": str(observation.id),
                        "object_type": observation.object_type,
                        "object_id": observation.object_id,
                    }
                )
        return related[:MAX_EVIDENCE_REFS]

    async def _record_observation(
        self,
        *,
        context: ToolExecutionContext,
        object_type: str,
        object_id: str,
        object_values: tuple[str, ...],
        meta: ShodanObservationMeta,
        summary: Mapping[str, Any],
        limitations: tuple[str, ...],
        raw_response: bytes,
        raw_sha256: str,
    ) -> tuple[tuple[UUID, ...], dict[str, Any], bool]:
        article_relation = await self._article_relation(context, object_values)
        vt_relationships = await self._vt_relationships(context.investigation_id, object_values)
        computed_sha256 = hashlib.sha256(raw_response).hexdigest()
        if computed_sha256 != raw_sha256:
            raise ValueError("Shodan response bytes do not match the provider SHA-256")
        archive_partial = False
        observation_limitations = limitations
        raw_blob_id: UUID | None = None
        if self._raw_archive is None:
            observation_limitations = _merge_limitations(
                observation_limitations,
                ("Réponse brute non archivée : aucun archiveur n'est configuré.",),
            )
        elif len(raw_response) > self._max_response_bytes:
            archive_partial = True
            observation_limitations = _merge_limitations(
                observation_limitations,
                ("Réponse brute non archivée : shodan_max_response_bytes est dépassé.",),
            )
        else:
            raw_blob_id = await self._raw_archive.put_bytes(
                raw_response,
                bucket=SHODAN_RAW_BUCKET,
                mime_type=SHODAN_RAW_MIME_TYPE,
            )
        payload = {
            "schema": "shodan-observation.v1",
            "captured_at": _iso(meta.captured_at),
            "service_captures": _service_captures(meta),
            "coverage_limits": list(dict.fromkeys((*meta.coverage_limits, _INDEX_LIMITATION))),
            "credits_consumed": meta.credits_consumed,
            "query_credits": meta.query_credits,
            "plan": meta.plan,
            "route": meta.route.value,
            "object": {"type": object_type, "value": object_id},
            "article_ioc_relation": article_relation,
            "vt_relationships": vt_relationships,
            "summary": dict(summary),
        }
        observation_refs: tuple[UUID, ...]
        summary_observation_id: UUID | None = None
        if raw_blob_id is not None:
            raw_observation = await self._recorder.record_existing_blob(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.SHODAN_OBSERVATION,
                blob_id=raw_blob_id,
                sha256=raw_sha256,
                source="shodan",
                source_version=SHODAN_SOURCE_VERSION,
                object_type=object_type,
                object_id=object_id,
                limitations=observation_limitations,
                observed_at=meta.captured_at,
                idempotency_key=f"shodan:{context.idempotency_key}:raw",
            )
            summary_observation = await self._recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.SHODAN_OBSERVATION,
                payload=payload,
                source="shodan",
                source_version=SHODAN_SOURCE_VERSION,
                object_type=object_type,
                object_id=object_id,
                limitations=observation_limitations,
                observed_at=meta.captured_at,
                idempotency_key=f"shodan:{context.idempotency_key}:summary",
            )
            summary_observation_id = summary_observation.id
            observation_refs = (raw_observation.id, summary_observation.id)
        else:
            observation = await self._recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.SHODAN_OBSERVATION,
                payload=payload,
                source="shodan",
                source_version=SHODAN_SOURCE_VERSION,
                object_type=object_type,
                object_id=object_id,
                limitations=observation_limitations,
                observed_at=meta.captured_at,
                idempotency_key=f"shodan:{context.idempotency_key}",
            )
            observation_refs = (observation.id,)
        result_output = {
            **payload,
            "observation_id": str(observation_refs[0]),
            "raw_sha256": raw_sha256,
            "raw_blob_id": str(raw_blob_id) if raw_blob_id is not None else None,
            "raw_response_archived": raw_blob_id is not None,
        }
        if summary_observation_id is not None:
            result_output["summary_observation_id"] = str(summary_observation_id)
        return observation_refs, _sanitize_public_json(result_output), archive_partial

    async def _record_negative(
        self,
        *,
        context: ToolExecutionContext,
        object_type: str,
        object_id: str,
        object_values: tuple[str, ...],
        reason: str,
        details: Mapping[str, Any],
        limitations: tuple[str, ...],
        meta: ShodanObservationMeta | None = None,
        observed_at: datetime | None = None,
    ) -> tuple[UUID, dict[str, Any]]:
        article_relation = await self._article_relation(context, object_values)
        vt_relationships = await self._vt_relationships(context.investigation_id, object_values)
        captured_at = meta.captured_at if meta is not None else (observed_at or datetime.now(UTC))
        coverage_limits = (
            tuple(dict.fromkeys((*meta.coverage_limits, _INDEX_LIMITATION)))
            if meta is not None
            else (_INDEX_LIMITATION,)
        )
        observation = await self._recorder.record_negative(
            investigation_id=context.investigation_id,
            reason=reason,
            source="shodan",
            source_version=SHODAN_SOURCE_VERSION,
            object_type=object_type,
            object_id=object_id,
            limitations=limitations,
            details={
                "schema": "shodan-negative-result.v1",
                "captured_at": _iso(captured_at),
                "service_captures": _service_captures(meta) if meta is not None else [],
                "coverage_limits": list(coverage_limits),
                "credits_consumed": meta.credits_consumed if meta is not None else 0,
                "query_credits": meta.query_credits if meta is not None else None,
                "plan": meta.plan if meta is not None else None,
                "route": meta.route.value if meta is not None else None,
                "object": {"type": object_type, "value": object_id},
                "article_ioc_relation": article_relation,
                "vt_relationships": vt_relationships,
                **dict(details),
            },
            observed_at=captured_at,
            idempotency_key=f"shodan:{context.idempotency_key}",
        )
        return observation.id, {
            "reason": "not_found",
            "object": {"type": object_type, "value": object_id},
            "observation_id": str(observation.id),
            "captured_at": _iso(captured_at),
            "coverage_limits": list(coverage_limits),
            "credits_consumed": meta.credits_consumed if meta is not None else 0,
            "article_ioc_relation": article_relation,
            "vt_relationships": vt_relationships,
        }

    def _error_result(self, exc: ShodanError, *, object_type: str, object_id: str) -> ToolResult:
        if isinstance(exc, ShodanNotFoundError) or exc.status_code == 404:
            raise exc
        if exc.status_code in {401, 403}:
            details: dict[str, Any] = {
                "limitations": [
                    "Shodan refused this provider capability; no host or domain data was returned."
                ]
            }
            upstream_message = getattr(exc, "upstream_message", None)
            if isinstance(upstream_message, str) and upstream_message:
                details["upstream_message"] = upstream_message[:200]
            plan = getattr(exc, "plan", None)
            if isinstance(plan, str) and plan:
                details["plan"] = plan[:50]
                details["limitations"].append(f"Shodan account plan reported: {plan[:50]}.")
            query_credits = getattr(exc, "query_credits", None)
            if isinstance(query_credits, int) and not isinstance(query_credits, bool):
                details["query_credits"] = max(0, query_credits)
            return _local_result(
                ToolInvocationStatus.UNAVAILABLE,
                "provider_authorization_refused",
                "Shodan refused authorization for this provider capability.",
                credits=0,
                details=details,
            )
        if isinstance(exc, ShodanQuotaError) or exc.status_code in {402, 429}:
            return _local_result(
                ToolInvocationStatus.UNAVAILABLE,
                "quota",
                "Le quota de crédits Shodan est épuisé.",
                credits=0,
            )
        if isinstance(exc, (ShodanNotConfiguredError, ShodanCapabilityDisabledError)):
            return _local_result(
                ToolInvocationStatus.UNAVAILABLE,
                "not_configured",
                "La capacité Shodan sélectionnée n'est pas configurée.",
                credits=0,
            )
        if isinstance(exc, ShodanResponseTooLargeError):
            return _local_result(
                ToolInvocationStatus.PARTIAL,
                "response_ceiling",
                "La réponse Shodan a dépassé la limite locale de taille.",
                credits=self._estimated_credits,
            )
        if isinstance(exc, ShodanTransportError) or (
            isinstance(exc, ShodanHttpError)
            and exc.status_code is not None
            and exc.status_code >= 500
        ):
            return _local_result(
                ToolInvocationStatus.FAILED,
                "provider_error",
                "La lecture passive Shodan a échoué pendant le transport ou côté fournisseur.",
                credits=self._estimated_credits,
            )
        return _local_result(
            ToolInvocationStatus.FAILED,
            "provider_error",
            "Shodan a refusé ou n'a pas pu traiter cette lecture.",
            credits=self._estimated_credits,
        )


class ShodanHostToolAdapter(_BaseShodanToolAdapter):
    capability = ShodanCapability.HOST

    def __init__(self, *, shodan: ShodanPort | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shodan = shodan

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        preflight = await self._preflight(context, port_available=self._shodan is not None)
        if preflight is not None:
            return preflight
        try:
            ip = validate_shodan_ip(str(arguments["ip"]))
        except ShodanError:
            return _local_result(
                ToolInvocationStatus.FAILED, "invalid_input", "Une adresse IP valide est requise."
            )
        assert self._shodan is not None
        try:
            view = await self._shodan.host(ip)
        except ShodanError as exc:
            return await self._handle_error(exc, context, "ip", ip, (ip,))
        if view.http_status == 404:
            return await self._not_found(context, "ip", ip, (ip,), meta=view.meta)
        summary, limits = _host_summary(view)
        object_values = (ip,)
        limitations = _base_limitations(view.meta, view.tags, shared_cert_hosts=1)
        limitations = _merge_limitations(limitations, limits)
        observation_refs, output, archive_partial = await self._record_observation(
            context=context,
            object_type="ip",
            object_id=ip,
            object_values=object_values,
            meta=view.meta,
            summary=summary,
            limitations=limitations,
            raw_response=view.raw_response,
            raw_sha256=view.raw_sha256,
        )
        status = (
            ToolInvocationStatus.EMPTY
            if not view.services and not view.hostnames
            else ToolInvocationStatus.SUCCEEDED
        )
        if status is ToolInvocationStatus.EMPTY:
            # A host record with no service data is still an observation, not a global negative.
            status = ToolInvocationStatus.SUCCEEDED
        if archive_partial:
            status = ToolInvocationStatus.PARTIAL
        return ToolResult(
            status=status,
            observation_refs=observation_refs,
            budget_consumed={BudgetDimension.SHODAN_CREDITS: view.meta.credits_consumed},
            output=output,
            message="Observation passive Shodan enregistrée.",
        )

    async def _handle_error(
        self,
        exc: ShodanError,
        context: ToolExecutionContext,
        object_type: str,
        object_id: str,
        object_values: tuple[str, ...],
    ) -> ToolResult:
        if isinstance(exc, ShodanNotFoundError) or exc.status_code == 404:
            return await self._not_found(context, object_type, object_id, object_values)
        return self._error_result(exc, object_type=object_type, object_id=object_id)

    async def _not_found(
        self,
        context: ToolExecutionContext,
        object_type: str,
        object_id: str,
        object_values: tuple[str, ...],
        *,
        meta: ShodanObservationMeta | None = None,
    ) -> ToolResult:
        limitations = _base_limitations(meta, ())
        ref, output = await self._record_negative(
            context=context,
            object_type=object_type,
            object_id=object_id,
            object_values=object_values,
            reason="Shodan n'a renvoyé aucune information pour cet objet.",
            details={"http_status": 404},
            limitations=limitations,
            meta=meta,
        )
        return ToolResult(
            status=ToolInvocationStatus.EMPTY,
            observation_refs=(ref,),
            budget_consumed={
                BudgetDimension.SHODAN_CREDITS: meta.credits_consumed if meta is not None else 0
            },
            output=output,
            message="Aucune information Shodan disponible pour cet objet.",
        )


class ShodanSearchToolAdapter(_BaseShodanToolAdapter):
    capability = ShodanCapability.SEARCH

    def __init__(self, *, shodan: ShodanPort | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shodan = shodan

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        preflight = await self._preflight(context, port_available=self._shodan is not None)
        if preflight is not None:
            return preflight
        try:
            query = validate_shodan_search_query(str(arguments["query"]))
            if _URL_PATTERN.search(query) or sanitize_evidence_text(query) != query:
                raise ValueError("Query cannot contain URLs or secret values")
            page = validate_shodan_page(arguments.get("page", 1))
            evidence_refs = await self._validate_evidence_refs(
                arguments.get("evidence_refs", []), context.investigation_id
            )
            justification = sanitize_evidence_text(arguments["justification"], max_chars=1_000)
            if not justification.strip():
                raise ValueError("A search justification is required")
        except (ShodanError, ValueError, KeyError, TypeError):
            return _local_result(
                ToolInvocationStatus.FAILED,
                "invalid_input",
                "La requête, la justification ou les références de preuve Shodan sont invalides.",
            )
        assert self._shodan is not None
        try:
            result = await self._shodan.search(query, page)
        except ShodanError as exc:
            if isinstance(exc, ShodanNotFoundError) or exc.status_code == 404:
                return await self._not_found(context, "query", query, (*_query_iocs(query), query))
            return self._error_result(exc, object_type="query", object_id=query)
        if result.http_status == 404:
            return await self._not_found(
                context, "query", query, (*_query_iocs(query), query), meta=result.meta
            )
        matches = result.matches[:MAX_MATCH_SUMMARIES]
        object_values = tuple(
            dict.fromkeys((*_query_iocs(query), query, *(item.ip for item in matches)))
        )
        match_summaries: list[dict[str, Any]] = []
        match_limits: list[str] = []
        for match in matches:
            match_summary, match_truncation_limits = _host_summary(match)
            match_summaries.append(match_summary)
            match_limits.extend(match_truncation_limits)
        summary = {
            "query": query,
            "justification": justification,
            "evidence_refs": [str(ref) for ref in evidence_refs],
            "page": result.page,
            "total": result.total,
            "matches": match_summaries,
            "matches_truncated": len(result.matches) > len(matches),
            "raw_sha256": result.raw_sha256,
        }
        shared_cert_hosts = _shared_certificate_host_count(matches)
        shared_asn_hosts = _shared_asn_host_count(matches)
        tags = tuple(tag for match in matches for tag in match.tags)
        limitations = _base_limitations(
            result.meta,
            tags,
            shared_cert_hosts=shared_cert_hosts,
            shared_asn_hosts=shared_asn_hosts,
        )
        limitations = _merge_limitations(limitations, tuple(match_limits))
        if result.total > len(result.matches):
            limitations = _merge_limitations(
                limitations,
                ("La réponse ne couvre qu'une page ; d'autres matches peuvent rester indexés.",),
            )
        partial = (
            result.total > len(result.matches)
            or len(result.matches) > len(matches)
            or any(item.banner_truncated for match in matches for item in match.services)
        )
        if result.total == 0 and not result.matches:
            ref, negative_output = await self._record_negative(
                context=context,
                object_type="query",
                object_id=query,
                object_values=object_values,
                reason="La requête Shodan n'a renvoyé aucun match dans l'index consulté.",
                details={
                    "page": result.page,
                    "total": 0,
                    "raw_sha256": result.raw_sha256,
                    "captured_at": _iso(result.meta.captured_at),
                    "credits_consumed": result.meta.credits_consumed,
                },
                limitations=limitations,
                meta=result.meta,
                observed_at=result.meta.captured_at,
            )
            return ToolResult(
                status=ToolInvocationStatus.EMPTY,
                observation_refs=(ref,),
                budget_consumed={BudgetDimension.SHODAN_CREDITS: result.meta.credits_consumed},
                output=negative_output,
                message="Aucun match dans la page Shodan consultée.",
            )
        observation_refs, output, archive_partial = await self._record_observation(
            context=context,
            object_type="query",
            object_id=query,
            object_values=object_values,
            meta=result.meta,
            summary=summary,
            limitations=limitations,
            raw_response=result.raw_response,
            raw_sha256=result.raw_sha256,
        )
        return ToolResult(
            status=(
                ToolInvocationStatus.PARTIAL
                if partial or archive_partial
                else ToolInvocationStatus.SUCCEEDED
            ),
            observation_refs=observation_refs,
            budget_consumed={BudgetDimension.SHODAN_CREDITS: result.meta.credits_consumed},
            output=output,
            message=(
                "Résultats Shodan partiels : page ou résumé borné."
                if partial
                else "Recherche passive Shodan enregistrée."
            ),
        )

    async def _not_found(
        self,
        context: ToolExecutionContext,
        object_type: str,
        object_id: str,
        object_values: tuple[str, ...],
        meta: ShodanObservationMeta | None = None,
    ) -> ToolResult:
        ref, output = await self._record_negative(
            context=context,
            object_type=object_type,
            object_id=object_id,
            object_values=object_values,
            reason="Shodan n'a renvoyé aucune information pour cette requête.",
            details={"http_status": 404},
            limitations=_base_limitations(meta, ()),
            meta=meta,
        )
        return ToolResult(
            status=ToolInvocationStatus.EMPTY,
            observation_refs=(ref,),
            budget_consumed={
                BudgetDimension.SHODAN_CREDITS: meta.credits_consumed if meta is not None else 0
            },
            output=output,
            message="Aucune information Shodan disponible pour cette requête.",
        )


class ShodanDomainToolAdapter(_BaseShodanToolAdapter):
    capability = ShodanCapability.DOMAIN

    def __init__(self, *, shodan: ShodanPort | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shodan = shodan

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        preflight = await self._preflight(context, port_available=self._shodan is not None)
        if preflight is not None:
            return preflight
        try:
            domain = validate_shodan_domain(str(arguments["domain"]))
        except ShodanError:
            return _local_result(
                ToolInvocationStatus.FAILED,
                "invalid_input",
                "Un nom de domaine valide est requis.",
            )
        assert self._shodan is not None
        try:
            result = await self._shodan.domain(domain)
        except ShodanError as exc:
            if isinstance(exc, ShodanNotFoundError) or exc.status_code == 404:
                return await self._not_found(context, domain)
            return self._error_result(exc, object_type="domain", object_id=domain)
        if result.http_status == 404:
            return await self._not_found(context, domain, meta=result.meta)
        records = [
            _sanitize_public_json(record) for record in result.dns_records[:MAX_DOMAIN_RECORDS]
        ]
        records_truncated = len(result.dns_records) > len(records)
        subdomains_truncated = len(result.subdomains) > MAX_DOMAIN_RECORDS
        summary = {
            "domain": domain,
            "subdomains": [
                sanitize_evidence_text(item, max_chars=253)
                for item in result.subdomains[:MAX_DOMAIN_RECORDS]
            ],
            "subdomains_truncated": subdomains_truncated,
            "dns_records": records,
            "records_truncated": records_truncated,
            "raw_sha256": result.raw_sha256,
        }
        limitations = _base_limitations(result.meta, ())
        if records_truncated or subdomains_truncated:
            limitations = _merge_limitations(
                limitations,
                (
                    "La liste des sous-domaines ou enregistrements DNS est tronquée "
                    "au plafond local.",
                ),
            )
        if not result.subdomains and not result.dns_records:
            ref, negative_output = await self._record_negative(
                context=context,
                object_type="domain",
                object_id=domain,
                object_values=(domain,),
                reason="Shodan n'a renvoyé aucun sous-domaine ni enregistrement pour ce domaine.",
                details={
                    "raw_sha256": result.raw_sha256,
                    "captured_at": _iso(result.meta.captured_at),
                    "credits_consumed": result.meta.credits_consumed,
                },
                limitations=limitations,
                meta=result.meta,
                observed_at=result.meta.captured_at,
            )
            return ToolResult(
                status=ToolInvocationStatus.EMPTY,
                observation_refs=(ref,),
                budget_consumed={BudgetDimension.SHODAN_CREDITS: result.meta.credits_consumed},
                output=negative_output,
                message="Aucun sous-domaine ou enregistrement trouvé dans la réponse Shodan.",
            )
        observation_refs, output, archive_partial = await self._record_observation(
            context=context,
            object_type="domain",
            object_id=domain,
            object_values=(domain,),
            meta=result.meta,
            summary=summary,
            limitations=limitations,
            raw_response=result.raw_response,
            raw_sha256=result.raw_sha256,
        )
        return ToolResult(
            status=(
                ToolInvocationStatus.PARTIAL
                if records_truncated or subdomains_truncated or archive_partial
                else ToolInvocationStatus.SUCCEEDED
            ),
            observation_refs=observation_refs,
            budget_consumed={BudgetDimension.SHODAN_CREDITS: result.meta.credits_consumed},
            output=output,
            message="Vue passive du domaine Shodan enregistrée.",
        )

    async def _not_found(
        self,
        context: ToolExecutionContext,
        domain: str,
        *,
        meta: ShodanObservationMeta | None = None,
    ) -> ToolResult:
        ref, output = await self._record_negative(
            context=context,
            object_type="domain",
            object_id=domain,
            object_values=(domain,),
            reason="Shodan n'a renvoyé aucune information pour ce domaine.",
            details={"http_status": 404},
            limitations=_base_limitations(meta, ()),
            meta=meta,
        )
        return ToolResult(
            status=ToolInvocationStatus.EMPTY,
            observation_refs=(ref,),
            budget_consumed={
                BudgetDimension.SHODAN_CREDITS: meta.credits_consumed if meta is not None else 0
            },
            output=output,
            message="Aucune information Shodan disponible pour ce domaine.",
        )


class ShodanCountToolAdapter(_BaseShodanToolAdapter):
    capability = ShodanCapability.COUNT

    def __init__(self, *, shodan_count: ShodanCountPort | None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._shodan_count = shodan_count

    async def execute(
        self, arguments: Mapping[str, Any], context: ToolExecutionContext
    ) -> ToolResult:
        preflight = await self._preflight(context, port_available=self._shodan_count is not None)
        if preflight is not None:
            return preflight
        try:
            query = validate_shodan_search_query(str(arguments["query"]))
            if _URL_PATTERN.search(query) or sanitize_evidence_text(query) != query:
                raise ValueError("Query cannot contain URLs or secret values")
            evidence_refs = await self._validate_evidence_refs(
                arguments.get("evidence_refs", []), context.investigation_id
            )
            justification = sanitize_evidence_text(arguments["justification"], max_chars=1_000)
            if not justification.strip():
                raise ValueError("A count justification is required")
        except (ShodanError, ValueError, KeyError, TypeError):
            return _local_result(
                ToolInvocationStatus.FAILED,
                "invalid_input",
                "La requête, la justification ou les références de preuve Shodan sont invalides.",
            )
        assert self._shodan_count is not None
        try:
            result = await self._shodan_count.count(query)
        except ShodanError as exc:
            if isinstance(exc, ShodanNotFoundError) or exc.status_code == 404:
                return await self._not_found(
                    context, query, object_values=(*_query_iocs(query), query)
                )
            return self._error_result(exc, object_type="query", object_id=query)
        if result.http_status == 404:
            return await self._not_found(
                context, query, meta=result.meta, object_values=(*_query_iocs(query), query)
            )
        summary = {
            "query": query,
            "justification": justification,
            "evidence_refs": [str(ref) for ref in evidence_refs],
            "total": result.total,
            "raw_sha256": result.raw_sha256,
        }
        limitations = _base_limitations(result.meta, ())
        if result.total == 0:
            ref, negative_output = await self._record_negative(
                context=context,
                object_type="query",
                object_id=query,
                object_values=(*_query_iocs(query), query),
                reason="Le comptage Shodan n'a renvoyé aucun match dans l'index consulté.",
                details={
                    "total": 0,
                    "raw_sha256": result.raw_sha256,
                    "captured_at": _iso(result.meta.captured_at),
                    "credits_consumed": result.meta.credits_consumed,
                },
                limitations=limitations,
                meta=result.meta,
                observed_at=result.meta.captured_at,
            )
            return ToolResult(
                status=ToolInvocationStatus.EMPTY,
                observation_refs=(ref,),
                budget_consumed={BudgetDimension.SHODAN_CREDITS: result.meta.credits_consumed},
                output=negative_output,
                message="Aucun match pour cette requête de comptage Shodan.",
            )
        observation_refs, output, archive_partial = await self._record_observation(
            context=context,
            object_type="query",
            object_id=query,
            object_values=(*_query_iocs(query), query),
            meta=result.meta,
            summary=summary,
            limitations=limitations,
            raw_response=result.raw_response,
            raw_sha256=result.raw_sha256,
        )
        return ToolResult(
            status=(
                ToolInvocationStatus.PARTIAL if archive_partial else ToolInvocationStatus.SUCCEEDED
            ),
            observation_refs=observation_refs,
            budget_consumed={BudgetDimension.SHODAN_CREDITS: result.meta.credits_consumed},
            output=output,
            message="Comptage Shodan enregistré.",
        )

    async def _not_found(
        self,
        context: ToolExecutionContext,
        query: str,
        *,
        meta: ShodanObservationMeta | None = None,
        object_values: tuple[str, ...] | None = None,
    ) -> ToolResult:
        ref, output = await self._record_negative(
            context=context,
            object_type="query",
            object_id=query,
            object_values=object_values or (*_query_iocs(query), query),
            reason="Shodan n'a renvoyé aucune information pour cette requête de comptage.",
            details={"http_status": 404},
            limitations=_base_limitations(meta, ()),
            meta=meta,
        )
        return ToolResult(
            status=ToolInvocationStatus.EMPTY,
            observation_refs=(ref,),
            budget_consumed={
                BudgetDimension.SHODAN_CREDITS: meta.credits_consumed if meta is not None else 0
            },
            output=output,
            message="Aucune information de comptage Shodan disponible.",
        )


def _tool_definition(
    capability: ShodanCapability, estimate: ShodanBudgetEstimate
) -> ToolDefinition:
    props: dict[str, Any]
    required: list[str]
    if capability is ShodanCapability.HOST:
        props = {"ip": {"type": "string", "minLength": 2, "maxLength": 45}}
        required = ["ip"]
    elif capability is ShodanCapability.DOMAIN:
        props = {"domain": {"type": "string", "minLength": 3, "maxLength": 253}}
        required = ["domain"]
    elif capability in {ShodanCapability.SEARCH, ShodanCapability.COUNT}:
        props = {
            "query": {"type": "string", "minLength": 1, "maxLength": 500},
            "justification": {"type": "string", "minLength": 1, "maxLength": 1_000},
            "evidence_refs": {
                "type": "array",
                "items": {"type": "string", "pattern": _UUID_PATTERN},
                "maxItems": MAX_EVIDENCE_REFS,
                "uniqueItems": True,
            },
        }
        required = ["query", "justification", "evidence_refs"]
        if capability is ShodanCapability.SEARCH:
            props["page"] = {"type": "integer", "minimum": 1, "maximum": 100}
    else:  # pragma: no cover - enum exhaustiveness
        raise ValueError(f"Unsupported Shodan capability: {capability}")
    return ToolDefinition(
        name=f"shodan.{capability.value}",
        schema_version=SCHEMA_VERSION,
        input_schema={
            "$schema": SCHEMA_DRAFT,
            "type": "object",
            "properties": props,
            "required": required,
            "additionalProperties": False,
        },
        effect=ToolEffect.EXTERNAL_READ,
        cost_model={BudgetDimension.SHODAN_CREDITS: estimate.credits_for(capability)},
        confidentiality=ToolConfidentiality.CONFIDENTIAL,
        limitations=(
            "Lecture passive de l'index Shodan ; aucun scan ni écriture.",
            _INDEX_LIMITATION,
            _SHARED_CERTIFICATE_LIMITATION,
            _TIME_WINDOW_LIMITATION,
        ),
        capability_key=SHODAN_TOOL_CAPABILITIES[capability],
    )


def _host_summary(view: ShodanHostView) -> tuple[dict[str, Any], tuple[str, ...]]:
    services: list[dict[str, Any]] = []
    limits: list[str] = []
    for service in view.services[:64]:
        banner, truncated = _bounded_banner(service)
        if truncated:
            limits.append("Les bannières sont tronquées explicitement dans le résumé borné.")
        age_seconds = (
            (view.meta.captured_at - service.timestamp).total_seconds()
            if service.timestamp is not None
            else None
        )
        services.append(
            {
                "port": service.port,
                "transport": _safe_text(service.transport, 32),
                "product": _safe_text(service.product, 128),
                "banner": banner,
                "banner_truncated": truncated,
                "timestamp": _iso(service.timestamp) if service.timestamp else None,
                "age_seconds_at_capture": age_seconds,
                "certificate": _certificate_summary(service),
            }
        )
    if len(view.services) > 64:
        limits.append("La liste des services est tronquée au plafond local de 64 entrées.")
    if len(view.hostnames) > 64:
        limits.append("La liste des noms d'hôte est tronquée au plafond local de 64 entrées.")
    if len(view.ports) > 64:
        limits.append("La liste des ports est tronquée au plafond local de 64 entrées.")
    if len(view.tags) > 32:
        limits.append("La liste des tags est tronquée au plafond local de 32 entrées.")
    return (
        {
            "ip": view.ip,
            "organization": _safe_text(view.organization, 256),
            "asn": _safe_text(view.asn, 64),
            "hostnames": [_safe_text(item, 253) for item in view.hostnames[:64]],
            "hostnames_truncated": len(view.hostnames) > 64,
            "ports": list(view.ports[:64]),
            "ports_truncated": len(view.ports) > 64,
            "tags": [_safe_text(item, 128) for item in view.tags[:32]],
            "tags_truncated": len(view.tags) > 32,
            "services": services,
            "services_truncated": len(view.services) > 64,
            "raw_sha256": view.raw_sha256,
        },
        tuple(limits),
    )


def _certificate_summary(service: ShodanService) -> dict[str, Any] | None:
    certificate = service.certificate
    if certificate is None:
        return None
    return {
        "fingerprint_sha256": certificate.fingerprint_sha256,
        "issuer": _safe_text(certificate.issuer, 256),
        "subject": _safe_text(certificate.subject, 256),
        "issued_at": _iso(certificate.issued_at) if certificate.issued_at else None,
        "expires_at": _iso(certificate.expires_at) if certificate.expires_at else None,
    }


def _bounded_banner(service: ShodanService) -> tuple[str | None, bool]:
    if service.banner is None:
        return None, service.banner_truncated
    value = _safe_text(service.banner, MAX_BANNER_SUMMARY_CHARS) or ""
    truncated = service.banner_truncated or len(service.banner) > MAX_BANNER_SUMMARY_CHARS
    if truncated:
        suffix = "…[bannière tronquée]"
        value = value[: max(0, MAX_BANNER_SUMMARY_CHARS - len(suffix))] + suffix
    return value, truncated


def _service_captures(meta: ShodanObservationMeta) -> list[dict[str, Any]]:
    captures: list[dict[str, Any]] = []
    for capture in meta.service_captures:
        age_seconds = (
            (meta.captured_at - capture.timestamp).total_seconds()
            if capture.timestamp is not None
            else None
        )
        captures.append(
            {
                "port": capture.port,
                "transport": _safe_text(capture.transport, 32),
                "timestamp": _iso(capture.timestamp) if capture.timestamp else None,
                "age_seconds_at_capture": age_seconds,
            }
        )
    return captures


def _base_limitations(
    meta: ShodanObservationMeta | None,
    tags: tuple[str, ...],
    *,
    shared_cert_hosts: int = 0,
    shared_asn_hosts: int = 0,
) -> tuple[str, ...]:
    limits = [_SHARED_CERTIFICATE_LIMITATION, _INDEX_LIMITATION, _TIME_WINDOW_LIMITATION]
    if meta is not None:
        limits.extend(meta.coverage_limits)
    normalized_tags = {tag.casefold() for tag in tags}
    if normalized_tags.intersection({"cdn", "cloud", "hosting", "cloudflare", "proxy"}):
        limits.append(
            "Les tags cloud/CDN de Shodan suggèrent un hébergement mutualisé "
            "ou un intermédiaire réseau."
        )
    if shared_cert_hosts > 1:
        limits.append(
            f"Le certificat apparaît sur {shared_cert_hosts} hôtes de la réponse consultée ; "
            "ce partage limite l'attribution."
        )
    if shared_asn_hosts > 1:
        limits.append(
            f"L'ASN apparaît sur {shared_asn_hosts} hôtes de la réponse consultée ; "
            "cet hébergement commun limite l'attribution."
        )
    return tuple(dict.fromkeys(limits))


def _shared_certificate_host_count(views: tuple[ShodanHostView, ...]) -> int:
    fingerprints: dict[str, set[str]] = {}
    for view in views:
        for service in view.services:
            fingerprint = service.certificate.fingerprint_sha256 if service.certificate else None
            if fingerprint:
                fingerprints.setdefault(fingerprint, set()).add(view.ip)
    return max((len(hosts) for hosts in fingerprints.values()), default=0)


def _shared_asn_host_count(views: tuple[ShodanHostView, ...]) -> int:
    by_asn: dict[str, set[str]] = {}
    for view in views:
        if view.asn:
            by_asn.setdefault(view.asn.casefold(), set()).add(view.ip)
    return max((len(hosts) for hosts in by_asn.values()), default=0)


def _merge_limitations(limitations: tuple[str, ...], extra: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys((*limitations, *extra)))


def _match_iocs(rows: object, object_values: tuple[str, ...], *, resolution: str) -> dict[str, Any]:
    targets = {_normalize_ioc(value) for value in object_values}
    targets.discard(None)
    matches: list[dict[str, str]] = []
    if isinstance(rows, (tuple, list)):
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            kind = str(row.get("artifact_type", row.get("type", ""))).casefold()
            if row.get("kind") != "documented_ioc" and "artifact_type" not in row:
                continue
            value = row.get("value")
            if not isinstance(value, str) or _normalize_ioc(value) not in targets:
                continue
            matches.append({"value": value, "ioc_type": kind or "unknown"})
    unique: dict[tuple[str, str], dict[str, str]] = {}
    for item in matches:
        unique[(item["ioc_type"], item["value"].casefold())] = item
    result = list(unique.values())
    return {
        "resolution": resolution,
        "present": bool(result),
        "matches": result[:MAX_EVIDENCE_REFS],
    }


def _normalize_ioc(value: str) -> str | None:
    candidate = value.strip().strip("[](){}<>,;\"'").rstrip(".")
    try:
        return str(ipaddress.ip_address(candidate))
    except ValueError:
        pass
    try:
        return validate_shodan_domain(candidate)
    except ShodanError:
        return None


def _query_iocs(query: str) -> tuple[str, ...]:
    candidates = (*_QUERY_IPV4.findall(query), *_QUERY_DOMAIN.findall(query))
    values: list[str] = []
    for candidate in candidates:
        normalized = _normalize_ioc(candidate)
        if normalized is not None and normalized not in values:
            values.append(normalized)
    return tuple(values)


def _iso(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("Shodan timestamps must be timezone-aware")
    return value.astimezone(UTC).isoformat()


def _safe_text(value: str | None, max_chars: int) -> str | None:
    if value is None:
        return None
    return sanitize_evidence_text(
        _BEARER_HEADER.sub("Bearer [redacted]", value), max_chars=max_chars
    )


def _sanitize_public_json(value: Any, *, depth: int = 0) -> Any:
    if depth > 16:
        return "[maximum depth omitted]"
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for index, (key, item) in enumerate(value.items()):
            if index >= MAX_PUBLIC_OBJECT_ITEMS:
                break
            safe_key = str(key)[:256]
            if _SECRET_FIELD.search(safe_key) or _BINARY_FIELD.search(safe_key):
                continue
            result[safe_key] = _sanitize_public_json(item, depth=depth + 1)
        return result
    if isinstance(value, list | tuple):
        return [
            _sanitize_public_json(item, depth=depth + 1) for item in value[:MAX_PUBLIC_ARRAY_ITEMS]
        ]
    if isinstance(value, str):
        return _safe_text(value, MAX_PUBLIC_TEXT_CHARS) or ""
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, bytes | bytearray | memoryview):
        return "[binary omitted]"
    return sanitize_evidence_text(value, max_chars=1_000)


def _local_result(
    status: ToolInvocationStatus,
    reason: str,
    message: str,
    *,
    alternatives: tuple[str, ...] = (),
    credits: int = 0,
    details: Mapping[str, Any] | None = None,
) -> ToolResult:
    return ToolResult(
        status=status,
        budget_consumed={BudgetDimension.SHODAN_CREDITS: credits},
        suggested_alternatives=alternatives,
        output={"reason": reason, **({"details": dict(details)} if details else {})},
        message=message,
    )


__all__ = [
    "MAX_BANNER_SUMMARY_CHARS",
    "SCHEMA_DRAFT",
    "SCHEMA_VERSION",
    "SHODAN_TOOL_CAPABILITIES",
    "ArticleSnapshotResolver",
    "ShodanCountToolAdapter",
    "ShodanDomainToolAdapter",
    "ShodanHostToolAdapter",
    "ShodanSearchToolAdapter",
    "register_shodan_tools",
    "shodan_tool_capabilities",
]
