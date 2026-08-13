from datetime import date
from pathlib import Path

import pytest

from cti_app.application.discovery_report_parser import (
    ReportParsingError,
    extract_http_urls,
    parse_discovery_report,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import IocPresence, PeriodRelation, SourceRole


def parse(report: str, citations: object = None):
    return parse_discovery_report(
        report,
        visible_citations=[] if citations is None else citations,
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )


def test_current_format_is_tolerant_partial_and_preserves_urls() -> None:
    result = parse(
        """# SUJETS CANDIDATS

## subject S1
Technical_Potential_Reason: Chaîne documentée.
TITLE: Campagne exemple
presentation: Première phrase.
  Deuxième phrase multiligne.
actor_or_campaign: ExampleActor
technical_potential: 3
artifacts: IOC, configurations, yara
uncertainties: attribution provisoire; date à vérifier
extra-field: conservé dans le bloc

### publication P1
publisher: Vendor
title: Rapport principal
published_at: unknown
period_relation: in_period
source_role: primary
ioc_presence: declared
ioc_declared_count: 42
ioc_visible_count: unknown
url: [rapport](https://Vendor.Example/report/?utm_source=chatgpt)

### PUBLICATION P2
title: Corroboration
url: <https://independent.example/a> et https://independent.example/a?utm_medium=x
publisher: Lab
published_at: 2026-05-12
period_relation: in_period
source_role: independent
ioc_presence: visible
ioc_declared_count: unknown
ioc_visible_count: 3

## SUBJECT S2
title: Sujet incomplet
presentation: Une URL ambiguë n'est pas corrigée.
actor_or_campaign: unknown
technical_potential: 1
technical_potential_reason: Présentation générale.
artifacts: unknown
uncertainties: URL manquante

### PUBLICATION P3
title: Publication sans URL
url: ftp://invalid.example/report
publisher: unknown
published_at: unknown
period_relation: unknown
source_role: unknown
ioc_presence: unknown
ioc_declared_count: unknown
ioc_visible_count: unknown
"""
    )

    assert result.status == "partial"
    assert len(result.candidates) == 2
    first, second = result.candidates
    assert first.summary == "Première phrase.\nDeuxième phrase multiligne."
    assert first.technical_potential == 3
    assert first.likely_artifacts == ("ioc", "configurations", "yara")
    assert len(first.sources) == 2
    assert first.sources[0].canonical_url == "https://vendor.example/report"
    assert first.sources[0].published_at is None
    assert first.sources[0].role is SourceRole.PRIMARY
    assert first.sources[0].ioc_presence is IocPresence.DECLARED
    assert first.sources[0].ioc_declared_count == 42
    assert first.sources[1].period_relation is PeriodRelation.IN_PERIOD
    assert second.selectable is False
    assert len(second.incomplete_sources) == 1
    assert second.incomplete_sources[0].raw_url == "ftp://invalid.example/report"
    assert any("champ non reconnu" in warning for warning in result.warnings)


def test_url_forms_tracking_invalid_and_duplicates() -> None:
    assert extract_http_urls(
        "[a](https://a.example/x?utm_source=z) <https://a.example/x> "
        "https://b.example/y?gclid=1 file:///etc/passwd"
    ) == [
        ("https://a.example/x?utm_source=z", "https://a.example/x"),
        ("https://b.example/y?gclid=1", "https://b.example/y"),
    ]


def test_legacy_iran_report_keeps_context_without_selecting_it() -> None:
    report = Path("backend/tests/fixtures/iran_archived_discovery.md").read_text()
    result = parse(report)

    assert [candidate.title for candidate in result.candidates[:2]] == [
        "CYFIRMA — APT Quarterly Report: Apr to Jun 2026",
        "NCC Group — Monthly Threat Pulse – Review of May 2026",  # noqa: RUF001
    ]
    assert all(candidate.selectable for candidate in result.candidates[:2])
    context = result.candidates[2:]
    assert context
    assert all(candidate.context_only and not candidate.selectable for candidate in context)
    urls = [source.canonical_url for candidate in result.candidates for source in candidate.sources]
    assert urls.count("https://example.org/archive/iranian-campaign") == 1


def test_visible_citations_not_in_blocks_are_preserved_as_context() -> None:
    result = parse(
        """## SUBJECT S1
title: Sujet
presentation: Présentation.
technical_potential: 2
### PUBLICATION P1
title: Source
url: https://one.example/report
""",
        [{"label": "Deux", "url": "https://two.example/report?utm_source=x"}],
    )
    recovered = result.candidates[-1]
    assert recovered.context_only is True
    assert recovered.sources[0].canonical_url == "https://two.example/report"


def test_contract_echo_is_never_a_valid_discovery() -> None:
    with pytest.raises(ReportParsingError, match="contrat") as caught:
        parse(
            '{"minimal_example":{"citations":[],"queries":[],"topics":[]},'
            '"version":"research-batch-compact-v1"}'
        )
    assert caught.value.code == "report_parsing_failed"
