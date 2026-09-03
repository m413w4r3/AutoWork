"""End-to-end IOC coverage invariant over a deterministic three-source corpus.

The corpus is fixed: one source publishes an explicit IOC table for the
requested subject (86 values), one mentions network values as prose context
only, and one publishes another campaign's indicators.  The pipeline is
exercised from the Q2 wire format down to ``collect_indicators`` and the
publication document, comparing sets: a single explicit IOC lost anywhere
between the parser and publication fails this module.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from cti_app.application.production_artifact_verification import (
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_normalization import canonical_indicator_key
from cti_app.application.production_parsers import (
    DisplayPolicy,
    IndicatorStatus,
    ParsedEvent,
    ParsedSource,
    Q2ArtifactProposal,
    Q2SourceOutput,
    ReferenceReport,
    TechnicalExtraction,
    parse_q2_proposals_markdown,
    technical_extraction_from_json,
    technical_extraction_to_json,
)
from cti_app.application.production_rendering import (
    build_reference_numbering,
    collect_indicators,
    render_publication_markdown,
)
from cti_app.application.production_source_evidence import (
    source_evidence_document_from_html,
    verify_ioc_rules_output_against_source,
    verify_q2_output_against_source,
)
from cti_app.application.publication_builder import build_publication_document
from cti_app.domain.discovery import SourceRole
from cti_app.domain.publication import ArtifactType

FIXTURES = Path(__file__).parent / "fixtures"

S1_IPS = (
    *(f"185.199.108.{index}" for index in range(1, 11)),
    *(f"45.61.136.{index}" for index in range(1, 10)),
    *(f"198.51.100.{index}" for index in range(20, 28)),
)
S1_DOMAINS = (
    *(f"uae{index}.locat.sbs" for index in range(1, 15)),
    "locat.sbs",
    "tiktok-u.sbs",
    "cloud.tiktok-u.sbs",
    *(f"node-{index:02d}.security-lab.io" for index in range(1, 41)),
)
S1_HASHES = (
    "5d41402abc4b2a76b9719d911017c592",
    "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824",
)
S2_DOMAINS = ("cdn.partner-hosting.io", "mail.partner-hosting.io")
S2_IPS = ("192.0.2.10",)
S3_IPS = tuple(f"203.0.113.{index}" for index in range(40, 48))
S3_DOMAINS = tuple(f"relay-{index}.other-campaign.io" for index in range(1, 8))

EXPECTED_EXPLICIT_IOCS = frozenset(
    (
        *((ArtifactType.IP.value, value) for value in S1_IPS),
        *((ArtifactType.DOMAIN.value, value) for value in S1_DOMAINS),
        *((ArtifactType.HASH.value, value) for value in S1_HASHES),
    )
)


def _source_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _parse(text: str) -> Q2SourceOutput:
    result = parse_q2_proposals_markdown(text)
    assert result.usable, (result.errors, result.warnings)
    assert result.value is not None
    return result.value


def _group(header: str, values: tuple[str, ...]) -> str:
    return header + "\n" + "\n".join(f"- {value}" for value in values)


def _s1_response() -> str:
    return "\n\n".join(
        (
            _group("IOC confirmed ip", S1_IPS),
            _group("IOC confirmed domain", S1_DOMAINS),
            _group("IOC confirmed md5", S1_HASHES[:1]),
            _group("IOC confirmed sha256", S1_HASHES[1:]),
        )
    )


def _s2_response() -> str:
    return "\n\n".join(
        (
            _group("IOC contextual domain", S2_DOMAINS),
            _group("IOC contextual ip", S2_IPS),
        )
    )


def _canonical_keys(items: object) -> set[tuple[str, str]]:
    keys: set[tuple[str, str]] = set()
    for item in items:  # type: ignore[union-attr]
        artifact_type = (
            item.artifact_type
            if isinstance(item.artifact_type, ArtifactType)
            else ArtifactType(item.artifact_type)
        )
        keys.add((artifact_type.value, canonical_indicator_key(item.value, artifact_type)))
    return keys


def _expected_keys() -> set[tuple[str, str]]:
    return {
        (artifact_type, canonical_indicator_key(value, ArtifactType(artifact_type)))
        for artifact_type, value in EXPECTED_EXPLICIT_IOCS
    }


def _corpus_extraction() -> TechnicalExtraction:
    """Run the deterministic chain for the whole corpus and return Q2 canon."""
    s1_gated = verify_ioc_rules_output_against_source(
        _parse(_s1_response()), _source_text("ioc_coverage_s1_explicit_table.md")
    )
    assert s1_gated.rejections == ()
    s2_gated = verify_ioc_rules_output_against_source(
        _parse(_s2_response()), _source_text("ioc_coverage_s2_contextual_prose.md")
    )
    assert s2_gated.rejections == ()
    # S3 publishes another campaign's indicators; the contract makes the model
    # emit nothing for the requested subject from that source.
    s3_output = Q2SourceOutput()

    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(output=s1_gated.output, source_ids=("S1",)),
            Q2ProposalSubmission(output=s2_gated.output, source_ids=("S2",)),
            Q2ProposalSubmission(output=s3_output, source_ids=("S3",)),
        )
    )
    assert verification.rejected == ()
    return verification.canonical


def test_fixture_corpus_matches_the_declared_expectations() -> None:
    s1 = _source_text("ioc_coverage_s1_explicit_table.md")
    s2 = _source_text("ioc_coverage_s2_contextual_prose.md")
    s3 = _source_text("ioc_coverage_s3_other_actor.md")

    assert len(set(S1_IPS)) == 27
    assert len(set(S1_DOMAINS)) == 57
    assert len(set(S1_HASHES)) == 2
    assert len(EXPECTED_EXPLICIT_IOCS) == 86
    assert all(value in s1 for value in (*S1_IPS, *S1_DOMAINS, *S1_HASHES))
    assert all(value in s2 for value in (*S2_DOMAINS, *S2_IPS))
    assert len(set(S3_IPS + S3_DOMAINS)) == 15
    assert all(value in s3 for value in (*S3_IPS, *S3_DOMAINS))


def test_every_explicit_ioc_survives_from_the_parser_to_collect_indicators() -> None:
    extraction = _corpus_extraction()

    indicators = collect_indicators(extraction)
    assert _canonical_keys(indicators) == _expected_keys()
    assert len(indicators) == 86
    assert all(item.indicator_status is IndicatorStatus.CONFIRMED_IOC for item in indicators)
    assert all(item.source_ids == ("S1",) for item in indicators)


def test_contextual_source_is_retained_without_being_promoted_to_the_ioc_section() -> None:
    extraction = _corpus_extraction()

    contextual = {
        item.value: item
        for item in extraction.items
        if item.indicator_status is IndicatorStatus.CONTEXTUAL
    }
    assert set(contextual) == {*S2_DOMAINS, *S2_IPS}
    assert all(item.display_policy is DisplayPolicy.BODY_ONLY for item in contextual.values())
    assert all(item.source_ids == ("S2",) for item in contextual.values())
    published = {value for _, value in _canonical_keys(collect_indicators(extraction))}
    assert published.isdisjoint({*S2_DOMAINS, *S2_IPS})


def test_another_campaign_never_contaminates_the_requested_subject() -> None:
    extraction = _corpus_extraction()

    values = {item.value for item in extraction.items}
    assert values.isdisjoint({*S3_IPS, *S3_DOMAINS})


def test_explicit_iocs_survive_the_extraction_artifact_round_trip() -> None:
    extraction = _corpus_extraction()

    restored = technical_extraction_from_json(technical_extraction_to_json(extraction))

    assert _canonical_keys(collect_indicators(restored)) == _expected_keys()


def test_explicit_iocs_reach_the_rendered_publication_and_document() -> None:
    extraction = _corpus_extraction()
    report = ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title="Nebula Serpent",
                url="https://security-lab.io/nebula",
                canonical_url="https://security-lab.io/nebula",
                publisher="Security Lab",
                published_at=None,
                role=SourceRole.PRIMARY,
            ),
            ParsedSource(
                local_id="S2",
                title="Telemetry note",
                url="https://partner-hosting.io/note",
                canonical_url="https://partner-hosting.io/note",
                publisher="Partner Hosting",
                published_at=None,
                role=SourceRole.INDEPENDENT,
            ),
        ),
        events=(ParsedEvent(local_id="E1", event_date=None, source_ids=("S1",), text="Campagne."),),
    )
    synthesis = "La campagne Nebula Serpent a reconstruit son infrastructure [S1]."

    numbering = build_reference_numbering(report, synthesis)
    markdown = render_publication_markdown(
        subject_title="[Nebula Serpent] Campagne",
        report=report,
        extraction=extraction,
        synthesis_text=synthesis,
        numbering=numbering,
    )
    assert all(f"`{value}`" in markdown for _, value in EXPECTED_EXPLICIT_IOCS)

    document = build_publication_document(
        subject_title="[Nebula Serpent] Campagne",
        report=report,
        extraction=extraction,
        synthesis_text=synthesis,
    )
    published = {
        (group.artifact_type.value, indicator.value)
        for group in document.indicators
        for indicator in group.values
    }
    assert published == set(EXPECTED_EXPLICIT_IOCS)


def test_similar_subdomains_stay_distinct_indicators() -> None:
    extraction = _corpus_extraction()

    published = {value for _, value in _canonical_keys(collect_indicators(extraction))}
    assert {f"uae{index}.locat.sbs" for index in range(1, 15)} <= published
    assert "locat.sbs" in published


def test_addresses_of_one_network_stay_distinct_and_are_never_aggregated() -> None:
    extraction = _corpus_extraction()

    published = {value for _, value in _canonical_keys(collect_indicators(extraction))}
    same_network = {f"185.199.108.{index}" for index in range(1, 11)}
    assert same_network <= published
    assert not any("/" in value for value in published)


def test_hash_algorithms_do_not_collide_through_normalization() -> None:
    hashes = ("a" * 32, "b" * 40, "c" * 64, "d" * 128)
    source = "Hashes\n" + "\n".join(hashes)
    response = "\n\n".join(
        (
            "IOC confirmed md5\n- " + hashes[0],
            "IOC confirmed sha1\n- " + hashes[1],
            "IOC confirmed sha256\n- " + hashes[2],
            "IOC confirmed sha512\n- " + hashes[3],
        )
    )

    gated = verify_ioc_rules_output_against_source(_parse(response), source)
    assert gated.rejections == ()
    verification = verify_q2_proposals(
        (Q2ProposalSubmission(output=gated.output, source_ids=("S1",)),)
    )

    indicators = collect_indicators(verification.canonical)
    assert {item.value for item in indicators} == set(hashes)
    assert len({item.normalized_value for item in indicators}) == 4


def test_source_evidence_gate_binds_a_value_to_the_source_that_published_it() -> None:
    borrowed = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value=S3_DOMAINS[0], artifact_type="domain", indicator_status="confirmed_ioc"
            )
        ]
    )

    wrong_source = verify_q2_output_against_source(
        borrowed, _source_text("ioc_coverage_s1_explicit_table.md")
    )
    right_source = verify_q2_output_against_source(
        borrowed, _source_text("ioc_coverage_s3_other_actor.md")
    )

    assert wrong_source.output.artifacts == []
    assert [rejection.reason_code for rejection in wrong_source.rejections] == [
        "source_evidence_missing"
    ]
    assert wrong_source.rejections[0].value == S3_DOMAINS[0]
    assert wrong_source.rejections[0].artifact_type == "domain"
    assert wrong_source.rejections[0].proposal_kind == "artifact"
    assert right_source.rejections == ()
    assert [artifact.value for artifact in right_source.output.artifacts] == [S3_DOMAINS[0]]


def test_an_ioc_split_by_inline_markup_is_still_proven_by_its_source() -> None:
    html = (
        "<html><body><table>"
        "<tr><td>Domain</td><td>Hash</td></tr>"
        "<tr><td><span>uae1.</span><b>locat.sbs</b></td>"
        "<td>5d41402abc4b2a76<wbr>b9719d911017c592</td></tr>"
        "<tr><td>cloud.tiktok-u.sbs</td><td>node-01.security-lab.io</td></tr>"
        "</table></body></html>"
    )
    document = source_evidence_document_from_html("", html)
    output = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="uae1.locat.sbs", artifact_type="domain", indicator_status="confirmed_ioc"
            ),
            Q2ArtifactProposal(
                value="5d41402abc4b2a76b9719d911017c592",
                artifact_type="hash",
                indicator_status="confirmed_ioc",
            ),
        ]
    )

    gated = verify_ioc_rules_output_against_source(output, document)

    assert gated.rejections == ()
    assert [artifact.value for artifact in gated.output.artifacts] == [
        "uae1.locat.sbs",
        "5d41402abc4b2a76b9719d911017c592",
    ]


def test_values_separated_by_real_whitespace_are_never_glued_into_one_token() -> None:
    document = source_evidence_document_from_html(
        "",
        "<html><body><td><span>uae1.locat.sbs</span>"
        " <span>uae2.locat.sbs</span></td></body></html>",
    )
    output = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="uae1.locat.sbsuae2.locat.sbs",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            )
        ]
    )

    gated = verify_ioc_rules_output_against_source(output, document)

    assert [rejection.reason_code for rejection in gated.rejections] == ["source_evidence_missing"]


def test_line_wrapping_characters_do_not_hide_a_published_indicator() -> None:
    source = "IOC table\nuae1.lo­cat.sbs\nnode​-01.security-lab.io\n"
    output = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="uae1.locat.sbs", artifact_type="domain", indicator_status="confirmed_ioc"
            ),
            Q2ArtifactProposal(
                value="node​-01.security-lab.io",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
        ]
    )

    gated = verify_ioc_rules_output_against_source(output, source)
    assert gated.rejections == ()
    verification = verify_q2_proposals(
        (Q2ProposalSubmission(output=gated.output, source_ids=("S1",)),)
    )

    assert verification.rejected == ()
    assert {item.normalized_value for item in collect_indicators(verification.canonical)} == {
        "uae1.locat.sbs",
        "node-01.security-lab.io",
    }


def test_markdown_decorated_values_stay_publishable_indicators() -> None:
    response = (
        "IOC confirmed ip\n- `185.199.108.1`\n- **185.199.108.2**\n\n"
        "IOC confirmed domain\n- `uae1.locat.sbs`\n- ***uae2.locat.sbs***"
    )

    parsed = _parse(response)
    gated = verify_ioc_rules_output_against_source(
        parsed, _source_text("ioc_coverage_s1_explicit_table.md")
    )
    assert gated.rejections == ()
    verification = verify_q2_proposals(
        (Q2ProposalSubmission(output=gated.output, source_ids=("S1",)),)
    )

    assert verification.rejected == ()
    assert {item.value for item in collect_indicators(verification.canonical)} == {
        "185.199.108.1",
        "185.199.108.2",
        "uae1.locat.sbs",
        "uae2.locat.sbs",
    }


def test_a_stand_in_word_used_as_a_label_is_not_treated_as_a_redaction() -> None:
    values = (
        "na.locat.sbs",
        "none.locat.sbs",
        "unknown-host.locat.sbs",
        "null-router.locat.sbs",
    )
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    artifacts=[
                        Q2ArtifactProposal(
                            value=value, artifact_type="domain", indicator_status="confirmed_ioc"
                        )
                        for value in values
                    ]
                ),
                source_ids=("S1",),
            ),
        )
    )

    assert verification.rejected == ()
    assert {item.value for item in collect_indicators(verification.canonical)} == set(values)


@pytest.mark.parametrize(
    "value",
    (
        "notexample.com",
        "fooexample.net",
        "myexample.org",
        "redacted-service.com",
        "fuzz.io",
        "unknown-host.locat.sbs",
        "none.locat.sbs",
    ),
)
def test_placeholder_like_labels_reach_confirmed_ioc_collection(value: str) -> None:
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    artifacts=[
                        Q2ArtifactProposal(
                            value=value,
                            artifact_type="domain",
                            indicator_status="confirmed_ioc",
                        )
                    ]
                ),
                source_ids=("S1",),
            ),
        )
    )

    indicators = collect_indicators(verification.canonical)
    assert verification.rejected == ()
    assert len(indicators) == 1
    assert indicators[0].value == value
    assert indicators[0].indicator_status is IndicatorStatus.CONFIRMED_IOC


@pytest.mark.parametrize(
    ("value", "artifact_type"),
    (
        ("example.com", "domain"),
        ("foo.example.com", "domain"),
        ("example.org", "domain"),
        ("https://example.com/path", "url"),
        ("https://sub.example.net/a", "url"),
        ("user@example.com", "email"),
        ("unknown", "domain"),
        ("N/A", "domain"),
        ("none", "domain"),
        ("null", "domain"),
        ("redacted", "domain"),
        ("<redacted>", "domain"),
        ("FUZZ", "domain"),
    ),
)
def test_explicit_placeholder_values_are_rejected_type_aware(
    value: str, artifact_type: str
) -> None:
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    artifacts=[
                        Q2ArtifactProposal(
                            value=value,
                            artifact_type=artifact_type,
                            indicator_status="confirmed_ioc",
                        )
                    ]
                ),
                source_ids=("S1",),
            ),
        )
    )

    assert collect_indicators(verification.canonical) == []
    assert [item.reason_code for item in verification.rejected] == ["redacted_placeholder"]


def test_a_bare_stand_in_word_is_still_rejected_as_a_placeholder() -> None:
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    artifacts=[
                        Q2ArtifactProposal(
                            value=value, artifact_type="domain", indicator_status="confirmed_ioc"
                        )
                        for value in ("unknown", "N/A", "<redacted>", "example.com")
                    ]
                ),
                source_ids=("S1",),
            ),
        )
    )

    assert [item.reason_code for item in verification.rejected] == ["redacted_placeholder"] * 4
    assert collect_indicators(verification.canonical) == []


def test_confirmed_and_contextual_proposals_merge_into_one_confirmed_indicator() -> None:
    value = S1_DOMAINS[0]
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    artifacts=[
                        Q2ArtifactProposal(
                            value=value, artifact_type="domain", indicator_status="confirmed_ioc"
                        )
                    ]
                ),
                source_ids=("S1",),
            ),
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    artifacts=[
                        Q2ArtifactProposal(
                            value=value, artifact_type="domain", indicator_status="contextual"
                        )
                    ]
                ),
                source_ids=("S2",),
            ),
        )
    )

    indicators = collect_indicators(verification.canonical)
    assert len(indicators) == 1
    assert indicators[0].indicator_status is IndicatorStatus.CONFIRMED_IOC
    assert indicators[0].source_ids == ("S1", "S2")
    assert "semantic_status_conflict" in verification.warnings
    conflict = verification.semantic_status_conflicts[0]
    assert conflict.statuses == ("confirmed_ioc", "contextual")
    assert conflict.source_ids == ("S1", "S2")


def test_two_confirmed_sources_merge_provenance_without_duplicating_the_indicator() -> None:
    value = S1_DOMAINS[0]
    artifacts = [
        Q2ArtifactProposal(value=value, artifact_type="domain", indicator_status="confirmed_ioc")
    ]
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(output=Q2SourceOutput(artifacts=artifacts), source_ids=("S1",)),
            Q2ProposalSubmission(output=Q2SourceOutput(artifacts=artifacts), source_ids=("S4",)),
        )
    )

    indicators = collect_indicators(verification.canonical)
    assert len(indicators) == 1
    assert indicators[0].source_ids == ("S1", "S4")
    assert verification.semantic_status_conflicts == ()
    assert "semantic_status_conflict" not in verification.warnings
