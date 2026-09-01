from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path
from uuid import uuid4

import pytest

from cti_app.application.edition_workspace import EditionWorkspaceMaterializer
from cti_app.application.production_artifact_verification import (
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_parsers import (
    DetectionRule,
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    ParsedSource,
    Q2RuleProposal,
    Q2SourceOutput,
    ReferenceReport,
    TechnicalExtraction,
    parse_q2_proposals_markdown,
    project_q2_source_output,
    technical_extraction_from_json,
    technical_extraction_to_json,
)
from cti_app.application.production_rendering import (
    build_reference_numbering,
    collect_indicators,
    render_publication_markdown,
)
from cti_app.application.production_state import ProductionStateSnapshotV1
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import DetectionRuleType, ExtractionProfile
from cti_app.domain.publication import ArtifactType


def _rule(rule_type: DetectionRuleType, name: str, body: str, source: str = "S3") -> DetectionRule:
    return DetectionRule(
        rule_type=rule_type,
        name=name,
        body=body,
        source_ids=(source,),
        context="published rule",
        evidence_quote="The source publishes this rule",
        supported=True,
        model_run_ids=(),
        sha256=hashlib.sha256(body.encode()).hexdigest(),
    )


def test_q2_rule_parser_preserves_multiline_bodies_and_rule_type_authority() -> None:
    yara_body = (
        'rule ExampleRule {\n    strings:\n        $a = "x:y {q}"\n    condition:\n        $a\n}'
    )
    sigma_body = (
        "title: Sigma: {quoted}\nlogsource:\n  product: windows\n"
        'detection:\n  selection:\n    CommandLine|contains: "x:y"\n  condition: selection'
    )
    text = f"""RULE yara: ExampleRule

```text
{yara_body}
```

RULE sigma: SigmaExample

```yaml
{sigma_body}
```

RULE suricata: alert-example

```suricata
alert http any any -> any any (msg:"x:y {{q}}"; content:"abc"; sid:1;)
```

RULE snort: alert-snort

```snort
alert tcp any any -> any 443 (msg:"snort: {{quoted}}"; sid:2;)
```
"""

    result = parse_q2_proposals_markdown(text)

    assert result.usable, result.errors
    assert [rule.rule_type for rule in result.value.rules] == [
        DetectionRuleType.YARA,
        DetectionRuleType.SIGMA,
        DetectionRuleType.SURICATA,
        DetectionRuleType.SNORT,
    ]
    assert result.value.rules[0].body == yara_body
    assert result.value.rules[1].body == sigma_body


def test_q2_truncated_rule_is_warned_and_not_promoted() -> None:
    result = parse_q2_proposals_markdown(
        """RULE yara: Truncated

```yara
rule Truncated {
  condition:
    true
"""
    )

    assert not result.usable
    assert result.errors == ["q2_no_payload"]
    assert result.value is None
    assert "rule_truncated_not_promoted" in result.warnings


def test_rules_are_deduplicated_by_type_and_body_hash_with_all_sources() -> None:
    body = "rule Same { condition: true }"
    output_one = Q2SourceOutput(
        rules=[
            Q2RuleProposal(
                rule_type="yara",
                name="Same",
                body=body,
                context="S1",
                evidence_quote="S1 quote",
            )
        ]
    )
    output_two = Q2SourceOutput(
        rules=[
            Q2RuleProposal(
                rule_type="yara",
                name="Same",
                body=body,
                context="S5",
                evidence_quote="S5 quote",
            )
        ]
    )

    result = verify_q2_proposals(
        (
            Q2ProposalSubmission(output=output_one, source_ids=("S1",), model_run_id="m1"),
            Q2ProposalSubmission(output=output_two, source_ids=("S5",), model_run_id="m5"),
        )
    )

    assert len(result.canonical.rules) == 1
    assert result.canonical.rules[0].source_ids == ("S1", "S5")
    assert result.canonical.rules[0].model_run_ids == ("m1", "m5")
    assert result.canonical.rules[0].sha256 == hashlib.sha256(body.encode()).hexdigest()


def test_same_rule_name_with_different_bodies_remains_distinct() -> None:
    result = verify_q2_proposals(
        (
            Q2ProposalSubmission(
                output=Q2SourceOutput(
                    rules=[
                        Q2RuleProposal(
                            rule_type="yara",
                            name="Same",
                            body="rule Same { condition: true }",
                            context="one",
                            evidence_quote="one",
                        ),
                        Q2RuleProposal(
                            rule_type="yara",
                            name="Same",
                            body="rule Same { condition: false }",
                            context="two",
                            evidence_quote="two",
                        ),
                    ]
                ),
                source_ids=("S1",),
            ),
        )
    )

    assert len(result.canonical.rules) == 2


def test_canonical_rules_round_trip_and_old_extraction_json_stays_compatible() -> None:
    rule = _rule(DetectionRuleType.SIGMA, "Sigma", "title: test\nlogsource:\n  product: windows")
    extraction = TechnicalExtraction(items=(), rules=(rule,))

    payload = technical_extraction_to_json(extraction)
    restored = technical_extraction_from_json(payload)
    legacy = technical_extraction_from_json({"items": [], "uncertainties": []})

    assert restored.rules == (rule,)
    assert legacy.rules == ()
    assert payload["rules"][0]["sha256"] == rule.sha256


def test_full_cached_rules_are_kept_in_ioc_rules_projection() -> None:
    source_rule = Q2RuleProposal(
        rule_type="snort",
        name="Cached",
        body="alert tcp any any -> any 443 (sid:9;)",
        context="cached",
        evidence_quote="source",
    )
    output = Q2SourceOutput(rules=[source_rule])

    projected = project_q2_source_output(output, ExtractionProfile.IOC_RULES)

    assert projected.rules == [source_rule]


def test_rules_are_not_iocs_or_publication_body() -> None:
    rule = _rule(DetectionRuleType.YARA, "Example", "rule Example { condition: true }")
    legacy_rule_item = ExtractionItem(
        local_id="legacy-rule",
        category="detections",
        value="Example",
        context="legacy",
        artifact_type=ArtifactType.YARA_RULE,
        attack_id=None,
        reference_ids=(),
        source_ids=("S1",),
        supported=True,
        indicator_status=IndicatorStatus.CONFIRMED_IOC,
        display_policy=DisplayPolicy.IOC_SECTION,
    )
    extraction = TechnicalExtraction(items=(legacy_rule_item,), rules=(rule,))
    source = ParsedSource(
        local_id="S1",
        title="Source",
        url="https://source.example/report",
        canonical_url="https://source.example/report",
        publisher="Source",
        published_at=date(2026, 8, 1),
        role=SourceRole.PRIMARY,
    )
    report = ReferenceReport(sources=(source,), events=())
    publication = render_publication_markdown(
        subject_title="Sujet",
        report=report,
        extraction=extraction,
        synthesis_text="La règle est publiée [S1].",
        numbering=build_reference_numbering(report, "La règle est publiée [S1]."),
    )

    assert collect_indicators(extraction) == []
    assert rule.body not in publication


def _state() -> ProductionStateSnapshotV1:
    return ProductionStateSnapshotV1.model_validate(
        {
            "format": "autowork.production-state",
            "schema_version": 1,
            "exported_at": "2026-08-29T10:00:00Z",
            "origin": {
                "subject_title": "Sujet",
                "editorial_type": "brief",
                "profile": "brief_auto",
                "research_date": "2026-08-01",
            },
            "artifacts": {
                "references": {"input_hash": "a" * 64, "canonical_content": {"items": []}},
                "extraction": {"input_hash": "b" * 64, "canonical_content": {"items": []}},
                "synthesis": {"input_hash": "c" * 64, "rendered_content": "Article"},
            },
            "content_sha256": "d" * 64,
        }
    )


@pytest.mark.asyncio
async def test_rule_sidecars_have_deterministic_names_extensions_and_bytes(tmp_path: Path) -> None:
    rules = (
        _rule(DetectionRuleType.YARA, "Example/Rule", "rule Example {\n  condition: true\n}"),
        _rule(
            DetectionRuleType.SIGMA,
            "Sigma Rule",
            "title: Sigma\nlogsource:\n  product: windows",
        ),
        _rule(DetectionRuleType.SURICATA, "HTTP Rule", "alert http any any -> any any (sid:3;)"),
        _rule(DetectionRuleType.SNORT, "Snort Rule", "alert tcp any any -> any 443 (sid:4;)"),
    )
    materializer = EditionWorkspaceMaterializer(tmp_path / "editions")
    kwargs = {
        "edition_id": uuid4(),
        "period": date(2026, 8, 1),
        "country_code": "FR",
        "position": 1,
        "subject_id": uuid4(),
        "subject_title": "Sujet",
        "production_state": _state(),
        "rules": rules,
    }

    first = await materializer.materialize(**kwargs)
    before = {
        path.name: path.read_bytes()
        for path in (first.item_path / "article/rules").iterdir()
        if path.is_file()
    }
    second = await materializer.materialize(**kwargs)
    after = {
        path.name: path.read_bytes()
        for path in (second.item_path / "article/rules").iterdir()
        if path.is_file()
    }
    manifest = json.loads((first.item_path / "article/rules/manifest.json").read_text())

    assert first.rule_sidecar_error is None
    assert before == after
    assert {Path(item["filename"]).suffix for item in manifest["rules"]} == {
        ".yar",
        ".yml",
        ".rules",
    }
    for item in manifest["rules"]:
        sidecar = first.item_path / "article/rules" / item["filename"]
        assert sidecar.read_bytes() == next(
            rule.body.encode() for rule in rules if rule.sha256 == item["sha256"]
        )
