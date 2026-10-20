from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError

from cti_app.api.investigation import (
    PivotFrontierResponseV1,
    get_investigation_pivot_frontier,
)
from cti_app.application.investigation_contracts import (
    ArticleContextV1,
    ConfirmedIOCContextV1,
    ToolEffect,
    ToolSummaryV1,
)
from cti_app.application.investigation_pivot_frontier import (
    build_pivot_frontier,
    discovered_indicator_values,
    frontier_to_dict,
    normalize_pivot_indicator,
)
from cti_app.application.investigation_pivots import available_investigation_pivots
from cti_app.application.research_context_builder import ArticleSnapshot
from cti_app.domain.investigation import (
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    InvestigationBudget,
    InvestigationEnvelope,
    ToolInvocation,
    ToolInvocationStatus,
)
from cti_app.domain.investigation_pivot_frontier import (
    PivotExecutionState,
    PivotFrontierV1,
    PivotG2State,
    PivotQualificationLevel,
)


def _snapshot(article_hash: str) -> ArticleSnapshot:
    artifact_id = uuid4()
    return ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=artifact_id,
        synthesis_sha256="1" * 64,
        article=ArticleContextV1(
            short_summary="Synthetic article",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
        tlp="green",
        confirmed_iocs=(
            ConfirmedIOCContextV1(
                indicator_type="sha256",
                value=article_hash,
                extraction_artifact_id=uuid4(),
                extraction_artifact_version=1,
                source_document_ids=(uuid4(),),
            ),
        ),
    )


def _relation_observation(
    parent_hash: str, digest: str, *, investigation_id=None
) -> EvidenceObservation:
    return EvidenceObservation(
        investigation_id=investigation_id or uuid4(),
        kind=EvidenceKind.VT_RELATIONSHIP,
        blob_id=uuid4(),
        sha256=digest,
        source="VirusTotal",
        source_version="proxy-v3",
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
        object_type="file",
        object_id=parent_hash,
        limitations=("La relation est fournie par VT.",),
    )


def _frontier_tools() -> tuple[ToolSummaryV1, ...]:
    return (
        ToolSummaryV1(
            name="vt.file_report",
            schema_version="investigation-vt-file-report.v1",
            effect=ToolEffect.EXTERNAL_READ,
            arguments_schema={
                "type": "object",
                "properties": {"file_hash": {"type": "string"}},
                "required": ["file_hash"],
                "additionalProperties": False,
            },
        ),
    )


def _frontier_for_descriptors(
    descriptors: list[dict[str, str]], *, snapshot: ArticleSnapshot | None = None
) -> PivotFrontierV1:
    article_hash = "a" * 64
    observation = _relation_observation(article_hash, "2" * 64)
    summary = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "contacted_domains",
        "observed_count": len(descriptors),
        "descriptors": descriptors,
        "pagination_complete": False,
    }
    return build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=snapshot or _snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): summary},
    )


def test_normalization_keeps_ipv6_and_case_sensitive_url_identity() -> None:
    ipv6 = normalize_pivot_indicator("ip_address", "2001:0db8::1")
    upper_path = normalize_pivot_indicator("url", "HTTPS://EXAMPLE.COM/Admin")
    lower_path = normalize_pivot_indicator("url", "https://example.com/admin")

    assert ipv6 is not None
    assert (ipv6.indicator_type, ipv6.value, ipv6.canonical_id) == (
        "ipv6",
        "2001:db8::1",
        "ipv6:2001:db8::1",
    )
    assert upper_path is not None and lower_path is not None
    assert upper_path.canonical_id != lower_path.canonical_id
    assert upper_path.value == "hxxps://example[.]com/Admin"


def test_vt_relation_adds_new_hash_with_proof_path_and_open_decision() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    observation = _relation_observation(article_hash, "2" * 64)
    summary = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "dropped_files",
        "observed_count": 1,
        "descriptors": [{"type": "file", "identifier": candidate_hash}],
        "input_cursor": "page-1",
        "next_cursor": "page-2",
        "pagination_complete": False,
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): summary},
    )

    discovered = next(item for item in frontier.records if item.value == candidate_hash)
    root = next(item for item in frontier.records if item.value == article_hash)
    assert discovered.level is PivotQualificationLevel.DISCOVERED
    assert discovered.source_observation_id == str(observation.id)
    assert discovered.source_sha256 == observation.sha256
    assert discovered.parent_id == root.id
    assert discovered.path == (root.id, discovered.id)
    assert discovered.novelty_vs_article is True
    assert discovered.g2_state is PivotG2State.REQUIRED
    assert discovered.state is PivotExecutionState.QUEUED
    assert discovered.cursor == "page-2"
    assert discovered.pagination_complete is False
    assert "famille" in " ".join(discovered.limitations)
    assert len(frontier.edges) == 1
    assert frontier.edges[0].relation == "dropped_files"
    assert frontier.paths[-1].node_ids == (root.id, discovered.id)


def test_omitted_vt_payload_does_not_create_a_pivot_or_evidence_edge() -> None:
    article_hash = "a" * 64
    observation = _relation_observation(article_hash, "2" * 64)

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): None},
    )

    assert len(frontier.records) == 1
    assert frontier.records[0].source == "article"
    assert str(observation.id) in " ".join(frontier.records[0].limitations)
    assert "preuve ni comme pivot" in " ".join(frontier.records[0].limitations)
    assert frontier.edges == ()


async def test_pivot_frontier_api_accepts_c1_strings_and_keeps_contract_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from collections.abc import AsyncIterator
    from contextlib import asynccontextmanager

    from cti_app.api import investigation as investigation_api

    article_hash = "a" * 64
    investigation_id = uuid4()
    observation = _relation_observation(article_hash, "2" * 64)
    snapshot = _snapshot(article_hash)
    investigation = SimpleNamespace(synthesis_artifact_id=snapshot.synthesis_artifact_id)

    class _Rows:
        async def get(self, _identifier: object) -> SimpleNamespace:
            return investigation

        async def list_for_investigation(self, _identifier: object) -> tuple[object, ...]:
            return ()

    uow = SimpleNamespace(
        analyst_investigations=_Rows(),
        evidence_observations=SimpleNamespace(
            list_for_investigation=lambda _identifier: _observations()
        ),
        tool_invocations=SimpleNamespace(list_for_investigation=lambda _identifier: _empty_rows()),
        investigation_envelopes=SimpleNamespace(get=lambda _identifier: _no_envelope()),
    )

    @asynccontextmanager
    async def _unit_of_work() -> AsyncIterator[SimpleNamespace]:
        yield uow

    async def _observations() -> tuple[EvidenceObservation, ...]:
        return (observation,)

    async def _empty_rows() -> tuple[object, ...]:
        return ()

    async def _no_envelope() -> None:
        return None

    async def _resolve_snapshot(_artifact_id: object) -> ArticleSnapshot:
        return snapshot

    async def _observation_payloads(
        _request: object,
        _uow: object,
        observations: tuple[EvidenceObservation, ...],
    ) -> dict[str, None]:
        assert observations == (observation,)
        return {str(observation.id): None}

    monkeypatch.setattr(investigation_api, "_uow_factory", lambda _request: _unit_of_work)
    monkeypatch.setattr(
        investigation_api,
        "_composition_factory",
        lambda _request: SimpleNamespace(
            article_snapshots=SimpleNamespace(resolve=_resolve_snapshot)
        ),
    )
    monkeypatch.setattr(investigation_api, "_pivot_observation_payloads", _observation_payloads)

    response = await get_investigation_pivot_frontier(
        investigation_id, SimpleNamespace(), limit=50, cursor=None
    )

    root = next(node for node in response.nodes if node.source == "article")
    assert response.investigation_id == investigation_id
    assert root.level is PivotQualificationLevel.DOCUMENTED
    assert str(observation.id) in " ".join(root.limitations)
    assert "preuve ni comme pivot" in " ".join(root.limitations)

    raw_payload = frontier_to_dict(
        build_pivot_frontier(
            investigation_id=str(investigation_id),
            snapshot=snapshot,
            observations=(observation,),
            observation_payloads={str(observation.id): None},
        )
    )
    raw_payload["investigation_id"] = investigation_id
    raw_payload["nodes"] = raw_payload.pop("records")
    assert isinstance(raw_payload["nodes"][0]["level"], str)
    raw_payload["nodes"][0]["confidence"] = 1.1
    with pytest.raises(ValidationError):
        PivotFrontierResponseV1.model_validate(raw_payload, strict=False)


def test_frontier_preserves_distinct_relations_but_deduplicates_candidate_values() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    first = _relation_observation(article_hash, "2" * 64)
    second = _relation_observation(article_hash, "3" * 64, investigation_id=first.investigation_id)
    payloads = {
        str(first.id): {
            "schema": "virustotal-investigation-summary.v1",
            "relation": "dropped_files",
            "observed_count": 1,
            "descriptors": [{"type": "file", "identifier": candidate_hash}],
        },
        str(second.id): {
            "schema": "virustotal-investigation-summary.v1",
            "relation": "communicating_files",
            "observed_count": 1,
            "descriptors": [{"type": "file", "identifier": candidate_hash}],
        },
    }

    frontier = build_pivot_frontier(
        investigation_id=str(first.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(first, second),
        observation_payloads=payloads,
    )

    candidates = [item for item in frontier.records if item.value == candidate_hash]
    assert len(candidates) == 2
    assert len({item.canonical_id for item in candidates}) == 1
    assert len({item.id for item in candidates}) == 2
    assert {item.relation for item in frontier.edges} == {
        "dropped_files",
        "communicating_files",
    }
    assert discovered_indicator_values((first, second), payloads) == (("sha256", candidate_hash),)


def test_duplicate_source_and_reason_share_pivot_but_keep_each_evidence_edge() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    first = _relation_observation(article_hash, "2" * 64)
    second = _relation_observation(article_hash, "3" * 64, investigation_id=first.investigation_id)
    payload = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "dropped_files",
        "observed_count": 1,
        "descriptors": [{"type": "file", "identifier": candidate_hash}],
    }

    frontier = build_pivot_frontier(
        investigation_id=str(first.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(first, second),
        observation_payloads={str(first.id): payload, str(second.id): payload},
    )

    candidates = [item for item in frontier.records if item.value == candidate_hash]
    assert len(candidates) == 1
    assert len(frontier.edges) == 2
    assert {edge.source_observation_id for edge in frontier.edges} == {
        str(first.id),
        str(second.id),
    }


def test_capture_observations_add_ip_and_domain_pivots_with_source_paths() -> None:
    article_hash = "a" * 64
    observation = EvidenceObservation(
        investigation_id=uuid4(),
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256="2" * 64,
        source="network.capture.inspect",
        source_version="1",
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
        object_type="network_capture_report",
        object_id="3" * 64,
        limitations=("Sortie de capture bornée.",),
    )
    payload = {
        "schema": "NetworkCaptureReportV1",
        "schema_version": "network-capture-report-v1",
        "endpoints": [
            {"address": "93.184.216.34", "port": 443},
            {"address": "192.168.122.14", "port": 443},
        ],
        "dns_observations": [
            {"name": "dns.query", "value": "cdn.example[.]com", "frame_number": 1}
        ],
        "http_observations": [{"name": "http.host", "value": "dns.google", "frame_number": 2}],
        "tls_observations": [],
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): payload},
    )

    by_value = {item.value: item for item in frontier.records}
    ip_record = by_value["93.184.216.34"]
    private_ip_record = by_value["192.168.122.14"]
    domain_record = by_value["cdn[.]example[.]com"]
    background_domain_record = by_value["dns[.]google"]
    assert ip_record.level is PivotQualificationLevel.DISCOVERED
    assert private_ip_record.level is PivotQualificationLevel.REJECTED
    assert "non_pivotable_non_global_ip_address" in private_ip_record.reason
    assert private_ip_record.priority_score == 0
    assert domain_record.source_observation_id == str(observation.id)
    assert domain_record.source_sha256 == observation.sha256
    assert domain_record.path == (domain_record.id,)
    assert any(path.target_id == domain_record.id for path in frontier.paths)
    assert background_domain_record.level is PivotQualificationLevel.REJECTED
    assert (
        "benign_background_infrastructure:list-v2:not_pivotable" in background_domain_record.reason
    )
    assert background_domain_record.priority_score == 0
    assert set(discovered_indicator_values((observation,), {str(observation.id): payload})) == {
        ("ipv4", "93.184.216.34"),
        ("domain", "cdn[.]example[.]com"),
    }


@pytest.mark.parametrize(
    "address",
    [
        "192.168.122.14",
        "127.0.0.1",
        "169.254.10.20",
        "100.64.0.1",
        "192.0.2.10",
        "198.18.0.1",
        "240.0.0.1",
        "::1",
        "fc00::1",
        "fe80::1",
        "2001:db8::1",
    ],
)
def test_non_global_ip_discoveries_are_rejected_and_have_zero_priority(address: str) -> None:
    frontier = _frontier_for_descriptors([{"type": "ip_address", "identifier": address}])

    node = next(item for item in frontier.records if item.value == address)
    assert node.level is PivotQualificationLevel.REJECTED
    assert "non_pivotable_non_global_ip_address" in node.reason
    assert "ipaddress" in " ".join(node.limitations)
    assert node.priority_score == 0
    assert "classification_adjustment(-100)" in node.priority_factors.formula
    # Novelty describes evidence coverage; it is not a maliciousness verdict.
    assert node.novelty_vs_verified is True
    assert node.priority_factors.novelty_vs_verified == 1


def test_known_benign_background_infrastructure_stays_visible_but_not_pivotable() -> None:
    frontier = _frontier_for_descriptors(
        [
            {"type": "ip_address", "identifier": "8.8.8.8"},
            {"type": "domain", "identifier": "dns.google"},
            {
                "type": "url",
                "identifier": "https://fe3cr.delivery.mp.microsoft.com/fs/fe3cr.delivery.mp.microsoft.com",
            },
            {"type": "domain", "identifier": "config.edge.skype.com"},
            {"type": "domain", "identifier": "c.pki.goog"},
            {"type": "ip_address", "identifier": "142.251.184.94"},
        ]
    )

    nodes = [item for item in frontier.records if item.source != "article"]
    assert len(nodes) == 6
    for node in nodes:
        assert node.level is PivotQualificationLevel.REJECTED
        assert "benign_background_infrastructure:list-v2:not_pivotable" in node.reason
        assert "Infrastructure publique de fond" in " ".join(node.limitations)
        assert "classification_adjustment(-100)" in node.priority_factors.formula
        assert node.priority_score == 0
        assert node.state is PivotExecutionState.QUEUED


def test_unknown_public_ip_and_similar_domain_remain_pivotable() -> None:
    frontier = _frontier_for_descriptors(
        [
            {"type": "ip_address", "identifier": "93.184.216.34"},
            {"type": "domain", "identifier": "dns.google.evil.example"},
            {"type": "domain", "identifier": "config-edge-skype.example"},
        ]
    )

    nodes = [item for item in frontier.records if item.source != "article"]
    assert len(nodes) == 3
    for node in nodes:
        assert node.level is PivotQualificationLevel.DISCOVERED
        assert "benign_background_infrastructure" not in node.reason
        assert "classification_adjustment(" not in node.priority_factors.formula
        assert node.priority_score == 80


def test_list_v2_covers_service_suffixes_and_public_dns_resolvers() -> None:
    frontier = _frontier_for_descriptors(
        [
            {"type": "domain", "identifier": "fs.microsoft.com"},
            {"type": "domain", "identifier": "download.windowsupdate.com"},
            {"type": "domain", "identifier": "config.edge.skype.com"},
            {"type": "domain", "identifier": "assets.msedge.net"},
            *(
                {"type": "ip_address", "identifier": address}
                for address in (
                    "1.1.1.1",
                    "1.0.0.1",
                    "8.8.8.8",
                    "8.8.4.4",
                    "9.9.9.9",
                )
            ),
        ]
    )

    nodes = [item for item in frontier.records if item.source != "article"]
    assert len(nodes) == 9
    for node in nodes:
        assert node.level is PivotQualificationLevel.REJECTED
        assert "benign_background_infrastructure:list-v2:not_pivotable" in node.reason


def test_observed_cloudflare_address_stays_pivotable_with_shared_cdn_limitation() -> None:
    frontier = _frontier_for_descriptors([{"type": "ip_address", "identifier": "162.159.36.2"}])

    node = next(item for item in frontier.records if item.value == "162.159.36.2")
    assert node.level is PivotQualificationLevel.DISCOVERED
    assert node.priority_score == 80
    assert "hébergement mutualisé/CDN possible" in " ".join(node.limitations)
    assert "benign_background_infrastructure" not in node.reason


def test_documented_microsoft_service_ioc_is_never_downgraded_by_list_v2() -> None:
    value = "fs.microsoft.com"
    snapshot = replace(
        _snapshot("a" * 64),
        confirmed_iocs=(
            ConfirmedIOCContextV1(
                indicator_type="domain",
                value=value,
                extraction_artifact_id=uuid4(),
                extraction_artifact_version=1,
                source_document_ids=(uuid4(),),
            ),
        ),
    )
    frontier = _frontier_for_descriptors(
        [{"type": "domain", "identifier": value}], snapshot=snapshot
    )

    matching = [item for item in frontier.records if item.canonical_id == "domain:fs.microsoft.com"]
    assert any(item.level is PivotQualificationLevel.DOCUMENTED for item in matching)
    assert all(item.level is not PivotQualificationLevel.REJECTED for item in matching)


def test_article_documented_private_ip_is_never_downgraded() -> None:
    value = "192.168.122.77"
    base = _snapshot("a" * 64)
    snapshot = replace(
        base,
        confirmed_iocs=(
            ConfirmedIOCContextV1(
                indicator_type="ipv4",
                value=value,
                extraction_artifact_id=uuid4(),
                extraction_artifact_version=1,
                source_document_ids=(uuid4(),),
            ),
        ),
    )
    frontier = _frontier_for_descriptors(
        [{"type": "ip_address", "identifier": value}], snapshot=snapshot
    )

    matching_nodes = [item for item in frontier.records if item.value == value]
    documented = next(item for item in matching_nodes if item.source == "article")
    assert documented.level is PivotQualificationLevel.DOCUMENTED
    assert all(item.level is not PivotQualificationLevel.REJECTED for item in matching_nodes)
    assert all(
        "classification_adjustment(" not in item.priority_factors.formula for item in matching_nodes
    )


def test_similarity_observation_adds_associated_samples_without_family_confirmation() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    observation = EvidenceObservation(
        investigation_id=uuid4(),
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256="2" * 64,
        source="sample_similarity",
        source_version="family-feature-comparison-v1",
        observed_at=datetime(2026, 10, 19, tzinfo=UTC),
        object_type="family_feature_comparison",
        object_id="3" * 64,
        limitations=("La comparaison ne confirme pas une parenté.",),
    )
    payload = {
        "schema_version": "family-feature-comparison-v1",
        "comparison_sample_sha256s": [candidate_hash],
        "features": [],
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): payload},
    )

    sample = next(item for item in frontier.records if item.value == candidate_hash)
    assert sample.level is PivotQualificationLevel.ASSOCIATED
    assert sample.reason == "sample_similarity:comparison_input"
    assert sample.source_observation_id == str(observation.id)
    assert sample.state is PivotExecutionState.QUEUED
    assert sample.level is not PivotQualificationLevel.CONFIRMED
    assert discovered_indicator_values((observation,), {str(observation.id): payload}) == (
        ("sha256", candidate_hash),
    )


def test_discovered_hash_becomes_report_pivot_without_reprocessing_covered_article_ioc() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    observation = _relation_observation(article_hash, "2" * 64)
    summary = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "dropped_files",
        "observed_count": 1,
        "descriptors": [{"type": "file", "identifier": candidate_hash}],
    }
    already_reported = ToolInvocation(
        investigation_id=observation.investigation_id,
        operation_key="root-report-operation",
        tool_name="vt.file_report",
        schema_version="investigation-vt-file-report.v1",
        normalized_parameters={"file_hash": article_hash},
        purpose="test prior IOC report",
        status=ToolInvocationStatus.SUCCEEDED,
    )

    pivots, truncated = available_investigation_pivots(
        inventory=(),
        discovered_inventory=discovered_indicator_values(
            (observation,), {str(observation.id): summary}
        ),
        tools=_frontier_tools(),
        tool_coverage=(),
        invocations=(already_reported,),
        observations=(observation,),
    )

    assert truncated is False
    assert [(item.tool_name, item.indicator_value) for item in pivots] == [
        ("vt.file_report", candidate_hash)
    ]


def test_invalid_hash_or_missing_cursor_does_not_create_a_confirmed_pivot() -> None:
    article_hash = "a" * 64
    observation = _relation_observation(article_hash, "2" * 64)
    payload = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "dropped_files",
        "observed_count": 2,
        "descriptors": [
            {"type": "file", "identifier": "not-a-hash"},
            {"type": "file", "identifier": "b" * 64},
        ],
        "pagination_complete": False,
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): payload},
    )

    candidates = [item for item in frontier.records if item.value == "b" * 64]
    assert len(candidates) == 1
    assert candidates[0].level is not PivotQualificationLevel.CONFIRMED
    assert candidates[0].pagination_complete is False
    assert candidates[0].state is PivotExecutionState.QUEUED
    assert candidates[0].cursor is None


def test_succeeded_invocation_with_cursor_remains_queued_for_next_page() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    observation = _relation_observation(article_hash, "2" * 64)
    invocation = ToolInvocation(
        investigation_id=observation.investigation_id,
        operation_key="page-one",
        tool_name="vt.relationships",
        schema_version="investigation-vt-relationships.v1",
        normalized_parameters={
            "object_type": "file",
            "object_id": article_hash,
            "relation": "dropped_files",
        },
        purpose="test continuation cursor",
        status=ToolInvocationStatus.SUCCEEDED,
        response_refs=(observation.id,),
    )
    summary = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "dropped_files",
        "observed_count": 1,
        "descriptors": [{"type": "file", "identifier": candidate_hash}],
        "next_cursor": "page-two",
        "pagination_complete": False,
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): summary},
        invocations=(invocation,),
    )

    candidate = next(item for item in frontier.records if item.value == candidate_hash)
    assert candidate.state is PivotExecutionState.QUEUED
    assert candidate.cursor == "page-two"
    assert candidate.pagination_complete is False


def test_non_authorized_discovery_stays_unconfirmed_and_g2_blocked() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    observation = _relation_observation(article_hash, "2" * 64)
    envelope = InvestigationEnvelope(
        investigation_id=observation.investigation_id,
        allowed_providers=("shodan",),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(do_not_submit=True),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    summary = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "dropped_files",
        "observed_count": 1,
        "descriptors": [{"type": "file", "identifier": candidate_hash}],
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=replace(_snapshot(article_hash), do_not_submit=False),
        observations=(observation,),
        observation_payloads={str(observation.id): summary},
        envelope=envelope,
    )

    candidate = next(item for item in frontier.records if item.value == candidate_hash)
    assert candidate.level is PivotQualificationLevel.DISCOVERED
    assert candidate.level is not PivotQualificationLevel.CONFIRMED
    assert candidate.g2_state is PivotG2State.BLOCKED
    assert candidate.disclosure_state.value == "blocked"


def test_approved_envelope_states_apply_to_derived_nodes() -> None:
    article_hash = "a" * 64
    candidate_hash = "b" * 64
    observation = _relation_observation(article_hash, "2" * 64)
    envelope = InvestigationEnvelope(
        investigation_id=observation.investigation_id,
        allowed_providers=("virustotal",),
        budget=InvestigationBudget(),
        acquire_hits_allowed=True,
        disclosure=DisclosureBounds(),
        approval_decision_id=uuid4(),
        approval_actor_id="analyst-1",
    )
    summary = {
        "schema": "virustotal-investigation-summary.v1",
        "relation": "dropped_files",
        "observed_count": 1,
        "descriptors": [{"type": "file", "identifier": candidate_hash}],
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=replace(_snapshot(article_hash), do_not_submit=False),
        observations=(observation,),
        observation_payloads={str(observation.id): summary},
        envelope=envelope,
    )

    documented = next(item for item in frontier.records if item.source == "article")
    candidate = next(item for item in frontier.records if item.value == candidate_hash)
    assert documented.g2_state is PivotG2State.NOT_APPLICABLE
    assert candidate.level is PivotQualificationLevel.DISCOVERED
    assert candidate.g2_state is PivotG2State.APPROVED
    assert candidate.disclosure_state.value == "allowed"
