from datetime import date
from types import SimpleNamespace

from cti_app.application.production_parsers import parse_reference_report
from cti_app.application.production_prompts import ProductionPromptTemplates
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator


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

    assert pack["version"] == "2"
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
