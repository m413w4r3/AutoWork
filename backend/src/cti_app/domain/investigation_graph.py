"""Typed, deterministic infrastructure-graph contracts for an investigation."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any
from uuid import UUID

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NODE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,63}$")
_GRAPH_SCHEMA = "investigation-infrastructure-graph.v1"


class InvestigationGraphNodeKind(StrEnum):
    FILE_SHA256 = "file_sha256"
    IP = "ip"
    DOMAIN = "domain"
    URL = "url"
    ARTICLE = "article"
    OMITTED = "omitted"


class InvestigationGraphSource(StrEnum):
    VIRUSTOTAL = "virustotal"
    PASSIVE_DNS = "passive_dns"
    SHODAN = "shodan"
    ARTICLE = "article"


class InvestigationGraphEvidenceBasis(StrEnum):
    OBSERVED = "observed"
    PROVIDER_ASSERTION = "provider_assertion"
    ARTICLE_ASSERTION = "article_assertion"
    INFERENCE = "inference"


class InvestigationGraphConfidence(StrEnum):
    UNASSESSED = "unassessed"
    LOW = "low"
    MODERATE = "moderate"
    HIGH = "high"


class InvestigationGraphDirection(StrEnum):
    PIVOT_TO_TARGET = "pivot_to_target"
    TARGET_TO_PIVOT = "target_to_pivot"
    ARTICLE_TO_IOC = "article_to_ioc"
    UNDIRECTED = "undirected"


@dataclass(frozen=True, slots=True)
class InvestigationGraphEvidenceRef:
    """One immutable observation or source passage that supports an edge."""

    sha256: str
    source_name: str
    observation_id: UUID | None = None
    source_document_id: UUID | None = None
    source_version: str | None = None
    evidence_key: str | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.sha256):
            raise ValueError("Graph evidence references require a lowercase SHA-256")
        if not self.source_name.strip() or len(self.source_name) > 256:
            raise ValueError("Graph evidence source must be non-empty and bounded")
        if (self.observation_id is None) == (self.source_document_id is None):
            raise ValueError("Graph evidence must identify one observation or source document")
        if self.source_version is not None and not self.source_version.strip():
            raise ValueError("Graph source version must be non-empty when present")
        if self.evidence_key is not None and not _SHA256.fullmatch(self.evidence_key):
            raise ValueError("Graph evidence keys must be lowercase SHA-256 when present")
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("Graph evidence timestamps must be timezone-aware")

    def to_payload(self) -> dict[str, Any]:
        return {
            "sha256": self.sha256,
            "source_name": self.source_name,
            "observation_id": str(self.observation_id) if self.observation_id else None,
            "source_document_id": (
                str(self.source_document_id) if self.source_document_id else None
            ),
            "source_version": self.source_version,
            "evidence_key": self.evidence_key,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> InvestigationGraphEvidenceRef:
        if not isinstance(raw, dict) or set(raw) != {
            "sha256",
            "source_name",
            "observation_id",
            "source_document_id",
            "source_version",
            "evidence_key",
            "observed_at",
        }:
            raise ValueError("Graph evidence reference has an unsupported shape")
        timestamp = raw["observed_at"]
        try:
            observed_at = (
                datetime.fromisoformat(_text(timestamp, "evidence timestamp"))
                if timestamp
                else None
            )
        except ValueError as exc:
            raise ValueError("Graph evidence timestamp must be ISO formatted") from exc
        return cls(
            sha256=_text(raw["sha256"], "evidence SHA-256"),
            source_name=_text(raw["source_name"], "evidence source"),
            observation_id=_uuid_or_none(raw["observation_id"], "observation id"),
            source_document_id=_uuid_or_none(raw["source_document_id"], "source document id"),
            source_version=_optional_text(raw["source_version"], "source version"),
            evidence_key=_optional_text(raw["evidence_key"], "evidence key"),
            observed_at=observed_at,
        )


@dataclass(frozen=True, slots=True)
class InvestigationGraphNode:
    node_id: str
    kind: InvestigationGraphNodeKind
    value: str
    label: str

    def __post_init__(self) -> None:
        if not _NODE_ID.fullmatch(self.node_id):
            raise ValueError("Graph node id is not a safe identifier")
        if not isinstance(self.kind, InvestigationGraphNodeKind):
            raise TypeError("Graph node kind must use InvestigationGraphNodeKind")
        if not self.value.strip() or len(self.value) > 2_048:
            raise ValueError("Graph node value must be non-empty and bounded")
        if not self.label.strip() or len(self.label) > 256:
            raise ValueError("Graph node label must be non-empty and bounded")

    def to_payload(self) -> dict[str, str]:
        return {
            "node_id": self.node_id,
            "kind": self.kind.value,
            "value": self.value,
            "label": self.label,
        }

    @classmethod
    def from_payload(cls, raw: Any) -> InvestigationGraphNode:
        if not isinstance(raw, dict) or set(raw) != {"node_id", "kind", "value", "label"}:
            raise ValueError("Graph node has an unsupported shape")
        return cls(
            node_id=_text(raw["node_id"], "node id"),
            kind=_enum(InvestigationGraphNodeKind, raw["kind"], "node kind"),
            value=_text(raw["value"], "node value"),
            label=_text(raw["label"], "node label"),
        )


@dataclass(frozen=True, slots=True)
class InvestigationGraphEdge:
    source_node_id: str
    target_node_id: str
    relation: str
    direction: InvestigationGraphDirection
    source_kind: InvestigationGraphSource
    evidence_basis: InvestigationGraphEvidenceBasis
    confidence: InvestigationGraphConfidence
    observed_at: datetime | None
    evidence_refs: tuple[InvestigationGraphEvidenceRef, ...]
    finding_refs: tuple[UUID, ...] = ()

    def __post_init__(self) -> None:
        if not _NODE_ID.fullmatch(self.source_node_id) or not _NODE_ID.fullmatch(
            self.target_node_id
        ):
            raise ValueError("Graph edge endpoints must use safe node ids")
        if self.source_node_id == self.target_node_id:
            raise ValueError("Graph edges cannot point to the same node")
        if not self.relation.strip() or len(self.relation) > 128:
            raise ValueError("Graph relation label must be non-empty and bounded")
        for value, enum_type, label in (
            (self.direction, InvestigationGraphDirection, "direction"),
            (self.source_kind, InvestigationGraphSource, "source"),
            (self.evidence_basis, InvestigationGraphEvidenceBasis, "evidence basis"),
            (self.confidence, InvestigationGraphConfidence, "confidence"),
        ):
            if not isinstance(value, enum_type):
                raise TypeError(f"Graph {label} must use {enum_type.__name__}")
        if self.observed_at is not None and (
            self.observed_at.tzinfo is None or self.observed_at.utcoffset() is None
        ):
            raise ValueError("Graph observation timestamps must be timezone-aware")
        if not isinstance(self.evidence_refs, tuple) or not self.evidence_refs:
            raise ValueError("Graph edges require at least one canonical evidence reference")
        if any(not isinstance(item, InvestigationGraphEvidenceRef) for item in self.evidence_refs):
            raise ValueError("Graph edge contains an invalid evidence reference")
        if not isinstance(self.finding_refs, tuple) or any(
            not isinstance(item, UUID) for item in self.finding_refs
        ):
            raise ValueError("Graph edge finding references must be UUIDs")
        object.__setattr__(
            self, "evidence_refs", tuple(sorted(self.evidence_refs, key=_evidence_sort))
        )
        object.__setattr__(self, "finding_refs", tuple(sorted(set(self.finding_refs), key=str)))

    def to_payload(self) -> dict[str, Any]:
        return {
            "source_node_id": self.source_node_id,
            "target_node_id": self.target_node_id,
            "relation": self.relation,
            "direction": self.direction.value,
            "source_kind": self.source_kind.value,
            "evidence_basis": self.evidence_basis.value,
            "confidence": self.confidence.value,
            "observed_at": self.observed_at.isoformat() if self.observed_at else None,
            "evidence_refs": [item.to_payload() for item in self.evidence_refs],
            "finding_refs": [str(item) for item in self.finding_refs],
        }

    @classmethod
    def from_payload(cls, raw: Any) -> InvestigationGraphEdge:
        fields = {
            "source_node_id",
            "target_node_id",
            "relation",
            "direction",
            "source_kind",
            "evidence_basis",
            "confidence",
            "observed_at",
            "evidence_refs",
            "finding_refs",
        }
        if not isinstance(raw, dict) or set(raw) != fields:
            raise ValueError("Graph edge has an unsupported shape")
        timestamp = raw["observed_at"]
        try:
            observed_at = (
                datetime.fromisoformat(_text(timestamp, "edge timestamp")) if timestamp else None
            )
        except ValueError as exc:
            raise ValueError("Graph edge timestamp must be ISO formatted") from exc
        evidence_refs = raw["evidence_refs"]
        finding_refs = raw["finding_refs"]
        if not isinstance(evidence_refs, list) or not isinstance(finding_refs, list):
            raise ValueError("Graph edge references must be arrays")
        return cls(
            source_node_id=_text(raw["source_node_id"], "source node id"),
            target_node_id=_text(raw["target_node_id"], "target node id"),
            relation=_text(raw["relation"], "relation"),
            direction=_enum(InvestigationGraphDirection, raw["direction"], "edge direction"),
            source_kind=_enum(InvestigationGraphSource, raw["source_kind"], "edge source"),
            evidence_basis=_enum(
                InvestigationGraphEvidenceBasis, raw["evidence_basis"], "edge evidence basis"
            ),
            confidence=_enum(InvestigationGraphConfidence, raw["confidence"], "edge confidence"),
            observed_at=observed_at,
            evidence_refs=tuple(
                InvestigationGraphEvidenceRef.from_payload(item) for item in evidence_refs
            ),
            finding_refs=tuple(_uuid(item, "finding id") for item in finding_refs),
        )


@dataclass(frozen=True, slots=True)
class InvestigationGraph:
    investigation_id: UUID
    publication_sha256: str
    nodes: tuple[InvestigationGraphNode, ...]
    edges: tuple[InvestigationGraphEdge, ...]
    omitted_count: int = 0
    omitted_edge_count: int = 0
    policy_version: str = "investigation-infrastructure-graph-v1"

    def __post_init__(self) -> None:
        if not isinstance(self.investigation_id, UUID):
            raise TypeError("Graph investigation identity must be a UUID")
        if not _SHA256.fullmatch(self.publication_sha256):
            raise ValueError("Graph publication identity must be a lowercase SHA-256")
        if not isinstance(self.nodes, tuple) or any(
            not isinstance(item, InvestigationGraphNode) for item in self.nodes
        ):
            raise ValueError("Graph nodes must be a tuple of InvestigationGraphNode")
        if not isinstance(self.edges, tuple) or any(
            not isinstance(item, InvestigationGraphEdge) for item in self.edges
        ):
            raise ValueError("Graph edges must be a tuple of InvestigationGraphEdge")
        if len(self.nodes) > 128 or len(self.edges) > 512:
            raise ValueError("Graph exceeds the domain size limit")
        node_ids = {item.node_id for item in self.nodes}
        if len(node_ids) != len(self.nodes):
            raise ValueError("Graph node identities must be unique")
        if any(
            edge.source_node_id not in node_ids or edge.target_node_id not in node_ids
            for edge in self.edges
        ):
            raise ValueError("Graph edge references an unknown node")
        if type(self.omitted_count) is not int or self.omitted_count < 0:
            raise ValueError("Graph omitted_count must be a non-negative integer")
        if type(self.omitted_edge_count) is not int or self.omitted_edge_count < 0:
            raise ValueError("Graph omitted_edge_count must be a non-negative integer")
        if not self.policy_version.strip():
            raise ValueError("Graph policy version must be non-empty")
        object.__setattr__(self, "nodes", tuple(sorted(self.nodes, key=lambda item: item.node_id)))
        object.__setattr__(self, "edges", tuple(sorted(self.edges, key=_edge_sort)))

    @property
    def sha256(self) -> str:
        encoded = json.dumps(
            self.to_payload(), ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()

    def to_payload(self) -> dict[str, Any]:
        return {
            "schema": _GRAPH_SCHEMA,
            "investigation_id": str(self.investigation_id),
            "publication_sha256": self.publication_sha256,
            "policy_version": self.policy_version,
            "omitted_count": self.omitted_count,
            "omitted_edge_count": self.omitted_edge_count,
            "nodes": [item.to_payload() for item in self.nodes],
            "edges": [item.to_payload() for item in self.edges],
        }

    @classmethod
    def from_payload(cls, raw: Any) -> InvestigationGraph:
        fields = {
            "schema",
            "investigation_id",
            "publication_sha256",
            "policy_version",
            "omitted_count",
            "omitted_edge_count",
            "nodes",
            "edges",
        }
        if not isinstance(raw, dict) or set(raw) != fields or raw["schema"] != _GRAPH_SCHEMA:
            raise ValueError("Investigation graph payload has an unsupported shape")
        nodes = raw["nodes"]
        edges = raw["edges"]
        if not isinstance(nodes, list) or not isinstance(edges, list):
            raise ValueError("Graph nodes and edges must be arrays")
        omitted_count = raw["omitted_count"]
        omitted_edge_count = raw["omitted_edge_count"]
        if type(omitted_count) is not int or type(omitted_edge_count) is not int:
            raise ValueError("Graph omission counts must be integers")
        return cls(
            investigation_id=_uuid(raw["investigation_id"], "investigation id"),
            publication_sha256=_text(raw["publication_sha256"], "publication SHA-256"),
            policy_version=_text(raw["policy_version"], "graph policy version"),
            omitted_count=omitted_count,
            omitted_edge_count=omitted_edge_count,
            nodes=tuple(InvestigationGraphNode.from_payload(item) for item in nodes),
            edges=tuple(InvestigationGraphEdge.from_payload(item) for item in edges),
        )


def _evidence_sort(item: InvestigationGraphEvidenceRef) -> tuple[str, str, str]:
    return (
        str(item.observation_id or item.source_document_id),
        item.sha256,
        item.evidence_key or "",
    )


def _edge_sort(item: InvestigationGraphEdge) -> tuple[str, ...]:
    return (
        item.source_node_id,
        item.target_node_id,
        item.relation,
        item.source_kind.value,
        item.evidence_basis.value,
        item.observed_at.isoformat() if item.observed_at else "",
    )


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Graph {label} must be non-empty text")
    return value


def _optional_text(value: Any, label: str) -> str | None:
    return None if value is None else _text(value, label)


def _uuid(value: Any, label: str) -> UUID:
    try:
        return UUID(_text(value, label))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ValueError(f"Graph {label} must be a UUID") from exc


def _uuid_or_none(value: Any, label: str) -> UUID | None:
    return None if value is None else _uuid(value, label)


def _enum(enum_type: type[StrEnum], value: Any, label: str) -> Any:
    try:
        return enum_type(_text(value, label))
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Graph {label} is unsupported") from exc


__all__ = [
    "InvestigationGraph",
    "InvestigationGraphConfidence",
    "InvestigationGraphDirection",
    "InvestigationGraphEdge",
    "InvestigationGraphEvidenceBasis",
    "InvestigationGraphEvidenceRef",
    "InvestigationGraphNode",
    "InvestigationGraphNodeKind",
    "InvestigationGraphSource",
]
