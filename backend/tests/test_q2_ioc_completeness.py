from __future__ import annotations

import hashlib
from pathlib import Path

from cti_app.application.production_artifact_verification import (
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_parsers import (
    IndicatorStatus,
    Q2ArtifactProposal,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
)
from cti_app.application.production_rendering import collect_indicators
from cti_app.application.production_source_evidence import verify_q2_output_against_source

FIXTURES = Path(__file__).parent / "fixtures"


def _parse(text: str) -> Q2SourceOutput:
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    return result.value


def _artifact(
    value: str, artifact_type: str, *, status: str = "confirmed_ioc"
) -> Q2ArtifactProposal:
    return Q2ArtifactProposal(
        value=value,
        artifact_type=artifact_type,
        indicator_status=status,
    )


def test_group_ib_ioc_section_keeps_published_values_confirmed() -> None:
    source = (FIXTURES / "group_ib_mirage_kitten_iocs.md").read_text(encoding="utf-8")
    output = _parse(
        """IOC confirmed md5
- db58adc4a6c192520ed509b20a928279

IOC confirmed sha256
- 07dd28b748656e9e1a870c538d6df68c

IOC confirmed ip
- 172.86.98.113
- 185.66.68.213
- 185.253.116.81

IOC confirmed domain
- neexportfolio.com
- locat.sbs
- tiktok-u.sbs
- uae1.locat.sbs
- uae14.locat.sbs
- cloud.tiktok-u.sbs

IOC contextual ip
- 203.0.113.50
"""
    )

    gated = verify_q2_output_against_source(output, source)
    assert gated.rejections == ()
    verification = verify_q2_proposals(
        (Q2ProposalSubmission(output=gated.output, source_ids=("S9",)),)
    )

    expected_confirmed = {
        "db58adc4a6c192520ed509b20a928279",
        "07dd28b748656e9e1a870c538d6df68c",
        "172.86.98.113",
        "185.66.68.213",
        "185.253.116.81",
        "neexportfolio.com",
        "locat.sbs",
        "tiktok-u.sbs",
        "uae1.locat.sbs",
        "uae14.locat.sbs",
        "cloud.tiktok-u.sbs",
    }
    items_by_value = {item.value: item for item in verification.canonical.items}

    assert set(items_by_value) == {*expected_confirmed, "203.0.113.50"}
    assert {item.value for item in collect_indicators(verification.canonical)} == expected_confirmed
    assert all(
        items_by_value[value].indicator_status is IndicatorStatus.CONFIRMED_IOC
        for value in expected_confirmed
    )
    assert items_by_value["203.0.113.50"].indicator_status is IndicatorStatus.CONTEXTUAL


def test_exhaustive_ioc_list_crosses_parser_source_gate_verifier_and_renderer() -> None:
    ips = tuple(f"198.51.100.{index}" for index in range(1, 26))
    domains = (
        *(f"uae{index}.locat.sbs" for index in range(1, 15)),
        "cloud.tiktok-u.sbs",
        *(f"domain-{index}.security-lab.io" for index in range(1, 26)),
    )
    assert len(ips) == 25
    assert len(domains) == 40

    response = (
        "IOC confirmed ip\n"
        + "\n".join(f"- {value}" for value in ips)
        + "\n\nIOC confirmed domain\n"
        + "\n".join(f"- {value}" for value in domains)
    )
    source = "## Indicators of Compromise\n" + "\n".join((*ips, *domains))

    parsed = _parse(response)
    gated = verify_q2_output_against_source(parsed, source)
    assert gated.rejections == ()
    verification = verify_q2_proposals(
        (Q2ProposalSubmission(output=gated.output, source_ids=("S9",)),)
    )

    indicators = collect_indicators(verification.canonical)
    assert len(indicators) == 65
    assert {item.value for item in indicators} == {*ips, *domains}


def test_86_confirmed_iocs_survive_verification_and_true_duplicate_deduplication() -> None:
    ips = tuple(f"203.0.113.{index}" for index in range(1, 28))
    domains = tuple(f"domain-{index}.security-lab.io" for index in range(1, 58))
    hashes = ("a" * 32, "b" * 64)
    values = (
        *(_artifact(value, "ip") for value in ips),
        *(_artifact(value, "domain") for value in domains),
        *(_artifact(value, "hash") for value in hashes),
        _artifact(domains[0].upper(), "domain"),
    )

    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(artifacts=list(values)),
                source_ids=("S9",),
            ),
        )
    )

    expected = {*ips, *domains, *hashes}
    assert len(verification.canonical.items) == 86
    assert {item.value.casefold() for item in verification.canonical.items} == {
        value.casefold() for value in expected
    }
    indicators = collect_indicators(verification.canonical)
    assert len(indicators) == 86
    assert {item.value.casefold() for item in indicators} == {
        value.casefold() for value in expected
    }
    assert all(item.indicator_status is IndicatorStatus.CONFIRMED_IOC for item in indicators)


def test_confirmed_status_wins_contextual_status_conflict_without_hiding_diagnostic() -> None:
    value = "same.security-lab.io"
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(artifacts=[_artifact(value, "domain")]),
                source_ids=("S1",),
            ),
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    artifacts=[_artifact(value, "domain", status="contextual")]
                ),
                source_ids=("S2",),
            ),
        )
    )

    assert len(verification.canonical.items) == 1
    item = verification.canonical.items[0]
    assert item.indicator_status is IndicatorStatus.CONFIRMED_IOC
    assert len(collect_indicators(verification.canonical)) == 1
    assert "semantic_status_conflict" in verification.warnings
    assert verification.semantic_status_conflicts[0].artifact_type == "domain"
    assert verification.semantic_status_conflicts[0].value_hash == hashlib.sha256(
        value.encode()
    ).hexdigest()
    assert verification.semantic_status_conflicts[0].statuses == ("confirmed_ioc", "contextual")
    assert verification.semantic_status_conflicts[0].source_ids == ("S1", "S2")


def test_invalid_ioc_diagnostic_keeps_type_index_hash_and_specific_reason() -> None:
    value = "999.999.999.999"
    verification = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(artifacts=[_artifact(value, "ip")]),
                source_ids=("S1",),
            ),
        )
    )

    diagnostic = verification.rejected[0]
    assert diagnostic.artifact_type == "ip"
    assert diagnostic.proposal_index == 1
    assert diagnostic.value_hash == hashlib.sha256(value.encode()).hexdigest()
    assert diagnostic.reason_code == "invalid_ip"
