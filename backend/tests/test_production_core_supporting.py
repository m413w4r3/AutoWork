import json
from datetime import date
from types import SimpleNamespace

from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    ParseResult,
    SynthesisViolation,
    TechnicalExtraction,
    parse_reference_report,
)
from cti_app.application.production_prompts import (
    SYNTHESIS_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _repair_problem_descriptions,
)
from cti_app.domain.production import DetectionRule, DetectionRuleType
from cti_app.domain.publication import ArtifactType


def test_references_prompt_keeps_core_and_supporting_separate() -> None:
    prompt = ProductionPromptTemplates.get_references_prompt(
        subject_title="Subject",
        subject_description="Description",
        actor_info="Actor",
        technical_summary="Summary",
        research_date="2026-08-01",
        period_start="2026-07-01",
        period_end="2026-07-31",
        core_sources_text="- https://core.example/report",
        supporting_sources_text="- https://supporting.example/report",
    )

    assert "**Core Publications**" in prompt
    assert "**Previously Known Supporting References**" in prompt
    assert "they do not replace them" in prompt


def test_synthesis_pack_assigns_tiers_without_defaulting_unknown_to_core() -> None:
    report = parse_reference_report(
        """# REFERENCES
editorial-title: Test
## SOURCE S1
title: Core
url: https://core.example/report
publisher: Core
published-at: 2026-07-01
role: primary
## SOURCE S2
title: Supporting
url: https://supporting.example/report
publisher: Supporting
published-at: 2026-07-02
role: independent
## SOURCE S3
title: Unknown
url: https://unknown.example/report
publisher: Unknown
published-at: 2026-07-03
role: unknown
## EVENT R1
date: 2026-07-03
sources: S1, S2, S3
text: Event
""",
        date(2026, 8, 1),
    ).value
    assert report is not None
    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        report,
        SimpleNamespace(items=(), uncertainties=()),
        {
            "https://core.example/report": "core",
            "https://supporting.example/report": "supporting",
        },
    )

    assert pack["version"] == "3"
    assert [source["tier"] for source in pack["reference_report"]["sources"]] == [
        "core",
        "supporting",
        "unknown",
    ]


def test_synthesis_prompt_makes_core_backbone_without_quota() -> None:
    prompt = ProductionPromptTemplates.get_synthesis_prompt("Subject")

    assert "CORE sources are the editorial backbone" in prompt
    assert "SUPPORTING sources are secondary evidence" in prompt
    assert "percentage" not in prompt.lower()
    assert "quota" not in prompt.lower()


def test_synthesis_prompt_version_matches_v7_template() -> None:
    assert SYNTHESIS_PROMPT_VERSION == "7"
    assert hasattr(ProductionPromptTemplates, "TECHNICAL_SYNTHESIS_V7")


def test_synthesis_prompt_requires_technical_cti_depth() -> None:
    """The V7 contract, not any particular model wording."""
    prompt = ProductionPromptTemplates.get_synthesis_prompt("Subject")
    lowered = prompt.lower()

    # Editorial priority: the discriminating CTI axes must be requested.
    for requirement in (
        "infection and execution chain",
        "processes, tools and commands",
        "persistence, privilege, evasion or anti-analysis mechanisms",
        "c2 protocols, communication structure and infrastructure role",
        "behavioral hunting or detection pivots",
        "differences between variants, campaigns or operators",
        "attribution",
    ):
        assert requirement in lowered

    # Concrete technical values must survive the IOC-inventory prohibition.
    for retained in (
        "parent/child execution relationships",
        "command-line patterns",
        "registry or scheduled-task persistence",
        "distinctive file paths",
        "local ports",
    ):
        assert retained in lowered
    assert "not an ioc inventory" in lowered or "not an IOC inventory" in prompt

    # Shape without a rigid word count.
    assert "3 to 6 dense paragraphs" in prompt

    # Attribution caution.
    assert "stronger attribution than the evidence supports" in prompt

    # Supporting sources keep their technical value.
    assert "solely\nbecause it comes from a supporting source" in prompt

    # No generic SOC playbook.
    assert "Il est recommandé de\nbloquer" in prompt
    assert "Les organisations devraient" in prompt
    assert "Never invent a SOC playbook." in prompt

    # Literal detection rules are reserved for future annexes; behavioral
    # detection and hunting pivots remain part of the main synthesis.
    for rule_format in ("yara", "sigma", "suricata", "snort"):
        assert rule_format not in lowered

    # Timeline restatement is discouraged.
    assert "Do not repeat chronology merely to restate the reference timeline." in prompt

    # Preserved invariants.
    assert "Produce no Markdown title or heading." in prompt
    assert "Produce no raw URL." in prompt
    assert "Do not copy the IOC inventory" in prompt
    assert "no final bibliography" in prompt

    # V7 separates IOC destination from artifact type.
    assert "may appear in prose only when their display-policy permits" in prompt
    assert "both body and IOC-section use" in prompt
    assert "not IOC-section inventory" in prompt
    assert "exhaustive file inventory" in prompt
    assert "A precise IOC value may appear only when its display-policy is both." not in prompt


def test_synthesis_repair_prompt_stays_structural() -> None:
    repaired = ProductionPromptTemplates.get_format_repair_prompt(
        stage="synthesis", problems=["heading"]
    )

    assert "Do not research, add, remove, or alter any fact." in repaired
    assert "deepen" not in repaired.lower()
    assert "improve" not in repaired.lower()


def _item(category: str, value: str, context: str) -> ExtractionItem:
    return ExtractionItem(
        local_id=f"E{abs(hash(value)) % 1000}",
        category=category,
        value=value,
        context=context,
        artifact_type=None,
        attack_id=None,
        reference_ids=(),
        source_ids=("S1",),
        supported=True,
        indicator_status=IndicatorStatus.CONTEXTUAL,
        display_policy=DisplayPolicy.BODY_ONLY,
    )


def _minimal_report():
    report = parse_reference_report(
        """# REFERENCES
editorial-title: Test
## SOURCE S1
title: Core
url: https://core.example/report
publisher: Core
published-at: 2026-07-01
role: primary
## EVENT R1
date: 2026-07-01
sources: S1
text: Event
""",
        date(2026, 8, 1),
    ).value
    assert report is not None
    return report


def test_synthesis_pack_keeps_behavioral_categories() -> None:
    extraction = TechnicalExtraction(
        items=(
            _item("infection_chain", "wscript.exe lance deno.exe", "chaîne"),
            _item("commands", "curl -o deno.exe https://…", "téléchargement runtime"),
            _item("persistence", "clé Run HKCU\\...\\Run", "persistance"),
            _item("protocols", "HTTP POST /api/v1/ping", "C2"),
            _item("other_technical", "port local 51337", "loopback"),
        ),
        uncertainties=(),
    )

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    categories = {item["category"] for item in pack["technical_extraction"]["items"]}
    assert categories == {
        "infection_chain",
        "commands",
        "persistence",
        "protocols",
        "other_technical",
    }
    values = {item["value"] for item in pack["technical_extraction"]["items"]}
    assert "clé Run HKCU\\...\\Run" in values
    assert "port local 51337" in values


def test_synthesis_pack_excludes_detection_rules_and_preserves_extraction() -> None:
    rule = DetectionRule(
        rule_type=DetectionRuleType.YARA,
        name="APT_ExampleRAT_loader",
        body='rule APT_ExampleRAT_loader { strings: $a = "SECRET_BODY" condition: $a }',
        source_ids=("S1",),
        context="règle publiée par la source",
        evidence_quote="citation interne",
        supported=True,
        model_run_ids=("run-1",),
        sha256="a" * 64,
    )
    unsupported = DetectionRule(
        rule_type=DetectionRuleType.SIGMA,
        name="unsupported_rule",
        body="title: unsupported",
        source_ids=("S1",),
        context="",
        evidence_quote="",
        supported=False,
        model_run_ids=(),
        sha256="b" * 64,
    )
    extraction = TechnicalExtraction(items=(), uncertainties=(), rules=(rule, unsupported))

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    assert pack["version"] == "3"
    assert "detection_rules" not in pack["technical_extraction"]
    assert extraction.rules == (rule, unsupported)
    serialized = json.dumps(pack, ensure_ascii=False)
    assert "SECRET_BODY" not in serialized
    assert "APT_ExampleRAT_loader" not in serialized
    assert "citation interne" not in serialized
    assert "run-1" not in serialized
    assert "a" * 64 not in serialized
    assert "unsupported_rule" not in serialized


def test_synthesis_pack_omits_detection_rules_when_none() -> None:
    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(),
        TechnicalExtraction(items=(), uncertainties=(), rules=()),
        {"https://core.example/report": "core"},
    )

    assert "detection_rules" not in pack["technical_extraction"]


# --- Q4 pack: body detail keeps its value, IOC-section noise is dropped -----


def _artifact_item(
    local_id: str,
    value: str,
    artifact_type: ArtifactType,
    *,
    status: IndicatorStatus = IndicatorStatus.CONFIRMED_IOC,
    policy: DisplayPolicy = DisplayPolicy.IOC_SECTION,
    category: str = "network_artifacts",
    context: str = "",
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
        indicator_status=status,
        display_policy=policy,
    )


def _dust_specter_pack_extraction() -> TechnicalExtraction:
    hashes = tuple(
        _artifact_item(f"H{index}", f"{index:064x}", ArtifactType.HASH)
        for index in range(40)
    )
    domains = tuple(
        _artifact_item(f"D{index}", f"c2-{index}.example", ArtifactType.DOMAIN)
        for index in range(10)
    )
    files = tuple(
        _artifact_item(
            f"F{index}",
            name,
            ArtifactType.FILENAME,
            policy=DisplayPolicy.BODY_ONLY,
            category="files",
            context="chaîne d'exécution",
        )
        for index, name in enumerate(("libvlc.dll", "in.txt", "hostfxr.dll"))
    )
    filepath = _artifact_item(
        "P1",
        "C:\\Users\\Public\\twintask\\in.txt",
        ArtifactType.FILEPATH,
        policy=DisplayPolicy.BODY_ONLY,
        category="files",
        context="chemin de travail",
    )
    cve = _artifact_item(
        "C1",
        "CVE-2026-1234",
        ArtifactType.CVE,
        policy=DisplayPolicy.BODY_ONLY,
        category="cves",
        context="vulnérabilité exploitée",
    )
    behavioral = (
        _item("infection_chain", "VLC.exe charge libvlc.dll", "side-loading"),
        _item("persistence", "tâche planifiée TWINTASK", "persistance"),
    )
    return TechnicalExtraction(
        items=(*hashes, *domains, *files, filepath, cve, *behavioral),
        uncertainties=(),
    )


def test_synthesis_pack_drops_metadata_only_ioc_rows_and_keeps_body_detail() -> None:
    extraction = _dust_specter_pack_extraction()

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    items = pack["technical_extraction"]["items"]
    assert pack["version"] == "3"

    # No IOC-section row survives with neither a value nor context.
    assert not [
        item
        for item in items
        if item["artifact_type"] in {"hash", "domain"}
        and item["display_policy"] == "ioc_section"
    ]

    values = {item.get("value") for item in items}
    for expected in (
        "libvlc.dll",
        "in.txt",
        "hostfxr.dll",
        "C:\\Users\\Public\\twintask\\in.txt",
        "CVE-2026-1234",
    ):
        assert expected in values

    # Every remaining row carries either a publishable value or usable context.
    assert all(item.get("value") or item["context"].strip() for item in items)

    # The 50 metadata-only IOC rows are gone, the behavioral evidence stays.
    assert len(items) == len(extraction.items) - 50
    serialized = json.dumps(pack, ensure_ascii=False)
    assert "c2-0.example" not in serialized
    assert f"{0:064x}" not in serialized

    # The canonical extraction is untouched.
    assert len(extraction.items) == 57
    assert sum(item.artifact_type is ArtifactType.HASH for item in extraction.items) == 40


def test_synthesis_pack_keeps_ioc_section_rows_that_still_carry_context() -> None:
    extraction = TechnicalExtraction(
        items=(
            _artifact_item("D1", "c2.example", ArtifactType.DOMAIN, context="serveur de C2"),
        ),
        uncertainties=(),
    )

    pack = ProductionWorkflowOrchestrator._build_synthesis_evidence_pack(
        _minimal_report(), extraction, {"https://core.example/report": "core"}
    )

    items = pack["technical_extraction"]["items"]
    assert len(items) == 1
    assert "value" not in items[0]
    assert items[0]["context"] == "serveur de C2"


def test_synthesis_repair_prompt_names_the_offending_value() -> None:
    result: ParseResult[str] = ParseResult()
    result.errors.extend(["ioc_repeated_in_body", "ioc_repeated_in_body"])
    result.violations.extend(
        (
            SynthesisViolation("ioc_repeated_in_body", "domain:evil.example", (10, 22)),
            SynthesisViolation("ioc_repeated_in_body", "ip:203[.]0[.]113[.]9", (30, 45)),
        )
    )

    problems = _repair_problem_descriptions(result)
    prompt = ProductionPromptTemplates.get_format_repair_prompt(
        stage="synthesis", problems=problems
    )

    assert problems == [
        "ioc_repeated_in_body: domain:evil.example",
        "ioc_repeated_in_body: ip:203[.]0[.]113[.]9",
    ]
    assert "ioc_repeated_in_body: domain:evil.example" in prompt
    assert "ioc_repeated_in_body: ip:203[.]0[.]113[.]9" in prompt
    assert "Do not research, add, remove, or alter any fact." in prompt


def test_repair_problem_descriptions_fall_back_to_codes() -> None:
    result: ParseResult[str] = ParseResult()
    result.errors.append("empty_response")

    assert _repair_problem_descriptions(result) == ["empty_response"]
