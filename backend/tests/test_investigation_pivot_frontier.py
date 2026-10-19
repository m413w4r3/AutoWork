from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from cti_app.application.investigation_contracts import (
    ArticleContextV1,
    ConfirmedIOCContextV1,
    ToolEffect,
    ToolSummaryV1,
)
from cti_app.application.investigation_pivot_frontier import (
    build_pivot_frontier,
    discovered_indicator_values,
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
        "endpoints": [{"address": "203.0.113.9", "port": 443}],
        "dns_observations": [
            {"name": "dns.query", "value": "cdn.example[.]com", "frame_number": 1}
        ],
        "http_observations": [],
        "tls_observations": [],
    }

    frontier = build_pivot_frontier(
        investigation_id=str(observation.investigation_id),
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): payload},
    )

    by_value = {item.value: item for item in frontier.records}
    ip_record = by_value["203.0.113.9"]
    domain_record = by_value["cdn[.]example[.]com"]
    assert ip_record.level is PivotQualificationLevel.DISCOVERED
    assert domain_record.source_observation_id == str(observation.id)
    assert domain_record.source_sha256 == observation.sha256
    assert domain_record.path == (domain_record.id,)
    assert any(path.target_id == domain_record.id for path in frontier.paths)
    assert set(discovered_indicator_values((observation,), {str(observation.id): payload})) == {
        ("ipv4", "203.0.113.9"),
        ("domain", "cdn[.]example[.]com"),
    }


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
        snapshot=_snapshot(article_hash),
        observations=(observation,),
        observation_payloads={str(observation.id): summary},
        envelope=envelope,
    )

    candidate = next(item for item in frontier.records if item.value == candidate_hash)
    assert candidate.level is PivotQualificationLevel.DISCOVERED
    assert candidate.level is not PivotQualificationLevel.CONFIRMED
    assert candidate.g2_state is PivotG2State.BLOCKED
    assert candidate.disclosure_state.value == "blocked"
