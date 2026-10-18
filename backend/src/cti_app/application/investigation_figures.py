"""Deterministic publication figures over canonical, hash-linked evidence."""

from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from cti_app.domain.analyst_opinion import AnalystOpinionStatus
from cti_app.domain.investigation_graph import (
    InvestigationGraph,
    InvestigationGraphDirection,
    InvestigationGraphEvidenceBasis,
    InvestigationGraphNodeKind,
    InvestigationGraphSource,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV5,
    PublicationDocumentV6,
    publication_document_text_anchors,
    publication_document_v5_sha256,
)

_TIMELINE_FIGURE_POLICY_VERSION = "publication-timeline-figure-v1-exact-dates"
_INVESTIGATION_CHAIN_FIGURE_POLICY_VERSION = "approved-opinion-chain-figure-v1-cited-refs"
_INFRASTRUCTURE_GRAPH_FIGURE_POLICY_VERSION = (
    "investigation-infrastructure-figure-v1-canonical-edges"
)
_SHA256 = frozenset("0123456789abcdef")


def _is_sha256(value: str | None) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in _SHA256 for character in value)
    )


def _figure_identity(payload: dict[str, Any]) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def build_publication_timeline_figure(
    publication: PublicationDocumentV5,
) -> dict[str, Any] | None:
    """Project dated canonical events to a source- and evidence-hash pinned figure."""
    if not isinstance(publication, PublicationDocumentV5):
        raise TypeError("Timeline figures require PublicationDocumentV5")
    sources_by_id = {source.source_document_id: source for source in publication.sources}
    candidates: list[tuple[date, str, tuple[tuple[str, str, str, str], ...]]] = []
    for entry in publication.document.timeline:
        if entry.event_date is None:
            continue
        if not entry.evidence_refs:
            return None
        provenance: set[tuple[str, str, str, str]] = set()
        for reference in entry.evidence_refs:
            source = sources_by_id.get(reference.source_document_id)
            if (
                reference.kind.value != "event"
                or source is None
                or not _is_sha256(source.content_sha256)
                or not _is_sha256(reference.evidence_key)
            ):
                return None
            provenance.add(
                (
                    str(reference.source_document_id),
                    source.canonical_url,
                    source.content_sha256,
                    reference.evidence_key,
                )
            )
        candidates.append((entry.event_date, entry.text, tuple(sorted(provenance))))

    distinct_dates = {item[0] for item in candidates}
    if len(distinct_dates) < 2:
        return None

    candidates.sort(key=lambda item: (item[0], item[1].casefold(), item[2]))
    first_date = candidates[0][0]
    events = [
        {
            "date": event_date.isoformat(),
            "offset_days": (event_date - first_date).days,
            "text": text,
            "sources": [
                {
                    "source_document_id": source_id,
                    "url": url,
                    "source_sha256": source_sha256,
                    "evidence_sha256": evidence_sha256,
                }
                for source_id, url, source_sha256, evidence_sha256 in provenance
            ],
        }
        for event_date, text, provenance in candidates
    ]
    input_payload = {
        "policy_version": _TIMELINE_FIGURE_POLICY_VERSION,
        "publication_sha256": publication_document_v5_sha256(publication),
        "events": events,
    }
    return {
        "figure_id": _figure_identity(input_payload),
        "title": "Chronologie des événements datés",
        "caption": (
            "Échelle calendaire en jours. La position suit les dates exactes "
            "des événements cités dans l'extraction canonique."
        ),
        "axis_unit": "jour calendaire",
        "publication_sha256": input_payload["publication_sha256"],
        "events": events,
    }


def build_investigation_chain_figure(
    publication: PublicationDocumentV6,
) -> dict[str, Any] | None:
    """Project only approved, anchored observations and their explicit citations."""
    if not isinstance(publication, PublicationDocumentV6):
        raise TypeError("Investigation chain figures require PublicationDocumentV6")
    opinion = publication.analyst_opinion
    if opinion is None or opinion.status is not AnalystOpinionStatus.APPROVED:
        return None
    anchors = publication_document_text_anchors(
        publication.base_publication.document,
        references=publication.base_publication.references,
    )
    investigation_id = opinion.investigation_id
    rows: list[dict[str, Any]] = []
    for observation in opinion.observations:
        if not (observation.finding_refs or observation.evidence_refs):
            continue
        if observation.anchor not in anchors:
            raise ValueError("Approved observation refers to an unknown publication anchor")
        rows.append(
            {
                "observation_id": str(observation.id),
                "anchor": observation.anchor,
                "anchor_text": anchors[observation.anchor],
                "state": observation.state.value,
                "relation": observation.relation.value,
                "text": observation.text,
                "finding_refs": [
                    {
                        "id": str(reference_id),
                        "href": (
                            f"autowork://investigations/{investigation_id}/findings/{reference_id}"
                        ),
                    }
                    for reference_id in observation.finding_refs
                ],
                "evidence_refs": [
                    {
                        "id": str(reference_id),
                        "href": (
                            f"autowork://investigations/{investigation_id}/evidence/{reference_id}"
                        ),
                    }
                    for reference_id in observation.evidence_refs
                ],
            }
        )
    rows.sort(key=lambda item: item["observation_id"])
    if not rows:
        return None

    base_sha256 = publication.provenance.base_publication_sha256
    input_payload = {
        "policy_version": _INVESTIGATION_CHAIN_FIGURE_POLICY_VERSION,
        "opinion_id": str(opinion.id),
        "opinion_version": opinion.version,
        "opinion_sha256": opinion.content_sha256,
        "base_publication_sha256": base_sha256,
        "rows": rows,
    }
    return {
        "figure_id": _figure_identity(input_payload),
        "title": "Chaîne d'ancrage des observations approuvées",
        "caption": (
            "Les flèches montrent l'ancrage et les citations de l'avis approuvé; "
            "elles ne représentent pas une causalité."
        ),
        "opinion_id": input_payload["opinion_id"],
        "opinion_version": input_payload["opinion_version"],
        "opinion_sha256": input_payload["opinion_sha256"],
        "base_publication_sha256": base_sha256,
        "rows": rows,
    }


def build_investigation_graph_figure(
    publication: PublicationDocumentV5,
    graph: InvestigationGraph | None,
) -> dict[str, Any] | None:
    """Project a bounded, hash-pinned graph without adding unsupported relationships."""
    if not isinstance(publication, PublicationDocumentV5):
        raise TypeError("Infrastructure graph figures require PublicationDocumentV5")
    if graph is None:
        return None
    if graph.publication_sha256 != publication_document_v5_sha256(publication):
        raise ValueError("Infrastructure graph is pinned to a different V5 publication")
    infrastructure_edges = [
        edge for edge in graph.edges if edge.source_kind is not InvestigationGraphSource.ARTICLE
    ]
    if not infrastructure_edges:
        return None

    nodes_by_id = {node.node_id: node for node in graph.nodes}
    node_payloads = [
        {
            **node.to_payload(),
            "display_label": _graph_node_display_label(node.kind, node.value, node.label),
        }
        for node in graph.nodes
    ]
    edge_payloads: list[dict[str, Any]] = []
    d2_lines = ["direction: down"]
    for node in graph.nodes:
        color = _GRAPH_NODE_COLORS[node.kind]
        display_label = _graph_node_display_label(node.kind, node.value, node.label)
        d2_lines.extend(
            (
                f"{node.node_id}: {_escape_d2_literal(display_label)} {{",
                "  shape: rectangle",
                "  style: {",
                f'    fill: "{color[0]}"',
                f'    stroke: "{color[1]}"',
                '    font-color: "#172033"',
                "    border-radius: 6",
                "  }",
                "}",
            )
        )

    for edge in graph.edges:
        evidence_refs = [item.to_payload() for item in edge.evidence_refs]
        from_node = nodes_by_id[edge.source_node_id]
        to_node = nodes_by_id[edge.target_node_id]
        source_label = _GRAPH_SOURCE_LABELS[edge.source_kind]
        basis_label = _GRAPH_BASIS_LABELS[edge.evidence_basis]
        confidence_label = _GRAPH_CONFIDENCE_LABELS[edge.confidence]
        timestamp = edge.observed_at.isoformat() if edge.observed_at else "date non fournie"
        edge_label = edge.relation
        connector = "--" if edge.direction is InvestigationGraphDirection.UNDIRECTED else "->"
        d2_lines.extend(
            (
                f"{edge.source_node_id} {connector} {edge.target_node_id}: "
                f"{_escape_d2_literal(edge_label)} {{",
                "  style: {",
                f'    stroke: "{_GRAPH_EDGE_COLORS[edge.evidence_basis]}"',
                "    stroke-width: 2",
                f"    stroke-dash: {_GRAPH_EDGE_DASHES[edge.evidence_basis]}",
                "  }",
                "}",
            )
        )
        edge_payloads.append(
            {
                "source_node_id": edge.source_node_id,
                "target_node_id": edge.target_node_id,
                "source_value": from_node.value,
                "target_value": to_node.value,
                "relation": edge.relation,
                "direction": edge.direction.value,
                "direction_label": _GRAPH_DIRECTION_LABELS[edge.direction],
                "direction_symbol": _GRAPH_DIRECTION_SYMBOLS[edge.direction],
                "source_kind": edge.source_kind.value,
                "source_label": source_label,
                "evidence_basis": edge.evidence_basis.value,
                "evidence_basis_label": basis_label,
                "confidence": edge.confidence.value,
                "confidence_label": confidence_label,
                "observed_at": timestamp if edge.observed_at else None,
                "evidence_refs": evidence_refs,
                "finding_refs": [str(item) for item in edge.finding_refs],
            }
        )

    if graph.omitted_count:
        d2_lines.append("# Les nœuds omis sont indiqués par le nœud d'agrégation.")
    if graph.omitted_edge_count:
        d2_lines.append(f"# {graph.omitted_edge_count} relations supplémentaires omises.")
    d2_source = "\n".join(d2_lines) + "\n"
    figure_identity_payload = {
        "policy_version": _INFRASTRUCTURE_GRAPH_FIGURE_POLICY_VERSION,
        "publication_sha256": graph.publication_sha256,
        "investigation_id": str(graph.investigation_id),
        "graph_sha256": graph.sha256,
        "nodes": node_payloads,
        "edges": edge_payloads,
        "omitted_count": graph.omitted_count,
        "omitted_edge_count": graph.omitted_edge_count,
    }
    figure = {
        "figure_id": _figure_identity(figure_identity_payload),
        "title": "Graphe d'infrastructure et de pivots",
        "caption": (
            "Les flèches représentent uniquement des relations positives tirées de preuves "
            "canoniques. Le style distingue observation, affirmation fournisseur ou article, "
            "et inférence. La confiance reste non évaluée si la source ne la chiffre pas."
        ),
        "legend": [
            {"basis": basis.value, "label": _GRAPH_BASIS_LABELS[basis]}
            for basis in InvestigationGraphEvidenceBasis
        ],
        "figure_policy_version": _INFRASTRUCTURE_GRAPH_FIGURE_POLICY_VERSION,
        "publication_sha256": graph.publication_sha256,
        "graph_sha256": graph.sha256,
        "d2_source": d2_source,
        "d2_source_sha256": hashlib.sha256(d2_source.encode("utf-8")).hexdigest(),
        "nodes": node_payloads,
        "edges": edge_payloads,
        "omitted_count": graph.omitted_count,
        "omitted_edge_count": graph.omitted_edge_count,
    }
    return figure


_GRAPH_NODE_COLORS = {
    InvestigationGraphNodeKind.FILE_SHA256: ("#FFF7ED", "#C2410C"),
    InvestigationGraphNodeKind.IP: ("#EFF6FF", "#2563EB"),
    InvestigationGraphNodeKind.DOMAIN: ("#F0FDF4", "#15803D"),
    InvestigationGraphNodeKind.URL: ("#F5F3FF", "#7C3AED"),
    InvestigationGraphNodeKind.ARTICLE: ("#F8FAFC", "#64748B"),
    InvestigationGraphNodeKind.OMITTED: ("#F1F5F9", "#64748B"),
}
_GRAPH_EDGE_COLORS = {
    InvestigationGraphEvidenceBasis.OBSERVED: "#15803D",
    InvestigationGraphEvidenceBasis.PROVIDER_ASSERTION: "#B45309",
    InvestigationGraphEvidenceBasis.ARTICLE_ASSERTION: "#2563EB",
    InvestigationGraphEvidenceBasis.INFERENCE: "#7C3AED",
}
_GRAPH_EDGE_DASHES = {
    InvestigationGraphEvidenceBasis.OBSERVED: 0,
    InvestigationGraphEvidenceBasis.PROVIDER_ASSERTION: 5,
    InvestigationGraphEvidenceBasis.ARTICLE_ASSERTION: 5,
    InvestigationGraphEvidenceBasis.INFERENCE: 2,
}
_GRAPH_SOURCE_LABELS = {
    InvestigationGraphSource.VIRUSTOTAL: "Relation VirusTotal",
    InvestigationGraphSource.PASSIVE_DNS: "Résolution passive",
    InvestigationGraphSource.SHODAN: "Shodan",
    InvestigationGraphSource.ARTICLE: "Article",
}
_GRAPH_DIRECTION_LABELS = {
    InvestigationGraphDirection.PIVOT_TO_TARGET: "pivot vers cible",
    InvestigationGraphDirection.TARGET_TO_PIVOT: "cible vers pivot",
    InvestigationGraphDirection.ARTICLE_TO_IOC: "article vers IOC",
    InvestigationGraphDirection.UNDIRECTED: "association non orientée",
}
_GRAPH_DIRECTION_SYMBOLS = {
    InvestigationGraphDirection.PIVOT_TO_TARGET: "→",
    InvestigationGraphDirection.TARGET_TO_PIVOT: "→",
    InvestigationGraphDirection.ARTICLE_TO_IOC: "→",
    InvestigationGraphDirection.UNDIRECTED: "↔",
}
_GRAPH_BASIS_LABELS = {
    InvestigationGraphEvidenceBasis.OBSERVED: "fait observé",
    InvestigationGraphEvidenceBasis.PROVIDER_ASSERTION: "affirmation fournisseur",
    InvestigationGraphEvidenceBasis.ARTICLE_ASSERTION: "affirmation article",
    InvestigationGraphEvidenceBasis.INFERENCE: "inférence",
}
_GRAPH_CONFIDENCE_LABELS = {
    "unassessed": "non évaluée",
    "low": "faible",
    "moderate": "modérée",
    "high": "élevée",
}


def _graph_node_display_label(kind: InvestigationGraphNodeKind, value: str, label: str) -> str:
    if kind is InvestigationGraphNodeKind.FILE_SHA256 and _is_sha256(value):
        return f"SHA-256 · {value[:12]}…{value[-8:]}"
    if kind is InvestigationGraphNodeKind.URL:
        parsed = urlsplit(value)
        return f"URL · {parsed.hostname or 'URL'}{parsed.path[:44]}"
    return label


def _escape_d2_literal(value: str) -> str:
    escapes = {"\\": "\\\\", '"': '\\"', "$": "\\$", "\n": "\\n", "\r": "\\r", "\t": "\\t"}
    return '"' + "".join(escapes.get(character, character) for character in value) + '"'
