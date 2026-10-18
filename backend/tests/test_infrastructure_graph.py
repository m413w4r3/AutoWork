from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID

import pytest

from cti_app.application.investigation_figures import build_investigation_graph_figure
from cti_app.application.investigation_graph_source import InvestigationGraphSource
from cti_app.application.publication_augmentation import augment_publication_with_opinion
from cti_app.domain.discovery import SourceRole
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import (
    PublicationIOCDetailV1,
    PublicationIOCDisposition,
    publication_document_from_json,
    serialize_publication_document,
)
from tests.test_editorial_figures import _publication_with_timeline
from tests.test_publication_document_v6 import _approved_opinion


class _BlobReader:
    def __init__(self, payloads: dict[UUID, bytes]) -> None:
        self.payloads = payloads

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        payload = self.payloads[blob_id]
        if len(payload) > max_bytes:
            raise ValueError("oversized test blob")
        return payload


class _ObservationRepository:
    def __init__(self, observations: tuple[EvidenceObservation, ...]) -> None:
        self.observations = observations

    async def list_for_investigation(self, investigation_id: UUID):
        return self.observations


class _FindingRepository:
    async def list_for_investigation(self, investigation_id: UUID):
        return ()


def _json_bytes(payload: dict[str, object]) -> bytes:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _publication_with_seed_hash(seed_hash: str):
    publication = _publication_with_timeline()
    source_id = publication.sources[0].source_document_id
    detail = PublicationIOCDetailV1(
        artifact_type=ArtifactType.HASH,
        value=seed_hash,
        normalized_value=seed_hash,
        source_role=SourceRole.PRIMARY,
        context="Échantillon référencé dans l'article",
        evidence_quote=f"SHA-256 : {seed_hash}",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
        disposition=PublicationIOCDisposition.RETAINED,
        disposition_reason="IOC source conservé",
    )
    return replace(publication, ioc_details=(detail,))


def _observation(
    *,
    observation_id: int,
    kind: EvidenceKind,
    object_type: str,
    object_id: str,
    source: str,
    payload: dict[str, object],
    observed_at: datetime,
) -> tuple[EvidenceObservation, tuple[UUID, bytes]]:
    blob_id = UUID(int=10_000 + observation_id)
    raw = _json_bytes(payload)
    observation = EvidenceObservation(
        id=UUID(int=observation_id),
        investigation_id=UUID(int=900),
        kind=kind,
        blob_id=blob_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        source=source,
        source_version="fixture-v1",
        observed_at=observed_at,
        object_type=object_type,
        object_id=object_id,
    )
    return observation, (blob_id, raw)


@pytest.mark.asyncio
async def test_projects_hash_to_vt_domain_to_shodan_ip_with_canonical_provenance() -> None:
    seed_hash = "a" * 64
    publication = _publication_with_seed_hash(seed_hash)
    vt_observation, vt_blob = _observation(
        observation_id=1,
        kind=EvidenceKind.VT_RELATIONSHIP,
        object_type="file",
        object_id=seed_hash,
        source="VirusTotal",
        observed_at=datetime(2026, 10, 17, 9, 0, tzinfo=UTC),
        payload={
            "schema": "virustotal-investigation-summary.v1",
            "relation": "contacted_domains",
            "observed_count": 1,
            "descriptors": [{"type": "domain", "identifier": "edge.example"}],
        },
    )
    shodan_observation, shodan_blob = _observation(
        observation_id=2,
        kind=EvidenceKind.SHODAN_OBSERVATION,
        object_type="domain",
        object_id="edge.example",
        source="shodan",
        observed_at=datetime(2026, 10, 17, 10, 0, tzinfo=UTC),
        payload={
            "schema": "shodan-observation.v1",
            "captured_at": "2026-10-17T10:00:00+00:00",
            "object": {"type": "domain", "value": "edge.example"},
            "summary": {
                "domain": "edge.example",
                "subdomains": [],
                "dns_records": [
                    {"type": "A", "value": "198.51.100.42", "last_seen": "2026-10-16T12:00:00Z"}
                ],
            },
        },
    )
    resolution_observation, resolution_blob = _observation(
        observation_id=6,
        kind=EvidenceKind.VT_RELATIONSHIP,
        object_type="ip_address",
        object_id="198.51.100.42",
        source="VirusTotal",
        observed_at=datetime(2026, 10, 17, 11, 0, tzinfo=UTC),
        payload={
            "schema": "virustotal-investigation-summary.v1",
            "relation": "resolutions",
            "descriptors": [{"type": "domain", "identifier": "edge.example"}],
        },
    )
    related_observation, related_blob = _observation(
        observation_id=8,
        kind=EvidenceKind.VT_RELATIONSHIP,
        object_type="file",
        object_id=seed_hash,
        source="VirusTotal",
        observed_at=datetime(2026, 10, 17, 11, 30, tzinfo=UTC),
        payload={
            "schema": "virustotal-investigation-summary.v1",
            "relation": "related_files",
            "descriptors": [{"type": "file", "identifier": "2" * 64}],
        },
    )
    reader = _BlobReader(dict((vt_blob, shodan_blob, resolution_blob, related_blob)))
    source = InvestigationGraphSource(artifact_reader=reader)

    graph = await source.project(
        investigation_id=UUID(int=900),
        publication=publication,
        observations=(
            vt_observation,
            shodan_observation,
            resolution_observation,
            related_observation,
        ),
        findings=(),
    )

    assert graph is not None
    nodes_by_id = {node.node_id: node for node in graph.nodes}
    edges = {
        (
            nodes_by_id[edge.source_node_id].value,
            nodes_by_id[edge.target_node_id].value,
            edge.relation,
        ): edge
        for edge in graph.edges
    }
    vt_edge = edges[(seed_hash, "edge.example", "contacted_domains")]
    dns_edge = edges[("edge.example", "198.51.100.42", "dns:A")]
    reverse_vt_edge = edges[("edge.example", "198.51.100.42", "resolutions")]
    undirected_edge = edges[(seed_hash, "2" * 64, "related_files")]
    assert vt_edge.source_kind.value == "virustotal"
    assert vt_edge.evidence_basis.value == "provider_assertion"
    assert vt_edge.confidence.value == "unassessed"
    assert vt_edge.evidence_refs[0].observation_id == vt_observation.id
    assert vt_edge.evidence_refs[0].sha256 == vt_observation.sha256
    assert dns_edge.source_kind.value == "passive_dns"
    assert dns_edge.evidence_refs[0].source_name == "shodan"
    assert dns_edge.direction.value == "pivot_to_target"
    assert dns_edge.evidence_basis.value == "provider_assertion"
    assert dns_edge.observed_at == datetime(2026, 10, 16, 12, 0, tzinfo=UTC)
    assert reverse_vt_edge.direction.value == "target_to_pivot"
    assert reverse_vt_edge.source_kind.value == "virustotal"
    assert undirected_edge.direction.value == "undirected"
    assert any(edge.source_kind.value == "article" for edge in graph.edges)

    repository_graph = await source.project(
        investigation_id=UUID(int=900),
        publication=publication,
        observation_repository=_ObservationRepository(
            (
                vt_observation,
                shodan_observation,
                resolution_observation,
                related_observation,
            )
        ),
        finding_repository=_FindingRepository(),
    )
    assert repository_graph == graph

    figure = build_investigation_graph_figure(publication, graph)
    assert figure is not None
    assert figure["graph_sha256"] == graph.sha256
    assert "contacted_domains" in figure["d2_source"]
    assert " -- " in figure["d2_source"]
    assert "confiance" in figure["caption"].lower()


@pytest.mark.asyncio
async def test_negative_empty_and_hash_mismatched_observations_do_not_make_edges() -> None:
    seed_hash = "b" * 64
    publication = _publication_with_seed_hash(seed_hash)
    negative, negative_blob = _observation(
        observation_id=3,
        kind=EvidenceKind.NEGATIVE_RESULT,
        object_type="file",
        object_id=seed_hash,
        source="VirusTotal",
        observed_at=datetime(2026, 10, 17, tzinfo=UTC),
        payload={
            "schema": "investigation-negative-result.v1",
            "reason": "empty relationship result",
            "details": {"relation": "contacted_ips", "status": "empty", "http_status": 404},
        },
    )
    bad_observation, bad_blob = _observation(
        observation_id=4,
        kind=EvidenceKind.VT_RELATIONSHIP,
        object_type="file",
        object_id=seed_hash,
        source="VirusTotal",
        observed_at=datetime(2026, 10, 17, tzinfo=UTC),
        payload={
            "schema": "virustotal-investigation-summary.v1",
            "relation": "contacted_ips",
            "descriptors": [{"type": "ip_address", "identifier": "203.0.113.55"}],
        },
    )
    tampered = replace(bad_observation, sha256="0" * 64)
    source = InvestigationGraphSource(
        artifact_reader=_BlobReader({negative_blob[0]: negative_blob[1], bad_blob[0]: bad_blob[1]})
    )

    graph = await source.project(
        investigation_id=UUID(int=900),
        publication=publication,
        observations=(negative, tampered),
        findings=(),
    )

    assert graph is None
    assert build_investigation_graph_figure(publication, graph) is None


@pytest.mark.asyncio
async def test_projection_is_deterministic_and_reports_omitted_nodes() -> None:
    seed_hash = "c" * 64
    publication = _publication_with_seed_hash(seed_hash)
    observation, blob = _observation(
        observation_id=5,
        kind=EvidenceKind.VT_RELATIONSHIP,
        object_type="file",
        object_id=seed_hash,
        source="VirusTotal",
        observed_at=datetime(2026, 10, 17, 11, 0, tzinfo=UTC),
        payload={
            "schema": "virustotal-investigation-summary.v1",
            "relation": "contacted_domains",
            "descriptors": [
                {"type": "domain", "identifier": f"host-{index:02d}.example"} for index in range(12)
            ],
        },
    )
    source = InvestigationGraphSource(artifact_reader=_BlobReader({blob[0]: blob[1]}), max_nodes=6)

    first = await source.project(
        investigation_id=UUID(int=900),
        publication=publication,
        observations=(observation,),
        findings=(),
    )
    second = await source.project(
        investigation_id=UUID(int=900),
        publication=publication,
        observations=(observation,),
        findings=(),
    )

    assert first is not None
    assert first == second
    assert len(first.nodes) <= 6
    assert first.omitted_count > 0
    figure = build_investigation_graph_figure(publication, first)
    assert figure is not None
    assert f"{first.omitted_count} autres omis" in figure["d2_source"]
    assert "domaines regroupés" in figure["d2_source"]
    assert first.omitted_edge_count > 0


@pytest.mark.asyncio
async def test_v6_graph_round_trip_and_legacy_payload_compatibility() -> None:
    publication = _publication_with_timeline()
    opinion, context = _approved_opinion(publication)
    legacy_v6 = augment_publication_with_opinion(publication, opinion, context)
    legacy_payload = serialize_publication_document(legacy_v6)

    assert "infrastructure_graph" not in legacy_payload
    parsed_legacy = publication_document_from_json(legacy_payload)
    assert serialize_publication_document(parsed_legacy) == legacy_payload

    observation, blob = _observation(
        observation_id=7,
        kind=EvidenceKind.VT_RELATIONSHIP,
        object_type="file",
        object_id="f" * 64,
        source="VirusTotal",
        observed_at=datetime(2026, 10, 17, 12, 0, tzinfo=UTC),
        payload={
            "schema": "virustotal-investigation-summary.v1",
            "relation": "contacted_domains",
            "descriptors": [{"type": "domain", "identifier": "roundtrip.example"}],
        },
    )
    seeded_publication = _publication_with_seed_hash("f" * 64)
    opinion_for_seed, context_for_seed = _approved_opinion(seeded_publication)
    observation = replace(observation, investigation_id=opinion_for_seed.investigation_id)
    source = InvestigationGraphSource(artifact_reader=_BlobReader({blob[0]: blob[1]}))
    graph = await source.project(
        investigation_id=opinion_for_seed.investigation_id,
        publication=seeded_publication,
        observations=(observation,),
        findings=(),
    )
    assert graph is not None
    graph_v6 = augment_publication_with_opinion(
        seeded_publication,
        opinion_for_seed,
        context_for_seed,
        infrastructure_graph=graph,
    )
    graph_payload = serialize_publication_document(graph_v6)
    parsed_graph = publication_document_from_json(graph_payload)
    assert parsed_graph.infrastructure_graph == graph
