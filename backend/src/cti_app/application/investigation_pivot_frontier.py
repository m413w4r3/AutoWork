"""Rebuild a bounded pivot graph from article identity and canonical evidence."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, replace
from enum import Enum
from typing import TYPE_CHECKING, Any, Literal, cast
from urllib.parse import urlsplit, urlunsplit

from cti_app.application.content_pivot_candidates import (
    candidate_frontier_limit,
    verify_content_pattern_candidates,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceObservation,
    InvestigationEnvelope,
    ToolInvocation,
    ToolInvocationStatus,
    thaw_json,
)
from cti_app.domain.investigation_pivot_frontier import (
    PIVOT_FRONTIER_SCHEMA_VERSION,
    PivotDisclosureState,
    PivotExecutionState,
    PivotFrontierEdge,
    PivotFrontierPath,
    PivotFrontierRecord,
    PivotFrontierV1,
    PivotG2State,
    PivotPriorityFactors,
    PivotQualificationLevel,
)

if TYPE_CHECKING:
    from cti_app.application.research_context_builder import ArticleSnapshot

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HASH_LENGTHS = {32: "md5", 40: "sha1", 64: "sha256"}
_DEFANG_DOT = re.compile(r"\s*(?:\[\.\]|\(\.\)|\{\.\})\s*", re.I)
_DEFANG_SCHEME = re.compile(r"^hxxps?://", re.I)
_MAX_FRONTIER_RECORDS = 2_000
PivotableIndicatorType = Literal["md5", "sha1", "sha256", "ipv4", "domain", "url"]
_PIVOTABLE_INDICATOR_TYPES = frozenset({"md5", "sha1", "sha256", "ipv4", "domain", "url"})
_BENIGN_BACKGROUND_INFRASTRUCTURE_LIST_VERSION = "2"
# Exact Cloudflare/Google/Quad9 resolver addresses and selected Microsoft,
# Windows Update, Skype, and Edge service suffixes. This context list is not an
# IOC denylist; article-documented indicators retain their article qualification.
_BENIGN_BACKGROUND_INFRASTRUCTURE_IPS = frozenset(
    {
        "1.0.0.1",
        "1.1.1.1",
        "8.8.4.4",
        "8.8.8.8",
        "9.9.9.9",
        "142.251.184.94",
    }
)
_BENIGN_BACKGROUND_INFRASTRUCTURE_DOMAINS = frozenset(
    {
        "c.pki.goog",
        "dns.google",
    }
)
_BENIGN_BACKGROUND_INFRASTRUCTURE_DOMAIN_SUFFIXES = frozenset(
    {
        "microsoft.com",
        "windowsupdate.com",
        "skype.com",
        "skype.net",
        "msedge.net",
        "edge.microsoft.com",
    }
)
# The observed Cloudflare address is not established as a public resolver. Keep
# this exact host pivotable and surface its possible shared hosting/CDN role.
_SHARED_HOSTING_CDN_POSSIBLE_IPS = frozenset({"162.159.36.2"})
_NON_PIVOTABLE_PRIORITY_ADJUSTMENT = -100


@dataclass(frozen=True, slots=True)
class NormalizedPivotIndicator:
    indicator_type: str
    value: str
    canonical_id: str


def normalize_pivot_indicator(
    indicator_type: str, raw_value: str
) -> NormalizedPivotIndicator | None:
    """Normalize only the closed IOC types accepted by the investigation tools."""
    if not isinstance(indicator_type, str) or not isinstance(raw_value, str):
        return None
    kind = indicator_type.strip().casefold()
    kind = {
        "file": "file",
        "files": "file",
        "md5": "file",
        "sha1": "file",
        "sha256": "file",
        "ip": "ip_address",
        "ip_addresses": "ip_address",
        "domains": "domain",
        "urls": "url",
    }.get(kind, kind)
    raw = raw_value.strip()
    try:
        if kind == "file":
            value = raw.casefold()
            if len(value) not in _HASH_LENGTHS or not re.fullmatch(r"[0-9a-fA-F]+", raw):
                return None
            family = _HASH_LENGTHS[len(value)]
            return NormalizedPivotIndicator(family, value, f"{family}:{value}")
        if kind == "ip_address":
            address = ipaddress.ip_address(raw)
            indicator_type = "ipv6" if address.version == 6 else "ipv4"
            value = address.compressed
            return NormalizedPivotIndicator(indicator_type, value, f"{indicator_type}:{value}")
        if kind == "ipv6":
            address = ipaddress.ip_address(raw)
            if address.version != 6:
                return None
            value = address.compressed
            return NormalizedPivotIndicator("ipv6", value, f"ipv6:{value}")
        if kind == "ipv4":
            value = ipaddress.ip_address(raw).compressed
            if ":" in value:
                return None
            return NormalizedPivotIndicator("ipv4", value, f"ipv4:{value}")
        if kind == "domain":
            canonical_domain = _normalize_domain_value(_DEFANG_DOT.sub(".", raw).rstrip("."))
            value = canonical_domain.replace(".", "[.]")
            return NormalizedPivotIndicator("domain", value, f"domain:{canonical_domain}")
        if kind == "url":
            if raw.startswith("url-sha256:"):
                digest = raw.removeprefix("url-sha256:").casefold()
                if _SHA256.fullmatch(digest) is None:
                    return None
                value = f"url-sha256:{digest}"
                canonical_digest = digest
            else:
                refanged = _DEFANG_SCHEME.sub(
                    lambda match: (
                        "https://" if match.group(0).casefold().startswith("hxxps") else "http://"
                    ),
                    raw,
                )
                refanged = _DEFANG_DOT.sub(".", refanged)
                normalized_url = _normalize_url_value(refanged)
                canonical_digest = hashlib.sha256(normalized_url.encode()).hexdigest()
                parts = urlsplit(normalized_url)
                display_host = parts.hostname or ""
                if ":" in display_host and not display_host.startswith("["):
                    display_host = f"[{display_host}]"
                if parts.port is not None:
                    display_host = f"{display_host}:{parts.port}"
                safe_scheme = "hxxps" if parts.scheme == "https" else "hxxp"
                safe_host = display_host.replace(".", "[.]")
                value = urlunsplit(
                    (safe_scheme, safe_host, parts.path, parts.query, parts.fragment)
                )
            return NormalizedPivotIndicator("url", value, f"url:{canonical_digest}")
    except (ValueError, TypeError):
        return None
    return None


def discovered_indicator_values(
    observations: Sequence[EvidenceObservation],
    payloads: Mapping[str, Mapping[str, Any] | None],
) -> tuple[tuple[PivotableIndicatorType, str], ...]:
    """Return actionable children; full frontier records retain classified observations."""
    values: dict[str, tuple[PivotableIndicatorType, str]] = {}
    for observation in observations:
        payload = payloads.get(str(observation.id))
        if not isinstance(payload, Mapping):
            continue
        for descriptor, _, _, _ in _summary_pivot_children(payload, observation):
            candidate = normalize_pivot_indicator(
                str(descriptor.get("type", "")), str(descriptor.get("identifier", ""))
            )
            if candidate is not None and candidate.indicator_type in _PIVOTABLE_INDICATOR_TYPES:
                if _classification_for_indicator(candidate.indicator_type, candidate.value)[1]:
                    continue
                indicator_type = cast(PivotableIndicatorType, candidate.indicator_type)
                values.setdefault(candidate.canonical_id, (indicator_type, candidate.value))
    return tuple(values[key] for key in sorted(values))


def build_pivot_frontier(
    *,
    investigation_id: str,
    snapshot: ArticleSnapshot,
    observations: Sequence[EvidenceObservation],
    observation_payloads: Mapping[str, Mapping[str, Any] | None],
    invocations: Sequence[ToolInvocation] = (),
    envelope: InvestigationEnvelope | None = None,
    g2_vt_authorized: bool | None = None,
    limit: int = _MAX_FRONTIER_RECORDS,
    cursor: str | None = None,
) -> PivotFrontierV1:
    """Create a deterministic graph projection; source rows remain canonical."""
    if limit < 1 or limit > _MAX_FRONTIER_RECORDS:
        raise ValueError("pivot frontier limit is outside its supported range")

    ordered_observations = tuple(
        sorted(observations, key=lambda item: (item.observed_at, item.id.hex))
    )
    observation_by_id = {str(item.id): item for item in ordered_observations}
    article_by_canonical: dict[str, NormalizedPivotIndicator] = {}
    records: dict[str, PivotFrontierRecord] = {}
    record_by_canonical: dict[str, list[str]] = {}
    edges: dict[str, PivotFrontierEdge] = {}
    children: dict[str, set[str]] = {}
    roots: list[str] = []
    content_candidate_order: dict[str, tuple[int, int, int, int, str]] = {}
    verified: set[str] = set()
    source_digests = [snapshot.synthesis_sha256]

    for item in snapshot.confirmed_iocs:
        normalized = normalize_pivot_indicator(item.indicator_type, item.value)
        if normalized is None:
            continue
        article_by_canonical[normalized.canonical_id] = normalized
        root_id = _stable_id(
            "pivot",
            normalized.canonical_id,
            str(item.extraction_artifact_id),
            str(item.extraction_artifact_version),
            ",".join(sorted(str(value) for value in item.source_document_ids)),
            "article",
        )
        factors = _priority_factors(novel=False, verified=True, depth=0, cost=0)
        record = PivotFrontierRecord(
            id=root_id,
            canonical_id=normalized.canonical_id,
            indicator_type=normalized.indicator_type,
            value=normalized.value,
            parent_id=None,
            path=(root_id,),
            source_observation_id=str(item.extraction_artifact_id),
            source="article",
            source_version=str(item.extraction_artifact_version),
            source_sha256=snapshot.synthesis_sha256,
            level=PivotQualificationLevel.DOCUMENTED,
            reason="documented_in_article",
            confidence=1.0,
            limitations=(
                "IOC documenté dans l'article; cela ne vérifie pas une relation de famille.",
            ),
            novelty_vs_article=False,
            novelty_vs_verified=True,
            estimated_cost=0,
            g2_state=PivotG2State.NOT_APPLICABLE,
            disclosure_state=PivotDisclosureState.NOT_APPLICABLE,
            priority_score=_priority_score(factors),
            priority_factors=factors,
            state=PivotExecutionState.QUEUED,
            state_reason="article_seed",
            cursor=None,
            pagination_complete=None,
            children_observed=(),
        )
        records[root_id] = record
        record_by_canonical.setdefault(normalized.canonical_id, []).append(root_id)
        roots.append(root_id)

    for observation in ordered_observations:
        payload = observation_payloads.get(str(observation.id))
        if (
            observation.source.casefold().startswith("virustotal")
            and observation.kind.value == "vt_file_report"
        ):
            direct = normalize_pivot_indicator("file", observation.object_id)
            if direct is not None:
                verified.add(direct.canonical_id)
        if not isinstance(payload, Mapping):
            if observation.kind.value == "vt_relationship":
                parent = normalize_pivot_indicator(observation.object_type, observation.object_id)
                if parent is not None:
                    limitation = (
                        f"Observation VT {observation.id} omise de la frontière: "
                        "son résumé JSON borné est indisponible; elle n'est pas utilisée "
                        "comme preuve ni comme pivot."
                    )
                    for root_id in record_by_canonical.get(parent.canonical_id, ()):
                        if root_id not in roots:
                            continue
                        root = records[root_id]
                        if limitation not in root.limitations:
                            records[root_id] = replace(
                                root, limitations=(*root.limitations, limitation)
                            )
            continue
        source_digests.append(observation.sha256)
        relation = str(payload.get("relation", ""))
        parent_records = _observation_parent_records(
            observation,
            payload,
            record_by_canonical=record_by_canonical,
            observations_by_id=observation_by_id,
        )
        for descriptor, level, reason, confidence in _summary_pivot_children(payload, observation):
            child = normalize_pivot_indicator(
                str(descriptor.get("type", "")), str(descriptor.get("identifier", ""))
            )
            if child is None:
                continue
            for parent_id in parent_records or (None,):
                path = (
                    (
                        *records[parent_id].path,
                        _stable_id(
                            "pivot",
                            child.canonical_id,
                            observation.source.casefold(),
                            observation.source_version,
                            reason,
                            parent_id or "root",
                        ),
                    )
                    if parent_id
                    else (
                        _stable_id(
                            "pivot",
                            child.canonical_id,
                            observation.source.casefold(),
                            observation.source_version,
                            reason,
                            "root",
                        ),
                    )
                )
                record_id = path[-1]
                novelty = child.canonical_id not in article_by_canonical
                is_verified = child.canonical_id in verified
                depth = max(1, len(path) - 1)
                cost = 1
                factors = _priority_factors(
                    novel=novelty, verified=not is_verified, depth=depth, cost=cost
                )
                state, state_reason, state_cursor, complete = _source_state(payload)
                g2, disclosure = _policy_states(snapshot, envelope, g2_vt_authorized)
                limitations = tuple(
                    dict.fromkeys(
                        (
                            *observation.limitations,
                            "Une observation de source unique ne confirme pas une parenté "
                            "de famille.",
                            "Un nom de fichier ou une métadonnée fournisseur ne constitue pas "
                            "une preuve de famille.",
                            "Une capture réseau ou une comparaison de similarité seule ne confirme "
                            "pas une parenté de famille.",
                        )
                    )
                )
                record = PivotFrontierRecord(
                    id=record_id,
                    canonical_id=child.canonical_id,
                    indicator_type=child.indicator_type,
                    value=child.value,
                    parent_id=parent_id,
                    path=path,
                    source_observation_id=str(observation.id),
                    source=observation.source,
                    source_version=observation.source_version,
                    source_sha256=observation.sha256,
                    level=level,
                    reason=reason,
                    confidence=confidence,
                    limitations=limitations,
                    novelty_vs_article=novelty,
                    novelty_vs_verified=not is_verified,
                    estimated_cost=cost,
                    g2_state=g2,
                    disclosure_state=disclosure,
                    priority_score=_priority_score(factors),
                    priority_factors=factors,
                    state=state,
                    state_reason=state_reason,
                    cursor=state_cursor,
                    pagination_complete=complete,
                    children_observed=(),
                )
                existing = records.get(record_id)
                if existing is None:
                    records[record_id] = record
                    record_by_canonical.setdefault(child.canonical_id, []).append(record_id)
                if parent_id is not None:
                    children.setdefault(parent_id, set()).add(record_id)
                    edge_id = _stable_id(
                        "edge", parent_id, record_id, str(observation.id), relation or reason
                    )
                    edges.setdefault(
                        edge_id,
                        PivotFrontierEdge(
                            id=edge_id,
                            parent_id=parent_id,
                            child_id=record_id,
                            relation=relation or reason,
                            source_observation_id=str(observation.id),
                            source_version=observation.source_version,
                            source_sha256=observation.sha256,
                            reason=reason,
                            confidence=confidence,
                            limitations=limitations,
                        ),
                    )

    content_candidates = verify_content_pattern_candidates(
        ordered_observations,
        observation_payloads,
    )
    if content_candidates:
        g2_state, disclosure_state = _policy_states(snapshot, envelope, g2_vt_authorized)
        for candidate in content_candidates:
            primary = candidate.measurements[0]
            record_id = _stable_id("content_pattern", candidate.id)
            if g2_state is not PivotG2State.APPROVED:
                state = PivotExecutionState.BLOCKED
                state_reason = "g2_approval_required"
            elif disclosure_state is not PivotDisclosureState.ALLOWED:
                state = PivotExecutionState.BLOCKED
                state_reason = "external_disclosure_blocked"
            elif envelope is not None and envelope.budget.remaining[BudgetDimension.VT_UNITS] == 0:
                state = PivotExecutionState.BLOCKED
                state_reason = "vt_budget_exhausted"
            else:
                state = PivotExecutionState.QUEUED
                state_reason = "operator_selection_required; generation_does_not_execute"
            bridge_measurements = tuple(
                "mesure_bridge="
                + json.dumps(
                    {
                        "kind": item.bridge_kind,
                        "value": item.bridge_value,
                        "offset": item.offset,
                        "length": item.length,
                        "sample_sha256": item.sample_sha256,
                        "observation_id": item.observation_id,
                        "source_file": item.source_file,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                for item in candidate.measurements[:8]
            )
            limitations = (
                *bridge_measurements,
                "evidence_refs=" + json.dumps(candidate.evidence_refs, separators=(",", ":")),
                "Exécution VT passive: l'opérateur choisit le pivot; query, G2, TLP et budget "
                "sont revérifiés à l'exécution.",
            )
            factors = _priority_factors(novel=True, verified=True, depth=1, cost=1)
            record = PivotFrontierRecord(
                id=record_id,
                canonical_id=f"content_pattern:{candidate.id}",
                indicator_type="content_pattern",
                value=candidate.query,
                parent_id=None,
                path=(record_id,),
                source_observation_id=primary.observation_id,
                source=primary.source,
                source_version=primary.source_version,
                source_sha256=primary.sample_sha256,
                level=PivotQualificationLevel.CANDIDATE,
                reason=candidate.justification,
                confidence=0.4,
                limitations=limitations,
                novelty_vs_article=True,
                novelty_vs_verified=True,
                estimated_cost=1,
                g2_state=g2_state,
                disclosure_state=disclosure_state,
                priority_score=_priority_score(factors),
                priority_factors=factors,
                state=state,
                state_reason=state_reason,
                cursor=None,
                pagination_complete=None,
                children_observed=(),
            )
            records[record_id] = record
            content_candidate_order[record_id] = candidate.rank_key
            roots.append(record_id)

    for invocation in sorted(invocations, key=lambda item: (item.updated_at, item.id.hex)):
        source_digests.append(
            hashlib.sha256(
                f"{invocation.id}:{invocation.version}:{invocation.status.value}".encode()
            ).hexdigest()
        )
        state, reason, cursor_value, complete = _invocation_state(
            invocation, ordered_observations, observation_payloads
        )
        params = thaw_json(invocation.normalized_parameters)
        for record_id, record in tuple(records.items()):
            if not _invocation_matches(invocation.tool_name, params, record):
                continue
            records[record_id] = replace(
                record,
                state=state,
                state_reason=reason,
                cursor=cursor_value or record.cursor,
                pagination_complete=complete
                if complete is not None
                else record.pagination_complete,
            )

    for record_id, record in tuple(records.items()):
        novel = record.canonical_id not in article_by_canonical
        novelty_verified = record.canonical_id not in verified
        (
            classified_level,
            classification_reason,
            classification_limitation,
            classification_adjustment,
        ) = _classification_for_record(record, article_by_canonical)
        reason = record.reason
        limitations = record.limitations
        if classification_reason is not None:
            reason = f"{reason}; {classification_reason}"
        if classification_limitation is not None:
            limitations = tuple(dict.fromkeys((*limitations, classification_limitation)))
        factors = _priority_factors(
            novel=novel,
            verified=novelty_verified,
            depth=max(1, len(record.path) - 1),
            cost=record.estimated_cost,
            classification_adjustment=classification_adjustment,
        )
        records[record_id] = replace(
            record,
            level=classified_level or record.level,
            reason=reason,
            limitations=limitations,
            children_observed=tuple(sorted(children.get(record_id, ()))),
            novelty_vs_article=novel,
            novelty_vs_verified=novelty_verified,
            priority_factors=factors,
            priority_score=_priority_score(
                factors, classification_adjustment=classification_adjustment
            ),
        )

    roots.extend(
        record.id
        for record in records.values()
        if record.parent_id is None and record.id not in roots
    )
    all_records = tuple(
        sorted(
            records.values(),
            key=lambda item: (
                (0, *content_candidate_order[item.id])
                if item.id in content_candidate_order
                else (1, item.id)
            ),
        )
    )
    all_edges = tuple(edges[key] for key in sorted(edges))
    all_paths = _paths(roots, records, all_edges)
    source_sha256 = hashlib.sha256("\n".join(sorted(source_digests)).encode()).hexdigest()
    start = 0
    if cursor is not None:
        matches = [index for index, item in enumerate(all_records) if item.id == cursor]
        if not matches:
            raise ValueError("pivot frontier cursor is not present in this projection")
        start = matches[0] + 1
    page_limit = candidate_frontier_limit(
        limit,
        has_content_candidates=bool(content_candidate_order),
    )
    page = all_records[start : start + page_limit]
    has_more = start + page_limit < len(all_records)
    next_cursor = page[-1].id if has_more and page else None
    included = {item.id for item in page}
    page_edges = tuple(
        edge for edge in all_edges if edge.parent_id in included or edge.child_id in included
    )
    page_paths = tuple(path for path in all_paths if path.target_id in included)
    frontier_identity = {
        "schema_version": PIVOT_FRONTIER_SCHEMA_VERSION,
        "background_infrastructure_list_version": (_BENIGN_BACKGROUND_INFRASTRUCTURE_LIST_VERSION),
        "investigation_id": investigation_id,
        "source_sha256": source_sha256,
        "records": [item.id for item in all_records],
        "edges": [item.id for item in all_edges],
    }
    return PivotFrontierV1(
        schema_version=PIVOT_FRONTIER_SCHEMA_VERSION,
        investigation_id=investigation_id,
        source_sha256=source_sha256,
        frontier_sha256=hashlib.sha256(_canonical_json(frontier_identity)).hexdigest(),
        records=page,
        edges=page_edges,
        paths=page_paths,
        truncated=has_more,
        next_cursor=next_cursor,
    )


def frontier_to_dict(frontier: PivotFrontierV1) -> dict[str, Any]:
    return cast(dict[str, Any], _json_ready(asdict(frontier)))


def _summary_pivot_children(
    payload: Mapping[str, Any],
    observation: EvidenceObservation,
) -> tuple[tuple[Mapping[str, Any], PivotQualificationLevel, str, float], ...]:
    result: list[tuple[Mapping[str, Any], PivotQualificationLevel, str, float]] = []
    if payload.get("schema") == "virustotal-investigation-summary.v1":
        candidate = payload.get("candidate")
        if isinstance(candidate, Mapping):
            result.append(
                (
                    candidate,
                    PivotQualificationLevel.CANDIDATE,
                    "candidate_from_vt_intelligence_search",
                    0.25,
                )
            )
        descriptors = payload.get("descriptors")
        if observation.object_type.casefold() in {"virustotal_search", "query"}:
            return tuple(result)
        relation = str(payload.get("relation", "")).strip()
        if isinstance(descriptors, Sequence) and not isinstance(descriptors, (str, bytes)):
            for descriptor in descriptors:
                if isinstance(descriptor, Mapping):
                    result.append(
                        (
                            descriptor,
                            PivotQualificationLevel.DISCOVERED,
                            f"vt_relationship:{relation or 'unspecified'}",
                            0.35,
                        )
                    )
        return tuple(result)

    if payload.get("schema") == "NetworkCaptureReportV1":
        for item in _mapping_items(payload.get("endpoints")):
            address = item.get("address")
            if isinstance(address, str):
                result.append(
                    (
                        {"type": "ip_address", "identifier": address},
                        PivotQualificationLevel.DISCOVERED,
                        "network_capture:endpoint",
                        0.55,
                    )
                )
        for field, relation in (
            ("dns_observations", "dns.query"),
            ("http_observations", "http.host"),
            ("tls_observations", "tls.sni"),
        ):
            for item in _mapping_items(payload.get(field)):
                if item.get("name") == relation and isinstance(item.get("value"), str):
                    result.append(
                        (
                            {"type": "domain", "identifier": item["value"]},
                            PivotQualificationLevel.DISCOVERED,
                            f"network_capture:{relation}",
                            0.55,
                        )
                    )
        return tuple(result)

    if (
        observation.source == "sample_similarity"
        and payload.get("schema_version") == "family-feature-comparison-v1"
    ):
        sample_hashes = payload.get("comparison_sample_sha256s")
        if isinstance(sample_hashes, Sequence) and not isinstance(sample_hashes, (str, bytes)):
            for sample_hash in sample_hashes:
                if isinstance(sample_hash, str):
                    result.append(
                        (
                            {"type": "sha256", "identifier": sample_hash},
                            PivotQualificationLevel.ASSOCIATED,
                            "sample_similarity:comparison_input",
                            0.45,
                        )
                    )
        return tuple(result)
    return ()


def _mapping_items(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _observation_parent_records(
    observation: EvidenceObservation,
    payload: Mapping[str, Any],
    *,
    record_by_canonical: Mapping[str, Sequence[str]],
    observations_by_id: Mapping[str, EvidenceObservation],
) -> tuple[str, ...]:
    if payload.get("schema") != "virustotal-investigation-summary.v1":
        return ()
    if isinstance(payload.get("candidate"), Mapping) or observation.object_type.casefold() in {
        "virustotal_search",
        "query",
    }:
        candidate_parents: list[str] = []
        query = payload.get("effective_query")
        if isinstance(query, str):
            normalized_query = _normalize_any_indicator(query)
            if normalized_query is not None:
                candidate_parents.extend(record_by_canonical.get(normalized_query.canonical_id, ()))
        refs = payload.get("evidence_refs")
        if isinstance(refs, Sequence) and not isinstance(refs, (str, bytes)):
            for reference in refs:
                source = observations_by_id.get(str(reference))
                if source is None:
                    continue
                normalized_source = normalize_pivot_indicator(source.object_type, source.object_id)
                if normalized_source is not None:
                    candidate_parents.extend(
                        record_by_canonical.get(normalized_source.canonical_id, ())
                    )
        return tuple(dict.fromkeys(candidate_parents))
    parent = normalize_pivot_indicator(observation.object_type, observation.object_id)
    return tuple(record_by_canonical.get(parent.canonical_id, ())) if parent else ()


def _normalize_any_indicator(value: str) -> NormalizedPivotIndicator | None:
    for kind in ("file", "ip_address", "domain", "url"):
        normalized = normalize_pivot_indicator(kind, value)
        if normalized is not None:
            return normalized
    return None


def _source_state(
    payload: Mapping[str, Any],
) -> tuple[PivotExecutionState, str, str | None, bool | None]:
    complete = payload.get("pagination_complete")
    if not isinstance(complete, bool):
        complete = (
            payload.get("exhaustive") if isinstance(payload.get("exhaustive"), bool) else None
        )
    next_cursor = payload.get("next_cursor")
    cursor = _safe_cursor_value(next_cursor)
    if cursor is not None:
        return PivotExecutionState.QUEUED, "pagination_incomplete", cursor, False
    if payload.get("observed_count") == 0:
        return (
            PivotExecutionState.BLOCKED,
            "provider_returned_empty_result; investigation remains open",
            None,
            False,
        )
    reason = (
        "source_page_complete; direct_pivot_report_queued"
        if complete is True
        else "provider_exposure_is_not_family_confirmation"
    )
    return PivotExecutionState.QUEUED, reason, None, complete


def _invocation_state(
    invocation: ToolInvocation,
    observations: Sequence[EvidenceObservation],
    payloads: Mapping[str, Mapping[str, Any] | None],
) -> tuple[PivotExecutionState, str, str | None, bool | None]:
    status = invocation.status
    related_payloads = [
        payloads.get(str(reference))
        for reference in invocation.response_refs
        if payloads.get(str(reference)) is not None
    ]
    if not related_payloads:
        related_payloads = [
            payloads.get(str(item.id))
            for item in observations
            if item.invocation_ref == invocation.id and payloads.get(str(item.id)) is not None
        ]
    outputs: list[Mapping[str, Any]] = []
    for payload in related_payloads:
        if isinstance(payload, Mapping):
            result = payload.get("result")
            if isinstance(result, Mapping):
                output = result.get("output")
                if isinstance(output, Mapping):
                    outputs.append(output)
            output = payload.get("output")
            if isinstance(output, Mapping):
                outputs.append(output)
            elif any(
                key in payload
                for key in ("next_cursor", "pagination_complete", "exhaustive", "error_code")
            ):
                outputs.append(payload)
    output = outputs[-1] if outputs else {}
    next_cursor = output.get("next_cursor")
    cursor = _safe_cursor_value(next_cursor)
    complete = output.get("exhaustive")
    if not isinstance(complete, bool):
        complete = (
            output.get("pagination_complete")
            if isinstance(output.get("pagination_complete"), bool)
            else None
        )
    if status in {
        ToolInvocationStatus.PENDING,
        ToolInvocationStatus.RUNNING,
        ToolInvocationStatus.PARTIAL,
    }:
        return PivotExecutionState.QUEUED, "invocation_pending_or_page_limit", cursor, complete
    if status in {
        ToolInvocationStatus.UNAVAILABLE,
        ToolInvocationStatus.POLICY_DENIED,
        ToolInvocationStatus.FAILED,
        ToolInvocationStatus.NEEDS_REVIEW,
        ToolInvocationStatus.EMPTY,
    }:
        detail = _invocation_reason(related_payloads)
        reason = detail or f"invocation_{status.value}; retry_or_operator_review_required"
        return PivotExecutionState.BLOCKED, reason, cursor, False
    if cursor is not None or complete is False:
        return (
            PivotExecutionState.QUEUED,
            "pagination_incomplete; continue_from_cursor",
            cursor,
            False,
        )
    if complete is True:
        return PivotExecutionState.EXHAUSTED, "provider_reported_complete_pagination", None, True
    return (
        PivotExecutionState.EXECUTED,
        "tool_executed; provider_completeness_not_proven",
        cursor,
        complete,
    )


def _invocation_matches(
    tool_name: str, params: Mapping[str, Any], record: PivotFrontierRecord
) -> bool:
    if tool_name == "vt.intelligence_search":
        query = params.get("query")
        normalized_query = _normalize_any_indicator(query) if isinstance(query, str) else None
        return normalized_query is not None and normalized_query.canonical_id == record.canonical_id
    if tool_name == "vt.file_report":
        target = params.get("file_hash")
        normalized = normalize_pivot_indicator("file", target) if isinstance(target, str) else None
        return normalized is not None and normalized.canonical_id == record.canonical_id
    if tool_name == "vt.object_report":
        target = params.get("object_id")
        target_type = params.get("object_type")
        normalized = normalize_pivot_indicator(str(target_type or ""), str(target or ""))
        return normalized is not None and normalized.canonical_id == record.canonical_id
    if tool_name == "vt.relationships":
        target = params.get("object_id")
        target_type = params.get("object_type")
        normalized = normalize_pivot_indicator(str(target_type or ""), str(target or ""))
        return normalized is not None and normalized.canonical_id == record.canonical_id
    return False


def _invocation_reason(payloads: Sequence[Any]) -> str | None:
    for payload in reversed(payloads):
        if not isinstance(payload, Mapping):
            continue
        result = payload.get("result")
        result = result if isinstance(result, Mapping) else payload
        output = result.get("output")
        output = output if isinstance(output, Mapping) else {}
        for key in ("reason", "error_code", "detail_code", "provider_error", "status"):
            value = result.get(key) or output.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()[:512]
        http_status = result.get("http_status") or output.get("http_status")
        if isinstance(http_status, int) and not isinstance(http_status, bool):
            return f"provider_http_status:{http_status}"
    return None


def _safe_cursor_value(value: Any) -> str | None:
    if not isinstance(value, str) or not value or len(value) > 2_048:
        return None
    if any(ord(character) < 32 for character in value):
        return None
    lowered = value.casefold()
    if "http://" in lowered or "https://" in lowered or "token=" in lowered:
        return None
    return value


def _policy_states(
    snapshot: ArticleSnapshot,
    envelope: InvestigationEnvelope | None,
    g2_vt_authorized: bool | None = None,
) -> tuple[PivotG2State, PivotDisclosureState]:
    # Derived records use the same envelope and snapshot bounds; these states
    # do not depend on whether a derived indicator is documented or benign.
    has_vt_provider = envelope is not None and any(
        provider.casefold() in {"vt", "virustotal"} for provider in envelope.allowed_providers
    )
    allows_vt = (
        g2_vt_authorized
        if g2_vt_authorized is not None
        else envelope is not None and envelope.acquire_hits_allowed and has_vt_provider
    )
    g2 = (
        PivotG2State.REQUIRED
        if envelope is None
        else PivotG2State.APPROVED
        if allows_vt
        else PivotG2State.BLOCKED
    )
    if envelope is None:
        return g2, PivotDisclosureState.UNKNOWN
    tlp = envelope.disclosure.tlp.value if envelope.disclosure.tlp is not None else ""
    allowed = (
        allows_vt
        and tlp not in {"amber", "red"}
        and (snapshot.tlp or "").strip().casefold().removeprefix("tlp:") not in {"amber", "red"}
    )
    return g2, PivotDisclosureState.ALLOWED if allowed else PivotDisclosureState.BLOCKED


def _priority_factors(
    *, novel: bool, verified: bool, depth: int, cost: int, classification_adjustment: int = 0
) -> PivotPriorityFactors:
    formula = "clamp(50*article_novelty + 30*verified_novelty + 5/depth - 5*cost, 0, 100)"
    if classification_adjustment:
        formula = (
            "clamp(50*article_novelty + 30*verified_novelty + 5/depth - 5*cost "
            f"+ classification_adjustment({classification_adjustment}), 0, 100)"
        )
    return PivotPriorityFactors(
        novelty_vs_article=1 if novel else 0,
        novelty_vs_verified=1 if verified else 0,
        depth=max(1, depth),
        cost=max(0, cost),
        formula=formula,
    )


def _priority_score(factors: PivotPriorityFactors, *, classification_adjustment: int = 0) -> int:
    score = (
        50 * factors.novelty_vs_article
        + 30 * factors.novelty_vs_verified
        + round(5 / factors.depth)
        - 5 * factors.cost
        + classification_adjustment
    )
    return max(0, min(100, score))


def _classification_for_record(
    record: PivotFrontierRecord,
    documented_indicators: Mapping[str, NormalizedPivotIndicator],
) -> tuple[PivotQualificationLevel | None, str | None, str | None, int]:
    """Classify non-global IPs and exact known background infrastructure.

    Article-documented indicators retain their article qualification even when
    the same canonical value also appears in a derived observation.
    """
    if record.source == "article" or record.canonical_id in documented_indicators:
        return None, None, None, 0

    return _classification_for_indicator(record.indicator_type, record.value)


def _classification_for_indicator(
    indicator_type: str, value: str
) -> tuple[PivotQualificationLevel | None, str | None, str | None, int]:
    """Return deterministic background classification and its score effect."""
    host = _indicator_host(indicator_type, value)
    if host is None:
        return None, None, None, 0
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        if host not in _BENIGN_BACKGROUND_INFRASTRUCTURE_DOMAINS and not any(
            host == suffix or host.endswith(f".{suffix}")
            for suffix in _BENIGN_BACKGROUND_INFRASTRUCTURE_DOMAIN_SUFFIXES
        ):
            return None, None, None, 0
    else:
        if not address.is_global:
            return (
                PivotQualificationLevel.REJECTED,
                "non_pivotable_non_global_ip_address",
                "Adresse IP non globale (privée, locale, link-local, partagée, documentaire "
                "ou réservée selon ipaddress); aucun pivot externe n'est proposé.",
                _NON_PIVOTABLE_PRIORITY_ADJUSTMENT,
            )
        if address.compressed not in _BENIGN_BACKGROUND_INFRASTRUCTURE_IPS:
            if address.compressed in _SHARED_HOSTING_CDN_POSSIBLE_IPS:
                return (
                    None,
                    None,
                    "Adresse d'hébergement mutualisé/CDN possible (plage Cloudflare); "
                    "le rôle DNS de cette adresse n'est pas établi et elle reste pivotable.",
                    0,
                )
            return None, None, None, 0

    return (
        PivotQualificationLevel.REJECTED,
        "benign_background_infrastructure:"
        f"list-v{_BENIGN_BACKGROUND_INFRASTRUCTURE_LIST_VERSION}:not_pivotable",
        "Infrastructure publique de fond reconnue par la liste exacte et versionnée "
        f"v{_BENIGN_BACKGROUND_INFRASTRUCTURE_LIST_VERSION}; conservée visible pour le contexte, "
        "mais non pivotable. Ce classement ne signifie pas malveillance ni innocuité absolue.",
        _NON_PIVOTABLE_PRIORITY_ADJUSTMENT,
    )


def _indicator_host(indicator_type: str, value: str) -> str | None:
    if indicator_type in {"ipv4", "ipv6"}:
        return value
    if indicator_type == "domain":
        return value.replace("[.]", ".").casefold()
    if indicator_type != "url":
        return None
    value = _DEFANG_DOT.sub(".", value)
    value = _DEFANG_SCHEME.sub(
        lambda match: "https://" if match.group(0).casefold().startswith("hxxps") else "http://",
        value,
    )
    try:
        return (urlsplit(value).hostname or "").casefold() or None
    except ValueError:
        return None


def _paths(
    roots: Sequence[str],
    records: Mapping[str, PivotFrontierRecord],
    edges: Sequence[PivotFrontierEdge],
) -> tuple[PivotFrontierPath, ...]:
    paths: list[PivotFrontierPath] = []
    edge_by_child: dict[str, list[PivotFrontierEdge]] = {}
    for edge in edges:
        edge_by_child.setdefault(edge.child_id, []).append(edge)
    for record in records.values():
        root = record.path[0]
        if root not in roots:
            continue
        edge_path = []
        for left, right in zip(record.path, record.path[1:], strict=False):
            found = next(
                (item for item in edge_by_child.get(right, ()) if item.parent_id == left), None
            )
            if found is not None:
                edge_path.append(found.id)
        path_id = _stable_id("path", *record.path, *edge_path)
        paths.append(
            PivotFrontierPath(
                id=path_id,
                root_id=root,
                target_id=record.id,
                node_ids=record.path,
                edge_ids=tuple(edge_path),
            )
        )
    return tuple(sorted(paths, key=lambda item: (len(item.node_ids), item.id)))


def _stable_id(*parts: str) -> str:
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def _canonical_json(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _json_ready(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {str(key): _json_ready(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_json_ready(item) for item in value]
    return value


def _normalize_domain_value(value: str) -> str:
    candidate = value.strip().rstrip(".").casefold()
    if not candidate or len(candidate) > 253 or ":" in candidate or "/" in candidate:
        raise ValueError("invalid domain")
    ascii_name = candidate.encode("idna").decode("ascii")
    labels = ascii_name.split(".")
    if len(labels) < 2 or any(
        not label
        or len(label) > 63
        or label.startswith("-")
        or label.endswith("-")
        or not re.fullmatch(r"[a-z0-9-]+", label)
        for label in labels
    ):
        raise ValueError("invalid domain")
    return ascii_name


def _normalize_url_value(value: str) -> str:
    parts = urlsplit(value.strip())
    if parts.scheme.casefold() not in {"http", "https"} or not parts.hostname:
        raise ValueError("invalid URL")
    if parts.username is not None or parts.password is not None:
        raise ValueError("URL credentials are not allowed")
    host = parts.hostname.encode("idna").decode("ascii").casefold()
    if ":" in host:
        host = f"[{host}]"
    port = parts.port
    if port is not None and not (
        (parts.scheme.casefold() == "http" and port == 80)
        or (parts.scheme.casefold() == "https" and port == 443)
    ):
        host = f"{host}:{port}"
    return urlunsplit(
        (parts.scheme.casefold(), host, parts.path or "/", parts.query, parts.fragment)
    )
