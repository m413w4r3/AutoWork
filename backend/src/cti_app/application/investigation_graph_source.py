"""Read canonical investigation evidence and project typed infrastructure relations."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections import defaultdict, deque
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from uuid import UUID

from cti_app.application.investigation_persistence import (
    EvidenceObservationRepository,
    FindingRepository,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationFinding,
)
from cti_app.domain.investigation_graph import (
    InvestigationGraph,
    InvestigationGraphConfidence,
    InvestigationGraphDirection,
    InvestigationGraphEdge,
    InvestigationGraphEvidenceBasis,
    InvestigationGraphEvidenceRef,
    InvestigationGraphNode,
    InvestigationGraphNodeKind,
)
from cti_app.domain.investigation_graph import (
    InvestigationGraphSource as InvestigationGraphSourceKind,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationIOCDisposition,
    publication_document_v5_sha256,
)

_MAX_EVIDENCE_BYTES = 128 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SENSITIVE_QUERY_KEYS = frozenset(
    {
        "access_token",
        "api_key",
        "apikey",
        "auth",
        "authorization",
        "credential",
        "key",
        "password",
        "secret",
        "sig",
        "signature",
        "token",
        "x-amz-signature",
        "x-goog-signature",
    }
)
_NODE_KIND_ORDER = {
    InvestigationGraphNodeKind.FILE_SHA256: 0,
    InvestigationGraphNodeKind.ARTICLE: 1,
    InvestigationGraphNodeKind.DOMAIN: 2,
    InvestigationGraphNodeKind.IP: 3,
    InvestigationGraphNodeKind.URL: 4,
    InvestigationGraphNodeKind.OMITTED: 5,
}
_VT_FILE_OUTBOUND = frozenset(
    {
        "contacted_domains",
        "contacted_ips",
        "contacted_urls",
        "dropped_files",
        "downloaded_files",
    }
)
_VT_FILE_REVERSED = frozenset({"execution_parents", "referrer_files"})
_VT_FILE_UNDIRECTED = frozenset({"bundled_files", "communicating_files", "related_files"})
_VT_REVERSED_FROM_NON_FILE = frozenset(
    {"communicating_files", "downloaded_files", "execution_parents", "referrer_files"}
)


class CanonicalEvidenceReader(Protocol):
    async def read_bytes(self, blob_id: UUID, *, max_bytes: int = ...) -> bytes: ...


@dataclass(frozen=True, slots=True)
class _Entity:
    kind: InvestigationGraphNodeKind
    value: str


@dataclass(frozen=True, slots=True)
class _CandidateEdge:
    source: _Entity
    target: _Entity
    relation: str
    direction: InvestigationGraphDirection
    source_kind: InvestigationGraphSourceKind
    evidence_basis: InvestigationGraphEvidenceBasis
    observed_at: datetime | None
    evidence_ref: InvestigationGraphEvidenceRef
    finding_refs: tuple[UUID, ...] = ()


class InvestigationGraphSource:
    """Project observations/findings already stored under immutable SHA-256 identities."""

    def __init__(
        self,
        *,
        artifact_reader: CanonicalEvidenceReader,
        max_nodes: int = 8,
        max_edges: int = 10,
    ) -> None:
        if max_nodes < 3 or max_edges < 1:
            raise ValueError("Graph bounds must allow a seed, a relation, and an omission note")
        self._artifact_reader = artifact_reader
        self._max_nodes = max_nodes
        self._max_edges = max_edges

    async def project(
        self,
        *,
        investigation_id: UUID,
        publication: PublicationDocumentV5,
        observations: Sequence[EvidenceObservation] | None = None,
        findings: Sequence[InvestigationFinding] | None = None,
        observation_repository: EvidenceObservationRepository | None = None,
        finding_repository: FindingRepository | None = None,
    ) -> InvestigationGraph | None:
        """Read the existing investigation ports unless their canonical rows were loaded."""
        if observations is None:
            if observation_repository is None:
                raise ValueError("An evidence observation repository is required")
            observations = await observation_repository.list_for_investigation(investigation_id)
        if findings is None:
            if finding_repository is None:
                raise ValueError("A finding repository is required")
            findings = await finding_repository.list_for_investigation(investigation_id)
        if not isinstance(publication, PublicationDocumentV5):
            raise TypeError("Infrastructure graph projection requires a V5 publication")

        publication_sha256 = publication_document_v5_sha256(publication)
        roots, article_edges, article_nodes = _article_seeds(publication)
        if not roots:
            return None

        findings_by_observation = _current_finding_refs(findings, investigation_id)
        provider_edges: list[_CandidateEdge] = []
        for observation in sorted(observations, key=lambda item: (item.observed_at, item.id.hex)):
            if observation.investigation_id != investigation_id:
                continue
            if observation.kind not in {
                EvidenceKind.VT_RELATIONSHIP,
                EvidenceKind.SHODAN_OBSERVATION,
            }:
                # Negative results, including empty relationships and 404s, never create edges.
                continue
            payload = await self._read_canonical_payload(observation)
            if payload is None:
                continue
            evidence_ref = InvestigationGraphEvidenceRef(
                observation_id=observation.id,
                sha256=observation.sha256,
                source_name=observation.source,
                source_version=observation.source_version,
                observed_at=observation.observed_at,
            )
            citation_refs = findings_by_observation.get(observation.id, ())
            if observation.kind is EvidenceKind.VT_RELATIONSHIP:
                provider_edges.extend(
                    _vt_edges(
                        observation=observation,
                        payload=payload,
                        evidence_ref=evidence_ref,
                        finding_refs=citation_refs,
                    )
                )
            else:
                provider_edges.extend(
                    _shodan_edges(
                        observation=observation,
                        payload=payload,
                        evidence_ref=evidence_ref,
                        finding_refs=citation_refs,
                    )
                )

        reachable_edges = _edges_reachable_from_roots(provider_edges, set(roots))
        if not reachable_edges:
            return None
        if not any(
            edge.source_kind is not InvestigationGraphSourceKind.ARTICLE for edge in reachable_edges
        ):
            return None

        candidates_by_entity: dict[_Entity, InvestigationGraphNode] = {}
        for entity in roots:
            candidates_by_entity[entity] = _node(entity)
        for edge in (*reachable_edges, *article_edges):
            candidates_by_entity.setdefault(edge.source, _node(edge.source, article_nodes))
            candidates_by_entity.setdefault(edge.target, _node(edge.target, article_nodes))

        ordered_entities = sorted(
            candidates_by_entity,
            key=lambda item: (
                0 if item in roots else 1,
                _NODE_KIND_ORDER[item.kind],
                item.value.casefold(),
                item.value,
            ),
        )
        omitted_count = 0
        capacity = self._max_nodes
        if len(ordered_entities) > self._max_nodes:
            capacity = self._max_nodes - 1
        omitted_entities = ordered_entities[capacity:]
        omitted_count = len(omitted_entities)
        selected_entities = set(ordered_entities[:capacity])
        candidates_after_node_bound = [
            edge
            for edge in (*reachable_edges, *article_edges)
            if edge.source in selected_entities and edge.target in selected_entities
        ]
        candidates_after_node_bound.sort(key=_candidate_sort)
        omitted_edge_count = (
            len(reachable_edges) + len(article_edges) - len(candidates_after_node_bound)
        )
        omitted_edge_count += max(0, len(candidates_after_node_bound) - self._max_edges)
        selected_candidates = candidates_after_node_bound[: self._max_edges]

        selected_nodes = [candidates_by_entity[item] for item in selected_entities]
        if omitted_count:
            selected_nodes.append(
                InvestigationGraphNode(
                    node_id="omitted",
                    kind=InvestigationGraphNodeKind.OMITTED,
                    value=str(omitted_count),
                    label=_omitted_group_label(omitted_entities),
                )
            )
        node_ids = {item.node_id for item in selected_nodes}
        edge_groups: dict[tuple[str, str, str, str, str], list[_CandidateEdge]] = defaultdict(list)
        for candidate in selected_candidates:
            source_id = _entity_id(candidate.source)
            target_id = _entity_id(candidate.target)
            if source_id not in node_ids or target_id not in node_ids:
                continue
            edge_groups[
                (
                    source_id,
                    target_id,
                    candidate.relation,
                    candidate.source_kind.value,
                    candidate.evidence_basis.value,
                )
            ].append(candidate)

        graph_edges: list[InvestigationGraphEdge] = []
        for group_key, group in sorted(edge_groups.items()):
            latest = max(
                (item.observed_at for item in group if item.observed_at is not None),
                default=None,
            )
            graph_edges.append(
                InvestigationGraphEdge(
                    source_node_id=group_key[0],
                    target_node_id=group_key[1],
                    relation=group_key[2],
                    direction=group[0].direction,
                    source_kind=InvestigationGraphSourceKind(group_key[3]),
                    evidence_basis=InvestigationGraphEvidenceBasis(group_key[4]),
                    confidence=InvestigationGraphConfidence.UNASSESSED,
                    observed_at=latest,
                    evidence_refs=tuple(item.evidence_ref for item in group),
                    finding_refs=tuple(
                        finding_ref for item in group for finding_ref in item.finding_refs
                    ),
                )
            )

        if not any(
            edge.source_kind is not InvestigationGraphSourceKind.ARTICLE for edge in graph_edges
        ):
            return None
        return InvestigationGraph(
            investigation_id=investigation_id,
            publication_sha256=publication_sha256,
            nodes=tuple(selected_nodes),
            edges=tuple(graph_edges),
            omitted_count=omitted_count,
            omitted_edge_count=omitted_edge_count,
        )

    async def _read_canonical_payload(
        self, observation: EvidenceObservation
    ) -> Mapping[str, Any] | None:
        raw = await self._artifact_reader.read_bytes(
            observation.blob_id, max_bytes=_MAX_EVIDENCE_BYTES
        )
        if hashlib.sha256(raw).hexdigest() != observation.sha256:
            return None
        try:
            payload = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, Mapping) else None


def _article_seeds(
    publication: PublicationDocumentV5,
) -> tuple[set[_Entity], list[_CandidateEdge], dict[_Entity, str]]:
    sources = {item.source_document_id: item for item in publication.sources}
    article_details: dict[tuple[_Entity, UUID], tuple[str, str]] = {}
    for detail in publication.ioc_details:
        if (
            detail.artifact_type is not ArtifactType.HASH
            or detail.disposition is not PublicationIOCDisposition.RETAINED
        ):
            continue
        normalized = _canonical_identifier("file", detail.normalized_value)
        if normalized is None or normalized.kind is not InvestigationGraphNodeKind.FILE_SHA256:
            continue
        for source_id in detail.source_document_ids:
            source = sources.get(source_id)
            if source is None or not _valid_sha256(source.content_sha256):
                continue
            quote_sha256 = hashlib.sha256(detail.evidence_quote.encode("utf-8")).hexdigest()
            article_details[(normalized, source_id)] = (source.content_sha256, quote_sha256)

    # Older V5 documents may not have ioc_details; their original IOC groups remain canonical.
    if not article_details:
        for group in publication.original_indicators:
            if group.artifact_type is not ArtifactType.HASH:
                continue
            for indicator in group.indicators:
                normalized = _canonical_identifier("file", indicator.normalized_value)
                if (
                    normalized is None
                    or normalized.kind is not InvestigationGraphNodeKind.FILE_SHA256
                ):
                    continue
                for source_id in indicator.source_document_ids:
                    source = sources.get(source_id)
                    if source is not None and _valid_sha256(source.content_sha256):
                        article_details[(normalized, source_id)] = (
                            source.content_sha256,
                            hashlib.sha256(indicator.value.encode("utf-8")).hexdigest(),
                        )

    roots = {entity for entity, _ in article_details}
    article_edges: list[_CandidateEdge] = []
    article_nodes: dict[_Entity, str] = {}
    for (file_entity, source_id), (source_sha256, evidence_key) in sorted(
        article_details.items(), key=lambda item: (item[0][0].value, str(item[0][1]))
    ):
        source = sources[source_id]
        hostname = urlsplit(source.canonical_url).hostname or "source"
        article_entity = _Entity(InvestigationGraphNodeKind.ARTICLE, str(source_id))
        article_nodes[article_entity] = f"Article · {hostname[:190]}"
        article_edges.append(
            _CandidateEdge(
                source=article_entity,
                target=file_entity,
                relation="mentions IOC",
                direction=InvestigationGraphDirection.ARTICLE_TO_IOC,
                source_kind=InvestigationGraphSourceKind.ARTICLE,
                evidence_basis=InvestigationGraphEvidenceBasis.ARTICLE_ASSERTION,
                observed_at=None,
                evidence_ref=InvestigationGraphEvidenceRef(
                    source_document_id=source_id,
                    sha256=source_sha256,
                    source_name=source.publisher or hostname,
                    evidence_key=evidence_key,
                ),
            )
        )
    return roots, article_edges, article_nodes


def _current_finding_refs(
    findings: Sequence[InvestigationFinding], investigation_id: UUID
) -> dict[UUID, tuple[UUID, ...]]:
    current = [item for item in findings if item.investigation_id == investigation_id]
    superseded = {item.supersedes for item in current if item.supersedes is not None}
    by_observation: dict[UUID, list[UUID]] = defaultdict(list)
    for finding in current:
        if finding.id in superseded:
            continue
        for observation_id in finding.observation_refs:
            by_observation[observation_id].append(finding.id)
    return {
        observation_id: tuple(sorted(set(finding_ids), key=str))
        for observation_id, finding_ids in by_observation.items()
    }


def _vt_edges(
    *,
    observation: EvidenceObservation,
    payload: Mapping[str, Any],
    evidence_ref: InvestigationGraphEvidenceRef,
    finding_refs: tuple[UUID, ...],
) -> list[_CandidateEdge]:
    if payload.get("schema") != "virustotal-investigation-summary.v1":
        return []
    relation = payload.get("relation")
    descriptors = payload.get("descriptors")
    if not isinstance(relation, str) or not isinstance(descriptors, list):
        return []
    relation = relation.strip().lower()
    pivot = _canonical_identifier(observation.object_type, observation.object_id)
    if pivot is None:
        return []
    results: list[_CandidateEdge] = []
    for descriptor in descriptors:
        if not isinstance(descriptor, Mapping):
            continue
        target = _canonical_identifier(
            str(descriptor.get("type", "")), str(descriptor.get("identifier", ""))
        )
        if target is None:
            continue
        oriented = _orient_vt_relation(pivot, target, relation)
        if oriented is None:
            continue
        source, destination, direction = oriented
        results.append(
            _CandidateEdge(
                source=source,
                target=destination,
                relation=relation,
                direction=direction,
                source_kind=InvestigationGraphSourceKind.VIRUSTOTAL,
                evidence_basis=InvestigationGraphEvidenceBasis.PROVIDER_ASSERTION,
                observed_at=observation.observed_at,
                evidence_ref=evidence_ref,
                finding_refs=finding_refs,
            )
        )
    return results


def _orient_vt_relation(
    pivot: _Entity, target: _Entity, relation: str
) -> tuple[_Entity, _Entity, InvestigationGraphDirection] | None:
    if pivot.kind is InvestigationGraphNodeKind.FILE_SHA256:
        if relation in _VT_FILE_OUTBOUND:
            return pivot, target, InvestigationGraphDirection.PIVOT_TO_TARGET
        if relation in _VT_FILE_REVERSED and target.kind is InvestigationGraphNodeKind.FILE_SHA256:
            return target, pivot, InvestigationGraphDirection.TARGET_TO_PIVOT
        if (
            relation in _VT_FILE_UNDIRECTED
            and target.kind is InvestigationGraphNodeKind.FILE_SHA256
        ):
            return pivot, target, InvestigationGraphDirection.UNDIRECTED
        return None
    if relation == "resolutions":
        if (
            pivot.kind is InvestigationGraphNodeKind.IP
            and target.kind is InvestigationGraphNodeKind.DOMAIN
        ):
            return target, pivot, InvestigationGraphDirection.TARGET_TO_PIVOT
        if (
            pivot.kind is InvestigationGraphNodeKind.DOMAIN
            and target.kind is InvestigationGraphNodeKind.IP
        ):
            return pivot, target, InvestigationGraphDirection.PIVOT_TO_TARGET
        return None
    if (
        relation in _VT_REVERSED_FROM_NON_FILE
        and target.kind is InvestigationGraphNodeKind.FILE_SHA256
    ):
        return target, pivot, InvestigationGraphDirection.TARGET_TO_PIVOT
    return None


def _shodan_edges(
    *,
    observation: EvidenceObservation,
    payload: Mapping[str, Any],
    evidence_ref: InvestigationGraphEvidenceRef,
    finding_refs: tuple[UUID, ...],
) -> list[_CandidateEdge]:
    if payload.get("schema") != "shodan-observation.v1":
        return []
    object_value = payload.get("object")
    summary = payload.get("summary")
    if not isinstance(object_value, Mapping) or not isinstance(summary, Mapping):
        return []
    object_type = object_value.get("type")
    object_id = object_value.get("value")
    if object_type != observation.object_type or object_id != observation.object_id:
        return []
    captured_at = _timestamp(payload.get("captured_at")) or observation.observed_at
    results: list[_CandidateEdge] = []
    if object_type == "ip":
        ip = _canonical_identifier("ip", str(summary.get("ip", object_id)))
        if ip is not None:
            results.extend(
                _hostname_edges(
                    ip,
                    summary.get("hostnames"),
                    evidence_ref,
                    finding_refs,
                    captured_at,
                )
            )
    elif object_type == "domain":
        domain = _canonical_identifier("domain", str(summary.get("domain", object_id)))
        if domain is not None:
            results.extend(
                _domain_dns_edges(
                    domain,
                    summary.get("dns_records"),
                    evidence_ref,
                    finding_refs,
                    captured_at,
                )
            )
            subdomains = summary.get("subdomains")
            if isinstance(subdomains, list):
                for raw_subdomain in subdomains:
                    subdomain = _canonical_identifier("domain", str(raw_subdomain))
                    if subdomain is not None and subdomain != domain:
                        results.append(
                            _candidate(
                                subdomain,
                                domain,
                                "subdomain_of",
                                InvestigationGraphSourceKind.SHODAN,
                                evidence_ref,
                                finding_refs,
                                captured_at,
                                direction=InvestigationGraphDirection.TARGET_TO_PIVOT,
                            )
                        )
    elif object_type == "query":
        matches = summary.get("matches")
        if isinstance(matches, list):
            for match in matches:
                if not isinstance(match, Mapping):
                    continue
                ip = _canonical_identifier("ip", str(match.get("ip", "")))
                if ip is not None:
                    results.extend(
                        _hostname_edges(
                            ip,
                            match.get("hostnames"),
                            evidence_ref,
                            finding_refs,
                            captured_at,
                        )
                    )
    return results


def _hostname_edges(
    ip: _Entity,
    hostnames: Any,
    evidence_ref: InvestigationGraphEvidenceRef,
    finding_refs: tuple[UUID, ...],
    observed_at: datetime,
) -> list[_CandidateEdge]:
    if not isinstance(hostnames, list):
        return []
    return [
        _candidate(
            domain,
            ip,
            "shodan-hostname",
            InvestigationGraphSourceKind.SHODAN,
            evidence_ref,
            finding_refs,
            observed_at,
            direction=InvestigationGraphDirection.TARGET_TO_PIVOT,
        )
        for raw_hostname in hostnames
        if (domain := _canonical_identifier("domain", str(raw_hostname))) is not None
    ]


def _domain_dns_edges(
    domain: _Entity,
    records: Any,
    evidence_ref: InvestigationGraphEvidenceRef,
    finding_refs: tuple[UUID, ...],
    observed_at: datetime,
) -> list[_CandidateEdge]:
    if not isinstance(records, list):
        return []
    results: list[_CandidateEdge] = []
    for record in records:
        if not isinstance(record, Mapping):
            continue
        record_type = str(record.get("type", record.get("record_type", ""))).upper()
        timestamp = (
            _timestamp(record.get("last_seen", record.get("timestamp", record.get("date"))))
            or observed_at
        )
        if record_type in {"A", "AAAA"}:
            targets = _string_values(record, ("value", "ip", "data", "rdata"))
            for raw_target in targets:
                target = _canonical_identifier("ip", raw_target)
                if target is not None:
                    results.append(
                        _candidate(
                            domain,
                            target,
                            f"dns:{record_type}",
                            InvestigationGraphSourceKind.PASSIVE_DNS,
                            evidence_ref,
                            finding_refs,
                            timestamp,
                        )
                    )
        elif record_type == "CNAME":
            targets = _string_values(record, ("value", "target", "data", "rdata"))
            for raw_target in targets:
                target = _canonical_identifier("domain", raw_target)
                if target is not None and target != domain:
                    results.append(
                        _candidate(
                            domain,
                            target,
                            "dns:CNAME",
                            InvestigationGraphSourceKind.PASSIVE_DNS,
                            evidence_ref,
                            finding_refs,
                            timestamp,
                        )
                    )
    return results


def _candidate(
    source: _Entity,
    target: _Entity,
    relation: str,
    source_kind: InvestigationGraphSourceKind,
    evidence_ref: InvestigationGraphEvidenceRef,
    finding_refs: tuple[UUID, ...],
    observed_at: datetime,
    *,
    direction: InvestigationGraphDirection = InvestigationGraphDirection.PIVOT_TO_TARGET,
) -> _CandidateEdge:
    return _CandidateEdge(
        source=source,
        target=target,
        relation=relation,
        direction=direction,
        source_kind=source_kind,
        evidence_basis=InvestigationGraphEvidenceBasis.PROVIDER_ASSERTION,
        observed_at=observed_at,
        evidence_ref=evidence_ref,
        finding_refs=finding_refs,
    )


def _edges_reachable_from_roots(
    edges: Sequence[_CandidateEdge], roots: set[_Entity]
) -> list[_CandidateEdge]:
    adjacent: dict[_Entity, list[_CandidateEdge]] = defaultdict(list)
    for edge in edges:
        adjacent[edge.source].append(edge)
        adjacent[edge.target].append(edge)
    reachable = set(roots)
    queue = deque(sorted(roots, key=lambda item: (item.kind.value, item.value)))
    while queue:
        current = queue.popleft()
        for edge in sorted(adjacent.get(current, ()), key=_candidate_sort):
            neighbor = edge.target if edge.source == current else edge.source
            if neighbor not in reachable:
                reachable.add(neighbor)
                queue.append(neighbor)
    return [edge for edge in edges if edge.source in reachable and edge.target in reachable]


def _canonical_identifier(item_type: str, value: str) -> _Entity | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    if item_type.lower() in {"file", "hash", "sha256", "file_hash"}:
        candidate = value.removeprefix("sha256:").removeprefix("SHA256:").lower()
        return (
            _Entity(InvestigationGraphNodeKind.FILE_SHA256, candidate)
            if _SHA256.fullmatch(candidate)
            else None
        )
    if item_type.lower() in {"ip", "ip_address", "ipv4", "ipv4_address", "ipv6", "ipv6_address"}:
        try:
            return _Entity(InvestigationGraphNodeKind.IP, str(ipaddress.ip_address(value)))
        except ValueError:
            return None
    if item_type.lower() in {"domain", "hostname", "subdomain"}:
        candidate = value.rstrip(".").lower()
        try:
            candidate = candidate.encode("idna").decode("ascii")
        except UnicodeError:
            return None
        if (
            len(candidate) > 253
            or not candidate
            or any(
                not label
                or len(label) > 63
                or label.startswith("-")
                or label.endswith("-")
                or not re.fullmatch(r"[a-z0-9-]+", label)
                for label in candidate.split(".")
            )
        ):
            return None
        return _Entity(InvestigationGraphNodeKind.DOMAIN, candidate)
    if item_type.lower() in {"url", "uri"}:
        return _canonical_url(value)
    return None


def _canonical_url(value: str) -> _Entity | None:
    try:
        parsed = urlsplit(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return None
        if parsed.username is not None or parsed.password is not None:
            return None
        if any(key.casefold() in _SENSITIVE_QUERY_KEYS for key, _ in parse_qsl(parsed.query)):
            return None
        hostname = parsed.hostname.encode("idna").decode("ascii").lower()
        port = parsed.port
    except (UnicodeError, ValueError):
        return None
    netloc = f"{hostname}:{port}" if port is not None else hostname
    normalized = urlunsplit(
        (parsed.scheme.lower(), netloc, parsed.path or "/", urlencode(parse_qsl(parsed.query)), "")
    )
    if len(normalized) > 2_048:
        return None
    return _Entity(InvestigationGraphNodeKind.URL, normalized)


def _timestamp(value: Any) -> datetime | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        try:
            return datetime.fromtimestamp(value, tz=UTC)
        except (OverflowError, OSError, ValueError):
            return None
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed.astimezone(UTC) if parsed.tzinfo is not None else None
    return None


def _string_values(record: Mapping[str, Any], keys: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    for key in keys:
        candidate = record.get(key)
        if isinstance(candidate, str):
            values.append(candidate)
        elif isinstance(candidate, list):
            values.extend(item for item in candidate if isinstance(item, str))
    return tuple(dict.fromkeys(values))


def _node(
    entity: _Entity, article_labels: Mapping[_Entity, str] | None = None
) -> InvestigationGraphNode:
    labels = article_labels or {}
    label = labels.get(entity)
    if label is None:
        label = {
            InvestigationGraphNodeKind.FILE_SHA256: f"SHA-256 · {entity.value}",
            InvestigationGraphNodeKind.IP: f"IP · {entity.value}",
            InvestigationGraphNodeKind.DOMAIN: f"Domaine · {entity.value}",
            InvestigationGraphNodeKind.URL: f"URL · {entity.value}",
            InvestigationGraphNodeKind.ARTICLE: "Article source",
            InvestigationGraphNodeKind.OMITTED: entity.value,
        }[entity.kind]
    return InvestigationGraphNode(
        node_id=_entity_id(entity), kind=entity.kind, value=entity.value, label=label
    )


def _omitted_group_label(entities: Sequence[_Entity]) -> str:
    kind = entities[0].kind
    category = {
        InvestigationGraphNodeKind.FILE_SHA256: "fichiers SHA-256",
        InvestigationGraphNodeKind.DOMAIN: "domaines",
        InvestigationGraphNodeKind.IP: "adresses IP",
        InvestigationGraphNodeKind.URL: "URL",
        InvestigationGraphNodeKind.ARTICLE: "sources article",
        InvestigationGraphNodeKind.OMITTED: "autres éléments",
    }
    suffix = f" ({category[kind]} regroupés)" if all(item.kind is kind for item in entities) else ""
    return f"{len(entities)} autres omis{suffix}"


def _entity_id(entity: _Entity) -> str:
    digest = hashlib.sha256(f"{entity.kind.value}:{entity.value}".encode()).hexdigest()[:20]
    return f"n_{digest}"


def _candidate_sort(edge: _CandidateEdge) -> tuple[str, ...]:
    return (
        edge.source.kind.value,
        edge.source.value.casefold(),
        edge.target.kind.value,
        edge.target.value.casefold(),
        edge.relation,
        edge.source_kind.value,
        edge.evidence_ref.sha256,
        edge.observed_at.isoformat() if edge.observed_at else "",
    )


def _valid_sha256(value: object) -> bool:
    return isinstance(value, str) and _SHA256.fullmatch(value) is not None


__all__ = ["CanonicalEvidenceReader", "InvestigationGraphSource"]
