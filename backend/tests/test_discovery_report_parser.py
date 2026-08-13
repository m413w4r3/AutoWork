import hashlib
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.discovery_report_parser import (
    ParsedDiscoveryReport,
    ReportParsingError,
    extract_http_urls,
    parse_discovery_report,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import IocPresence, PeriodRelation, SourceRole


def parse(report: str, citations: object = None) -> ParsedDiscoveryReport:
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
    report = (Path(__file__).parent / "fixtures/iran_archived_discovery.md").read_text()
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


def test_visible_citations_not_in_blocks_never_become_a_topic() -> None:
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
    assert len(result.candidates) == 1
    assert result.unattached_visible_citations[0]["canonical_url"] == ("https://two.example/report")


def test_contract_echo_is_never_a_valid_discovery() -> None:
    with pytest.raises(ReportParsingError, match="contrat") as caught:
        parse(
            '{"minimal_example":{"citations":[],"queries":[],"topics":[]},'
            '"version":"research-batch-compact-v1"}'
        )
    assert caught.value.code == "report_parsing_failed"


def test_real_escaped_iran_report_keeps_all_five_subjects_and_metadata() -> None:
    report = (Path(__file__).parent / "fixtures/chatgpt_iran_2026_08_escaped.md").read_text()

    result = parse(report)

    assert hashlib.sha256(report.encode()).hexdigest() == (
        "b7243f1ed8e8bab4e49e08021d73abe4248236d01a65f7c4dd84ab8e4bceebc8"
    )
    assert len(result.candidates) == 5
    assert [candidate.technical_potential for candidate in result.candidates] == [4, 4, 4, 4, 3]
    assert [candidate.actor_or_campaign for candidate in result.candidates] == [
        "Cyber Isnaad Front / Aria Sepehr Ayandehsazan (ASA), ex-Emennet Pasargad",
        "Nimbus Manticore / UNC1549",
        "MuddyWater",
        "Seedworm / MuddyWater / Temp Zagros / Static Kitten",
        "Iran-affiliated APT / unknown",
    ]
    assert sum(len(candidate.sources) for candidate in result.candidates) == 12
    sources = [source for candidate in result.candidates for source in candidate.sources]
    assert [(source.published_at, source.period_relation, source.role) for source in sources] == [
        (date(2026, 8, 3), PeriodRelation.IN_PERIOD, SourceRole.RELAY),
        (date(2026, 5, 24), PeriodRelation.OUTSIDE_PERIOD, SourceRole.PRIMARY),
        (date(2026, 8, 5), PeriodRelation.IN_PERIOD, SourceRole.RELAY),
        (date(2026, 8, 3), PeriodRelation.IN_PERIOD, SourceRole.RELAY),
        (date(2026, 5, 22), PeriodRelation.OUTSIDE_PERIOD, SourceRole.PRIMARY),
        (date(2026, 6, 1), PeriodRelation.OUTSIDE_PERIOD, SourceRole.INDEPENDENT),
        (date(2026, 8, 3), PeriodRelation.IN_PERIOD, SourceRole.RELAY),
        (date(2026, 4, 14), PeriodRelation.OUTSIDE_PERIOD, SourceRole.PRIMARY),
        (date(2026, 8, 3), PeriodRelation.IN_PERIOD, SourceRole.RELAY),
        (date(2026, 5, 12), PeriodRelation.OUTSIDE_PERIOD, SourceRole.PRIMARY),
        (date(2026, 8, 3), PeriodRelation.IN_PERIOD, SourceRole.RELAY),
        (None, PeriodRelation.OUTSIDE_PERIOD, SourceRole.PRIMARY),
    ]
    assert [source.canonical_url for source in sources] == [
        "https://ics-cert.kaspersky.com/publications/reports/2026/08/03/"
        "apt-and-financial-attacks-on-industrial-organizations-in-q2-2026",
        "https://profero.io/blog/war-between-wars",
        "https://www.coolingpost.com/world-news/cyber-attacker-targets-refrigeration-plant",
        "https://ics-cert.kaspersky.com/publications/reports/2026/08/03/"
        "apt-and-financial-attacks-on-industrial-organizations-in-q2-2026",
        "https://research.checkpoint.com/2026/"
        "fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict",
        "https://www.nextron-systems.com/2026/06/01/"
        "detecting-nimbus-manticore-and-their-sideloading-infection-chains",
        "https://ics-cert.kaspersky.com/publications/reports/2026/08/03/"
        "apt-and-financial-attacks-on-industrial-organizations-in-q2-2026",
        "https://oasis-security.io/blog/260414-Iran",
        "https://ics-cert.kaspersky.com/publications/reports/2026/08/03/"
        "apt-and-financial-attacks-on-industrial-organizations-in-q2-2026",
        "https://www.security.com/threat-intelligence/iran-seedworm-electronics",
        "https://ics-cert.kaspersky.com/publications/reports/2026/08/03/"
        "apt-and-financial-attacks-on-industrial-organizations-in-q2-2026",
        "https://www.cisa.gov/news-events/cybersecurity-advisories/aa26-097a",
    ]
    assert result.candidates[0].sources[0].publisher == "Kaspersky ICS CERT"
    assert result.candidates[0].sources[0].published_at == date(2026, 8, 3)
    assert result.candidates[0].sources[0].period_relation is PeriodRelation.IN_PERIOD
    assert result.candidates[0].sources[0].role is SourceRole.RELAY
    assert result.candidates[1].sources[1].canonical_url == (
        "https://research.checkpoint.com/2026/"
        "fast-and-furious-nimbus-manticore-operations-during-the-iranian-conflict"
    )
    assert all(
        "\npublished" not in source.publisher
        for candidate in result.candidates
        for source in candidate.sources
    )


def test_escaped_keys_enum_values_markdown_url_and_unknown_field_are_safe() -> None:
    result = parse(
        r"""## SUBJECT S1
title: Sujet échappé
presentation: Première ligne.
technical\_potential: 4
actor-campaign: Example Actor
technical-reason: Détails visibles.
uncertainty: Attribution provisoire.
### PUBLICATION P1
title: Rapport
url: [rapport](https://Example.test/report/?utm_source=chatgpt)
publisher: Example Lab
unknown-field: ne doit pas polluer publisher
ce texte non indenté n'est pas une continuation
published-at: 2026-05-02
period: in\_period
role: independent
ioc-visibility: visible
visible-ioc-types: ipv4, sha-256
visible-iocs:
  - 192.0.2.1
  - AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA
publisher-ioc-count: 2
ioc-note: Deux valeurs visibles.
"""
    )

    candidate = result.candidates[0]
    source = candidate.sources[0]
    assert candidate.technical_potential == 4
    assert candidate.actor_or_campaign == "Example Actor"
    assert source.canonical_url == "https://example.test/report"
    assert source.publisher == "Example Lab"
    assert source.period_relation is PeriodRelation.IN_PERIOD
    assert source.role is SourceRole.INDEPENDENT
    assert [ioc.proposed_type.value for ioc in candidate.provisional_iocs] == ["ipv4", "sha256"]
    assert all(ioc.status.value == "provisional_visible" for ioc in candidate.provisional_iocs)


def test_provisional_iocs_are_typed_warned_deduplicated_and_keep_provenance() -> None:
    model_run_id = uuid4()
    result = parse_discovery_report(
        """## SUBJECT S1
title: Sujet IOC
presentation: Valeurs explicitement visibles.
technical-potential: 4
### PUBLICATION P1
title: Première source
url: https://one.example/report
ioc-visibility: visible
visible-ioc-types: domain, ipv4
visible-iocs: 192.0.2.1; evil.example
### PUBLICATION P2
title: Seconde source
url: https://two.example/report
ioc-visibility: visible
visible-ioc-types: ipv4, url
visible-iocs:
  - 192.0.2.1
  - https://evil.example/path?x=1
""",
        visible_citations=[],
        period_start=date(2026, 5, 1),
        period_end=date(2026, 5, 31),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        research_model_run_id=model_run_id,
    )

    iocs = result.candidates[0].provisional_iocs
    assert [(ioc.raw_value, ioc.normalized_value, ioc.proposed_type.value) for ioc in iocs] == [
        ("192.0.2.1", "192.0.2.1", "ipv4"),
        ("evil.example", "evil.example", "domain"),
        (
            "https://evil.example/path?x=1",
            "https://evil.example/path?x=1",
            "url",
        ),
    ]
    assert iocs[0].model_run_id == model_run_id
    assert "type_conflict" in iocs[0].warnings
    assert "type_conflict" in iocs[1].warnings
    assert [relation.publication_ref for relation in iocs[0].publication_relations] == ["P1", "P2"]
    assert all(relation.markdown_block for relation in iocs[0].publication_relations)
