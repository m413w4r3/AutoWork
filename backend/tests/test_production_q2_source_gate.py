"""The deterministic source-local evidence gate of canonical extraction."""

from __future__ import annotations

from datetime import date

import pytest

from cti_app.application.production_artifact_verification import (
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_extraction import gate_source_output
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2EventProposal,
    Q2FactProposal,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
)
from cti_app.application.production_source_evidence import (
    SourceEvidenceDocument,
    SourceEvidenceResult,
    SourceEvidenceSpan,
    SourceEvidenceSpanKind,
    verify_ioc_rules_output_against_source,
    verify_q2_output_against_source,
)
from cti_app.domain.production import ExtractionProfile


def _gate(text: str, source: str | SourceEvidenceDocument) -> SourceEvidenceResult:
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    return verify_q2_output_against_source(result.value, source)


def test_fact_is_accepted_only_with_local_evidence() -> None:
    source = "The campaign deployed ExampleRAT against healthcare providers."

    accepted = _gate(
        "FACT malware\n"
        "- Famille de logiciel malveillant ExampleRAT :: "
        "The campaign deployed ExampleRAT against healthcare providers.\n",
        source,
    )
    assert [fact.value for fact in accepted.output.facts] == [
        "Famille de logiciel malveillant ExampleRAT"
    ]
    assert accepted.output.facts[0].context == ""
    assert accepted.output.facts[0].evidence_quote == source
    assert accepted.rejections == ()

    invented = _gate("FACT malware\n- GhostRAT\n", source)
    assert invented.output.facts == []
    assert [rejection.proposal_kind for rejection in invented.rejections] == ["fact"]
    assert invented.rejections[0].reason_code == "source_fact_evidence_missing"
    assert invented.rejections[0].proposal_index == 1


def test_french_fact_without_a_literal_quote_is_rejected_with_a_warning() -> None:
    source = "The campaign deployed ExampleRAT against healthcare providers."
    parsed = parse_q2_proposals_markdown(
        "FACT malware\n- La campagne vise des prestataires de santé.\n"
    )
    assert parsed.usable, parsed.errors
    assert parsed.value is not None

    gated, warnings, rejections = gate_source_output(
        parsed.value,
        SourceEvidenceDocument(parsed_text=source),
        profile=ExtractionProfile.FULL,
    )

    assert gated.facts == []
    assert [item.reason_code for item in rejections] == ["source_fact_evidence_missing"]
    assert warnings == ("extraction_proposal_rejected:source_fact_evidence_missing",)


def test_french_event_with_literal_quote_is_verified_against_english_capture() -> None:
    source = "On 2024-03-02 the actor deployed ExampleRAT."
    parsed = parse_q2_proposals_markdown(
        "EVENT 2024-03-02\n"
        "- Les opérateurs déploient ExampleRAT. :: "
        "On 2024-03-02 the actor deployed ExampleRAT.\n"
    )
    assert parsed.usable, parsed.errors
    assert parsed.value is not None

    verified = verify_q2_output_against_source(parsed.value, source)

    assert [event.text for event in verified.output.events] == [
        "Les opérateurs déploient ExampleRAT."
    ]
    assert verified.output.events[0].context == ""
    assert verified.output.events[0].evidence_quote == source
    assert verified.rejections == ()


def test_indicator_is_accepted_only_with_local_evidence() -> None:
    source = "C2 observed at evil.example during the campaign."

    accepted = _gate("IOC confirmed domain\n- evil.example\n", source)
    assert [item.value for item in accepted.output.artifacts] == ["evil.example"]
    assert accepted.output.artifacts[0].context == ""

    absent = _gate("IOC confirmed domain\n- absent.example\n", source)
    assert absent.output.artifacts == []
    assert absent.rejections[0].reason_code == "source_evidence_missing"


def test_dated_event_must_prove_its_date_in_the_same_evidence_area() -> None:
    source = "On 2024-03-02 the actor deployed ExampleRAT.\n"

    accepted = _gate("EVENT 2024-03-02\n- actor deployed ExampleRAT\n", source)
    assert accepted.output.events[0].event_date == date(2024, 3, 2)
    assert accepted.output.events[0].text == "actor deployed ExampleRAT"

    wrong_date = _gate("EVENT 2023-01-01\n- actor deployed ExampleRAT\n", source)
    assert wrong_date.output.events == []
    assert wrong_date.rejections[0].proposal_kind == "event"
    assert wrong_date.rejections[0].reason_code == "source_event_evidence_missing"

    invented = _gate("EVENT 2024-03-02\n- actor deployed GhostRAT\n", source)
    assert invented.output.events == []

    undated = _gate("EVENT\n- actor deployed ExampleRAT\n", source)
    assert undated.output.events[0].event_date is None
    assert undated.rejections == ()


def test_dated_event_is_not_proven_from_another_evidence_area() -> None:
    document = SourceEvidenceDocument(
        parsed_text="2024-03-02\n\nThe actor deployed ExampleRAT much later.",
        spans=(
            SourceEvidenceSpan(SourceEvidenceSpanKind.BODY_TEXT, "2024-03-02"),
            SourceEvidenceSpan(
                SourceEvidenceSpanKind.BODY_TEXT,
                "The actor deployed ExampleRAT much later.",
            ),
        ),
    )

    result = _gate("EVENT 2024-03-02\n- actor deployed ExampleRAT\n", document)

    assert result.output.events == []
    assert result.rejections[0].reason_code == "source_event_evidence_missing"


def test_rule_body_must_be_published_in_the_source() -> None:
    body = "rule Present { condition: true }"
    source = f"Detection guidance:\n{body}\n"

    accepted = _gate(f"RULE yara: Present\n```yara\n{body}\n```\n", source)
    assert [rule.body for rule in accepted.output.rules] == [body]
    assert accepted.output.rules[0].context == ""

    hallucinated = _gate("RULE yara: Ghost\n```yara\nrule Ghost { condition: true }\n```\n", source)
    assert hallucinated.output.rules == []
    assert hallucinated.rejections[0].reason_code == "source_rule_evidence_missing"


def test_rule_merely_linked_is_not_a_published_body() -> None:
    source = "The YARA rule is available at https://github.com/example/rules/blob/main/ghost.yar"

    result = _gate("RULE yara: Ghost\n```yara\nrule Ghost { condition: true }\n```\n", source)

    assert result.output.rules == []


def test_ioc_rules_gate_yields_no_facts_or_events() -> None:
    source = "ExampleRAT hit evil.example on 2024-03-02; the actor deployed ExampleRAT."
    parsed = parse_q2_proposals_markdown(
        "FACT malware\n- ExampleRAT\n"
        "EVENT 2024-03-02\n- the actor deployed ExampleRAT\n"
        "IOC confirmed domain\n- evil.example\n"
    )
    assert parsed.usable, parsed.errors
    assert parsed.value is not None

    result = verify_ioc_rules_output_against_source(parsed.value, source)

    assert result.output.facts == []
    assert result.output.events == []
    assert "fact_not_allowed" in result.warnings
    assert "event_not_allowed" in result.warnings
    assert [item.value for item in result.output.artifacts] == ["evil.example"]


def test_hatching_article_cannot_borrow_triage_iocs() -> None:
    triage_hash = "a" * 64
    hatching = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="evil.example",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
            Q2ArtifactProposal(
                value=triage_hash,
                artifact_type="hash",
                indicator_status="confirmed_ioc",
            ),
        ]
    )
    triage = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="evil.example",
                artifact_type="domain",
                indicator_status="confirmed_ioc",
            ),
            Q2ArtifactProposal(
                value=triage_hash,
                artifact_type="hash",
                indicator_status="confirmed_ioc",
            ),
        ]
    )

    hatching_gate = verify_q2_output_against_source(
        hatching,
        "NightLedger detection added; sample report linked.",
    )
    triage_gate = verify_q2_output_against_source(
        triage,
        f"Triage sample report\nevil.example\nSHA256 {triage_hash}",
    )

    assert hatching_gate.output.artifacts == []
    assert all(
        rejection.reason_code == "source_evidence_missing" for rejection in hatching_gate.rejections
    )
    verification = verify_q2_proposals(
        (Q2ProposalSubmission(output=triage_gate.output, source_ids=("S8",)),)
    )
    assert {item.source_ids for item in verification.canonical.items} == {("S8",)}


def test_every_accepted_proposal_carries_its_local_quote() -> None:
    source = "ExampleRAT beacons to evil.example every minute."

    result = _gate("FACT malware\n- ExampleRAT\nIOC confirmed domain\n- evil.example\n", source)

    assert result.output.facts[0].evidence_quote == source
    assert result.output.artifacts[0].evidence_quote == source


def test_a_paraphrased_fact_is_anchored_by_a_published_quote() -> None:
    source = "The loader persists through a scheduled task named Updater."
    output = Q2SourceOutput(
        facts=[
            Q2FactProposal(
                category="persistence",
                value="Scheduled task persistence",
                evidence_quote="persists through a scheduled task named Updater",
            ),
            Q2FactProposal(
                category="persistence",
                value="Registry run key",
                evidence_quote="persists through a registry run key",
            ),
        ]
    )

    result = verify_q2_output_against_source(output, source)

    assert [fact.value for fact in result.output.facts] == ["Scheduled task persistence"]
    assert result.output.facts[0].evidence_quote == source
    assert [rejection.value for rejection in result.rejections] == ["Registry run key"]


@pytest.mark.parametrize(
    "published",
    [
        "On 15 August 2026 the actor deployed ExampleRAT.",
        "On August 15, 2026 the actor deployed ExampleRAT.",
        "On Aug 15th, 2026 the actor deployed ExampleRAT.",
        "Le 15 août 2026, the actor deployed ExampleRAT.",
        "On 15/08/2026 the actor deployed ExampleRAT.",
    ],
)
def test_a_dated_event_accepts_every_published_date_spelling(published: str) -> None:
    output = Q2SourceOutput(
        events=[Q2EventProposal(event_date=date(2026, 8, 15), text="the actor deployed ExampleRAT")]
    )

    result = verify_q2_output_against_source(output, published)

    assert [event.event_date for event in result.output.events] == [date(2026, 8, 15)]
    assert result.output.events[0].evidence_quote == published


def test_a_dated_event_never_borrows_a_neighbouring_date() -> None:
    output = Q2SourceOutput(
        events=[Q2EventProposal(event_date=date(2026, 8, 16), text="the actor deployed ExampleRAT")]
    )

    result = verify_q2_output_against_source(
        output, "On 15 August 2026 the actor deployed ExampleRAT."
    )

    assert result.output.events == []
    assert result.rejections[0].reason_code == "source_event_evidence_missing"
