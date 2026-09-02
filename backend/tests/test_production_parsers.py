"""Tolerance tests for the production Markdown parsers.

The model will not answer in exactly the requested shape. What matters is that
a recoverable deviation costs a warning, an unreadable block is dropped alone,
and only a genuinely empty result is unusable.
"""

from __future__ import annotations

import pathlib
from datetime import date

import pytest

from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    ParsedEvent,
    ParsedSource,
    ReferenceReport,
    SemanticType,
    TechnicalExtraction,
    exact_artifact_value_allowed_in_body,
    parse_reference_report,
    validate_synthesis,
)
from cti_app.application.production_prompts import ProductionPromptTemplates
from cti_app.domain.discovery import SourceRole
from cti_app.domain.publication import ArtifactType

RESEARCH_DATE = date(2026, 8, 1)

PERFECT_Q1 = """# REFERENCES

## SOURCE S1

title: Rapport ExampleRAT
url: https://research.example/rapport
publisher: Example Labs
published-at: 2026-07-01
role: primary

## SOURCE S2

title: Analyse complementaire
url: https://other.example/analyse
publisher: Other
published-at: 2026-07-05
role: independent

## EVENT R1

date: 2026-07-01
sources: S1, S2
text: Premiere observation de la campagne.

# UNCERTAINTIES
- Attribution non confirmee
"""

# Sentinel consumed by _FakeConversations in test_production_workflow_stages:
# it swaps this marker for a real Q2 Markdown answer built from whichever
# archived literal appears in that specific source's prompt.
PERFECT_Q2 = "__PERFECT_Q2_SENTINEL__"


def _report() -> ReferenceReport:
    result = parse_reference_report(PERFECT_Q1, RESEARCH_DATE)
    assert result.usable
    assert result.value is not None
    return result.value


def test_perfect_report_is_parsed() -> None:
    report = _report()

    assert [s.local_id for s in report.sources] == ["S1", "S2"]
    assert report.sources[0].title == "Rapport ExampleRAT"
    assert report.sources[0].role is SourceRole.PRIMARY
    assert report.sources[0].published_at == date(2026, 7, 1)
    assert report.events[0].source_ids == ("S1", "S2")
    assert report.uncertainties == ("Attribution non confirmee",)


def test_reference_prompt_keeps_ids_as_compact_transport_aliases() -> None:
    prompt = ProductionPromptTemplates.REFERENCES_RESEARCH_V2

    assert "## SOURCE S1" in prompt
    assert "## EVENT R1" in prompt
    assert "sources: S1, S2" in prompt
    assert "compact transport aliases only" in prompt
    assert "not canonical identifiers" in prompt


def test_outer_markdown_fence_is_stripped() -> None:
    result = parse_reference_report(f"```markdown\n{PERFECT_Q1}\n```", RESEARCH_DATE)

    assert result.usable


def test_crlf_and_non_breaking_spaces_are_tolerated() -> None:
    mangled = PERFECT_Q1.replace("\n", "\r\n").replace("title:", "title:\u00a0")

    result = parse_reference_report(mangled, RESEARCH_DATE)

    assert result.usable


@pytest.mark.parametrize(
    "variant",
    [
        PERFECT_Q1.replace("## SOURCE", "## publication").replace("## EVENT", "## Événement"),
        PERFECT_Q1.replace(":", " =", 0).replace("url:", "url ="),
        PERFECT_Q1.replace("published-at:", "published_at:"),
        PERFECT_Q1.upper().replace("HTTPS://", "https://"),
    ],
)
def test_heading_and_field_variants_are_tolerated(variant: str) -> None:
    result = parse_reference_report(variant, RESEARCH_DATE)

    assert result.usable, result.errors


def test_multiline_field_is_joined() -> None:
    text = PERFECT_Q1.replace(
        "text: Premiere observation de la campagne.",
        "text: Premiere observation\n  de la campagne sur plusieurs lignes.",
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable
    assert result.value is not None
    assert "plusieurs lignes" in result.value.events[0].text


def test_missing_ids_are_generated() -> None:
    text = PERFECT_Q1.replace("## SOURCE S1", "## SOURCE").replace("## EVENT R1", "## EVENT")

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable
    assert result.value is not None
    assert [source.local_id for source in result.value.sources] == ["S1", "S2"]
    assert result.value.events[0].local_id == "R1"
    assert "source_id_generated" in result.warnings
    assert "event_id_generated" in result.warnings


@pytest.mark.parametrize("alias", ("S1", "S-1", "S_1", "S 1"))
def test_source_alias_variants_are_normalized_in_headings_and_events(alias: str) -> None:
    text = PERFECT_Q1.replace("## SOURCE S1", f"## SOURCE {alias}").replace(
        "sources: S1, S2", f"sources: {alias}, S2"
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable, result.errors
    assert result.value is not None
    assert [source.local_id for source in result.value.sources] == ["S1", "S2"]
    assert result.value.events[0].source_ids == ("S1", "S2")


def test_duplicate_url_is_merged_and_events_remapped() -> None:
    """The same publication announced twice must not become two sources."""
    text = PERFECT_Q1.replace(
        "url: https://other.example/analyse",
        "url: https://research.example/rapport?utm_source=x",
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable
    assert result.value is not None
    assert len(result.value.sources) == 1
    assert "duplicate_source_merged" in result.warnings
    # The event cited S1 and S2; both now resolve to the single surviving source.
    assert result.value.events[0].source_ids == ("S1",)


def test_duplicate_url_with_normalized_aliases_maps_to_one_canonical_source() -> None:
    text = (
        PERFECT_Q1.replace("## SOURCE S2", "## SOURCE S-1")
        .replace(
            "url: https://other.example/analyse",
            "url: https://research.example/rapport?utm_source=x",
        )
        .replace("sources: S1, S2", "sources: S1, S_1")
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable, result.errors
    assert result.value is not None
    assert [source.local_id for source in result.value.sources] == ["S1"]
    assert result.value.events[0].source_ids == ("S1",)


def test_canonical_source_ids_are_continuous_after_url_deduplication() -> None:
    third_source = """## SOURCE S42

title: Troisieme publication
url: https://third.example/article
publisher: Third
published-at: 2026-07-06
role: relay

"""
    text = (
        PERFECT_Q1.replace("## SOURCE S1", "## SOURCE S9")
        .replace(
            "url: https://other.example/analyse",
            "url: https://research.example/rapport?utm_medium=x",
        )
        .replace("## EVENT R1", third_source + "## EVENT R77")
        .replace("sources: S1, S2", "sources: S9, S2, S42")
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable, result.errors
    assert result.value is not None
    assert [source.local_id for source in result.value.sources] == ["S1", "S2"]
    assert result.value.events[0].source_ids == ("S1", "S2")


def test_same_model_alias_for_different_urls_drops_citing_event() -> None:
    text = PERFECT_Q1.replace("## SOURCE S2", "## SOURCE S-1").replace(
        "sources: S1, S2", "sources: S_1"
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert not result.usable
    assert "no_usable_event" in result.errors
    assert "event_ambiguous_source_alias_dropped" in result.warnings


def test_canonical_event_ids_are_continuous_after_dropped_events() -> None:
    extra_events = """## EVENT R99

date: 2027-01-01
sources: S1
text: Événement futur.

## EVENT R42

date: 2026-07-02
sources: S1
text: Deuxième observation.
"""
    text = PERFECT_Q1.replace("## EVENT R1", extra_events + "## EVENT R7")

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable, result.errors
    assert result.value is not None
    assert [event.local_id for event in result.value.events] == ["R1", "R2"]
    assert [event.text for event in result.value.events] == [
        "Deuxième observation.",
        "Premiere observation de la campagne.",
    ]


def test_url_is_recovered_from_free_text() -> None:
    text = PERFECT_Q1.replace(
        "url: https://research.example/rapport",
        "Disponible ici https://research.example/rapport",
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable
    assert "source_url_recovered_from_text" in result.warnings


def test_event_citing_only_unknown_sources_is_dropped() -> None:
    text = PERFECT_Q1.replace("sources: S1, S2", "sources: S9")

    result = parse_reference_report(text, RESEARCH_DATE)

    assert not result.usable
    assert "no_usable_event" in result.errors
    assert "event_without_known_source_dropped" in result.warnings


def test_event_keeps_its_known_sources_when_one_is_unknown() -> None:
    text = PERFECT_Q1.replace("sources: S1, S2", "sources: S1, S9")

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable
    assert result.value is not None
    assert result.value.events[0].source_ids == ("S1",)
    assert "event_unknown_source_removed" in result.warnings


def test_future_event_date_is_rejected() -> None:
    text = PERFECT_Q1.replace("date: 2026-07-01", "date: 2027-01-01")

    result = parse_reference_report(text, RESEARCH_DATE)

    assert not result.usable
    assert "event_with_future_date_dropped" in result.warnings


def test_one_broken_block_does_not_sink_the_others() -> None:
    text = PERFECT_Q1.replace(
        "## EVENT R1\n\ndate: 2026-07-01\nsources: S1, S2\n"
        "text: Premiere observation de la campagne.",
        "## EVENT R1\n\ndate: 2026-07-01\nsources: S1\ntext: Premiere observation.\n\n"
        "## EVENT R2\n\nsources: S2\n",
    )

    result = parse_reference_report(text, RESEARCH_DATE)

    assert result.usable
    assert result.value is not None
    assert [e.local_id for e in result.value.events] == ["R1"]
    assert result.dropped_blocks


def test_totally_unusable_answer_reports_errors() -> None:
    result = parse_reference_report("Je ne peux pas répondre à cette demande.", RESEARCH_DATE)

    assert not result.usable
    assert result.errors


def test_empty_answer_is_unusable() -> None:
    result = parse_reference_report("   \n  ", RESEARCH_DATE)

    assert not result.usable
    assert "empty_response" in result.errors


# --- Synthesis validation --------------------------------------------------


def _corpus() -> ReferenceReport:
    return ReferenceReport(
        sources=(
            ParsedSource(
                local_id="S1",
                title="t",
                url="https://research.example/rapport",
                canonical_url="https://research.example/rapport",
                publisher=None,
                published_at=None,
                role=SourceRole.PRIMARY,
            ),
        ),
        events=(ParsedEvent(local_id="R1", event_date=None, source_ids=("S1",), text="x"),),
    )


def test_valid_synthesis_passes() -> None:
    text = "Le groupe agit depuis 2020 [S1]. Le CVE-2026-1234 est exploité [S1]."

    result = validate_synthesis(text, _corpus(), {"CVE-2026-1234"})

    assert result.usable


def test_unknown_source_marker_is_rejected() -> None:
    result = validate_synthesis("Analyse [S9].", _corpus(), set())

    assert not result.usable
    assert any("unknown_source_marker" in e for e in result.errors)


def test_factual_paragraph_without_source_marker_is_rejected() -> None:
    result = validate_synthesis("Analyse factuelle sans citation.", _corpus(), set())

    assert not result.usable
    assert "uncited_factual_paragraph" in result.errors


def test_url_outside_corpus_is_rejected() -> None:
    result = validate_synthesis("Voir https://elsewhere.example/page [S1].", _corpus(), set())

    assert not result.usable
    assert any("raw_url" in e for e in result.errors)


@pytest.mark.parametrize("label", ("EXCLUDED", "hidden"))
def test_internal_publication_labels_are_rejected(label: str) -> None:
    result = validate_synthesis(f"Élément {label} [S1].", _corpus(), set())

    assert not result.usable
    assert "internal_display_label" in result.errors


def test_indicator_absent_from_corpus_is_rejected() -> None:
    result = validate_synthesis("Le hash " + "a" * 64 + " circule [S1].", _corpus(), set())

    assert not result.usable
    assert any("unknown_indicator" in e for e in result.errors)


def test_version_numbers_are_not_mistaken_for_indicators() -> None:
    """A naive IPv4 rule would bounce this perfectly good synthesis."""
    result = validate_synthesis(
        "La version 4.2.1.3 du greffon est affectée [S1].", _corpus(), set()
    )

    assert result.usable


def test_empty_synthesis_is_rejected() -> None:
    result = validate_synthesis("   ", _corpus(), set())

    assert not result.usable
    assert "empty_synthesis" in result.errors


# --- Real ChatGPT output ---------------------------------------------------

REAL_ANSWER = (
    pathlib.Path(__file__).parent / "fixtures" / "real_chatgpt_references.md"
).read_text()


def test_real_chatgpt_answer_is_parsed() -> None:
    """Regression guard against a genuine answer, quirks included.

    A real answer writes URLs as `[https://x](https://x)`, appends citation
    markers after values, and carries fields the report does not use. A naive
    URL regex dropped every source here.
    """
    result = parse_reference_report(REAL_ANSWER, date(2026, 8, 1))

    assert result.usable, result.errors
    assert result.value is not None
    sources = result.value.sources
    assert len(sources) == 3

    first = sources[0]
    assert first.canonical_url == (
        "https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat"
    )
    assert first.publisher == "Recorded Future / Insikt Group"
    assert first.published_at == date(2026, 7, 1)
    assert first.role is SourceRole.PRIMARY
    assert len(result.value.events[0].source_ids) == 3


def test_explicit_unknown_date_is_not_reported_as_a_parse_failure() -> None:
    """`published-at: unknown` is the model answering, not a broken format."""
    text = REAL_ANSWER.replace("published-at: 2026-07-01", "published-at: unknown", 1)

    result = parse_reference_report(text, date(2026, 8, 1))

    assert result.usable
    assert result.value is not None
    assert result.value.sources[0].published_at is None
    assert "source_date_unreadable" not in result.warnings


BRIDGE_ANSWER = (
    pathlib.Path(__file__).parent / "fixtures" / "real_bridge_references.md"
).read_text()


def test_answer_delivered_by_the_bridge_is_parsed() -> None:
    """The bridge serialises ChatGPT's rendered DOM, so `#` markers are gone.

    This is a verbatim Q1 answer as it reached the application. Requiring `#`
    made the parser find no block at all and report `no_source_or_event_block`
    on a perfectly well-formed answer.
    """
    result = parse_reference_report(BRIDGE_ANSWER, date(2026, 8, 18))

    assert result.usable, result.errors
    assert result.value is not None
    report = result.value

    assert [source.local_id for source in report.sources] == ["S1", "S2", "S3"]
    assert report.sources[0].role is SourceRole.PRIMARY
    assert report.sources[1].role is SourceRole.RELAY
    # The tracking parameter the model appends is canonicalised away.
    assert report.sources[0].canonical_url == (
        "https://www.recordedfuture.com/research/nexus-tag182-disseminates-markirat"
    )

    assert len(report.events) == 6
    assert report.events[0].event_date == date(2026, 3, 7)
    assert report.events[4].source_ids == ("S1", "S2")
    assert len(report.uncertainties) == 4

    # Nothing was recovered or thrown away: the answer was simply readable.
    assert result.warnings == []
    assert result.dropped_blocks == []


def test_prose_is_never_mistaken_for_a_bare_heading() -> None:
    """Dropping the `#` requirement must not turn continuation lines into blocks."""
    # Anchored on ASCII only: the fixture uses precomposed accents.
    text = BRIDGE_ANSWER.replace(
        "\ntext: ",
        "\ntext: Une phrase courte\nqui continue ici\n",
        1,
    )

    result = parse_reference_report(text, date(2026, 8, 18))

    assert result.usable, result.errors
    assert result.value is not None
    assert len(result.value.events) == 6
    joined = " ".join(event.text for event in result.value.events)
    assert "qui continue ici" in joined


# --- Body-only artifacts vs network IOC section (Dust-Specter regression) ---


def _artifact(
    local_id: str,
    value: str,
    artifact_type: ArtifactType,
    *,
    status: IndicatorStatus = IndicatorStatus.CONFIRMED_IOC,
    policy: DisplayPolicy = DisplayPolicy.BODY_ONLY,
    category: str = "files",
    context: str = "chaîne d'exécution",
) -> ExtractionItem:
    return ExtractionItem(
        local_id=local_id,
        category=category,
        value=value,
        context=context,
        artifact_type=artifact_type,
        attack_id=None,
        reference_ids=(),
        source_ids=("S1",),
        supported=True,
        semantic_type=(
            SemanticType.FILE
            if artifact_type in {ArtifactType.FILENAME, ArtifactType.FILEPATH}
            else SemanticType.INDICATOR
        ),
        indicator_status=status,
        display_policy=policy,
        normalized_value=normalize_indicator_value(value, artifact_type),
    )


def _dust_specter_extraction() -> TechnicalExtraction:
    return TechnicalExtraction(
        items=(
            _artifact("F1", "libvlc.dll", ArtifactType.FILENAME),
            _artifact("F2", "in.txt", ArtifactType.FILENAME, status=IndicatorStatus.CONTEXTUAL),
            _artifact("F3", "hostfxr.dll", ArtifactType.FILENAME),
            _artifact(
                "D1",
                "evil.example",
                ArtifactType.DOMAIN,
                policy=DisplayPolicy.IOC_SECTION,
                category="network_artifacts",
                context="C2",
            ),
        )
    )


DUST_SPECTER_SYNTHESIS = (
    "VLC.exe charge latéralement libvlc.dll ; TWINTASK surveille in.txt, "
    "puis WingetUI.exe charge hostfxr.dll [S1]."
)


def test_body_only_file_artifacts_are_not_ioc_repetition() -> None:
    """A confirmed filename kept out of the IOC section is behavioral detail."""
    result = validate_synthesis(
        DUST_SPECTER_SYNTHESIS, _corpus(), _dust_specter_extraction()
    )

    assert result.usable, result.errors


def test_ioc_section_domain_stays_forbidden_in_body() -> None:
    result = validate_synthesis(
        f"{DUST_SPECTER_SYNTHESIS[:-1]} et contacte evil.example [S1].",
        _corpus(),
        _dust_specter_extraction(),
    )

    assert not result.usable
    assert "ioc_repeated_in_body" in result.errors
    assert any(
        violation.code == "ioc_repeated_in_body" and "evil.example" in violation.detail
        for violation in result.violations
    )


def test_body_only_filepath_and_cve_values_are_allowed() -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact("P1", "C:\\Users\\Public\\twintask\\in.txt", ArtifactType.FILEPATH),
            _artifact("C1", "CVE-2026-1234", ArtifactType.CVE, category="cves"),
        )
    )

    result = validate_synthesis(
        "Le chargeur écrit C:\\Users\\Public\\twintask\\in.txt et exploite "
        "CVE-2026-1234 [S1].",
        _corpus(),
        extraction,
    )

    assert result.usable, result.errors


@pytest.mark.parametrize(
    ("artifact_type", "value", "written"),
    (
        (ArtifactType.DOMAIN, "evil.example", "evil[.]example"),
        (ArtifactType.IP, "203.0.113.9", "203[.]0[.]113[.]9"),
        (ArtifactType.URL, "hxxps://evil.example/gate", "hxxps://evil[.]example/gate"),
        (ArtifactType.HASH, "b" * 64, "b" * 64),
        (ArtifactType.EMAIL, "operator@evil.example", "operator(at)evil[.]example"),
    ),
)
def test_network_ioc_section_values_stay_rejected(
    artifact_type: ArtifactType, value: str, written: str
) -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact(
                "N1",
                value,
                artifact_type,
                policy=DisplayPolicy.IOC_SECTION,
                category="network_artifacts",
            ),
        )
    )

    result = validate_synthesis(f"L'implant contacte {written} [S1].", _corpus(), extraction)

    assert not result.usable
    assert "ioc_repeated_in_body" in result.errors


def test_hidden_artifact_value_stays_rejected() -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact(
                "H1",
                "hidden.example",
                ArtifactType.DOMAIN,
                status=IndicatorStatus.EXCLUDED,
                policy=DisplayPolicy.HIDDEN,
                category="network_artifacts",
            ),
            _artifact(
                "H2",
                "secret.dll",
                ArtifactType.FILENAME,
                status=IndicatorStatus.EXCLUDED,
                policy=DisplayPolicy.HIDDEN,
            ),
        )
    )

    for value in ("hidden.example", "secret.dll"):
        result = validate_synthesis(f"Le chargeur utilise {value} [S1].", _corpus(), extraction)
        assert not result.usable
        assert "ioc_repeated_in_body" in result.errors


def test_both_policy_domain_remains_publishable() -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact(
                "B1",
                "public.example",
                ArtifactType.DOMAIN,
                policy=DisplayPolicy.BOTH,
                category="network_artifacts",
            ),
        )
    )

    result = validate_synthesis(
        "Le C2 public.example reste actif [S1].", _corpus(), extraction
    )

    assert result.usable, result.errors


def test_exact_value_permission_separates_status_from_destination() -> None:
    confirmed_filename = _artifact("F1", "libvlc.dll", ArtifactType.FILENAME)
    confirmed_domain = _artifact(
        "D1",
        "evil.example",
        ArtifactType.DOMAIN,
        policy=DisplayPolicy.IOC_SECTION,
        category="network_artifacts",
    )

    assert exact_artifact_value_allowed_in_body(confirmed_filename)
    assert not exact_artifact_value_allowed_in_body(confirmed_domain)
