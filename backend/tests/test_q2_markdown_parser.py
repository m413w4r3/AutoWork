from __future__ import annotations

import hashlib

from cti_app.application.production_artifact_verification import (
    ProposalStatus,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_parsers import (
    DetectionRule,
    Q2ArtifactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
    project_q2_source_output,
)
from cti_app.application.production_prompts import (
    EXTRACTION_PROMPT_VERSION,
    IOC_RULES_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.domain.production import DetectionRuleType, ExtractionProfile


def _parse(text: str):
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    return result.value


def test_full_prompt_requires_compact_facts_iocs_and_rules() -> None:
    prompt = ProductionPromptTemplates.get_extraction_prompt(
        "Subject",
        source_id="S1",
        source_title="Source",
        source_url="https://source.example/report",
        profile=ExtractionProfile.FULL,
        archived_source_content="archived source",
    )

    assert "# FACTS" in prompt
    assert "# IOCS" in prompt
    assert "# RULES" in prompt
    assert "# FACT\n" not in prompt
    assert "indicator-status" not in prompt
    assert EXTRACTION_PROMPT_VERSION == "11"


def test_ioc_rules_prompt_forbids_facts_and_narrative_extraction() -> None:
    prompt = ProductionPromptTemplates.get_extraction_prompt(
        "Subject",
        source_title="Source",
        source_url="https://source.example/report",
        profile=ExtractionProfile.IOC_RULES,
        archived_source_content="archived source",
    )

    assert "# IOCS" in prompt
    assert "# RULES" in prompt
    assert "# FACTS" not in prompt
    assert "Do not emit FACTS" in prompt
    assert "narrative" in prompt
    assert IOC_RULES_PROMPT_VERSION == "4"


def test_compact_grouped_lists_parse_100_confirmed_iocs() -> None:
    values = "\n".join(f"- 192.0.2.{index + 1}" for index in range(100))
    output = _parse(f"# IOCS\n\n## confirmed\n\nip:\n{values}\n")

    assert len(output.artifacts) == 100
    assert all(item.indicator_status == "confirmed_ioc" for item in output.artifacts)
    assert all(item.context == "" and item.evidence_quote == "" for item in output.artifacts)


def test_status_is_inferred_and_optional_context_is_supported() -> None:
    output = _parse(
        """# IOCS

## confirmed
domain:
- evil.example :: C2

## contextual
domain:
- provider.example
"""
    )

    assert output.artifacts[0].indicator_status == "confirmed_ioc"
    assert output.artifacts[0].context == "C2"
    assert output.artifacts[1].indicator_status == "contextual"
    assert output.artifacts[1].context == ""


def test_full_facts_are_grouped_without_required_evidence() -> None:
    output = _parse(
        """# FACTS

## malware
- ExampleRAT :: payload family

## ttps
- T1059 :: shell
"""
    )

    assert [(fact.category, fact.value, fact.context) for fact in output.facts] == [
        ("malware", "ExampleRAT", "payload family"),
        ("ttps", "T1059", "shell"),
    ]
    assert output.facts[1].attack_id == "T1059"


def test_old_verbose_q2_dialect_is_not_supported() -> None:
    result = parse_q2_proposals_markdown(
        """# ARTIFACT
artifact-type: domain
value: evil.example
indicator-status: confirmed_ioc
context: C2
evidence: source
"""
    )

    assert not result.usable
    assert "q2_compact_sections_missing" in result.errors


def test_source_ids_are_attached_by_verifier_not_required_from_model() -> None:
    output = _parse("# IOCS\n\n## confirmed\n\nip:\n- 192.0.2.10\n")
    result = verify_q2_proposals(
        (Q2ProposalSubmission(output=output, source_ids=("S42",), model_run_id="run-1"),)
    )

    item = result.canonical.items[0]
    assert item.source_ids == ("S42",)
    assert item.model_run_ids == ("run-1",)
    assert item.provenance.value == "source"
    assert item.normalized_value == "192.0.2.10"


def test_excluded_and_placeholder_values_are_rejected_or_omitted() -> None:
    output = _parse(
        """# IOCS

## confirmed
domain:
- example[.]com
- <redacted>
"""
    )
    rejected = verify_q2_proposals((Q2ProposalSubmission(output=output, source_ids=("S1",)),))
    assert not rejected.canonical.items
    assert all(item.status is ProposalStatus.REJECTED for item in rejected.diagnostics)

    excluded = Q2SourceOutput(
        artifacts=[
            Q2ArtifactProposal(
                value="evil.com",
                artifact_type="domain",
                indicator_status="excluded",
            )
        ]
    )
    excluded_result = verify_q2_proposals(
        (Q2ProposalSubmission(output=excluded, source_ids=("S1",)),)
    )
    assert excluded_result.rejected[0].reason_code == "excluded_artifact_not_emitted"


def test_defanged_ioc_literal_is_preserved_while_normalizing_locally() -> None:
    visible = r"hxxps\://evil[.]com/path"
    output = _parse(f"# IOCS\n\n## confirmed\n\nurl:\n- {visible}\n")
    assert output.artifacts[0].value == visible

    result = verify_q2_proposals((Q2ProposalSubmission(output=output, source_ids=("S1",)),))
    item = result.canonical.items[0]
    assert item.value == visible
    assert item.normalized_value == "https://evil.com/path"


def test_rule_bodies_are_literal_for_all_supported_languages() -> None:
    bodies = {
        "yara": "rule ExampleRule { condition: true }",
        "sigma": "title: Example\nlogsource:\n  product: windows",
        "suricata": 'alert tcp any any -> any 443 (msg:"x"; sid:1;)',
        "snort": 'alert tcp any any -> any 443 (msg:"x"; sid:2;)',
    }
    text = "# RULES\n\n" + "\n\n".join(
        f"## {rule_type}: Example\n```{rule_type}\n{body}\n```"
        for rule_type, body in bodies.items()
    )
    output = _parse(text)

    assert [rule.rule_type for rule in output.rules] == [
        DetectionRuleType.YARA,
        DetectionRuleType.SIGMA,
        DetectionRuleType.SURICATA,
        DetectionRuleType.SNORT,
    ]
    assert [rule.body for rule in output.rules] == list(bodies.values())
    assert all(rule.context == "" and rule.evidence_quote == "" for rule in output.rules)


def test_flattened_yara_stays_flattened_and_defanged_rule_text_stays_visible() -> None:
    body = r'rule Flat { strings: $u = "hxxps\://evil[.]com" condition: $u }'
    output = _parse(f"# RULES\n\n## yara: Flat\n```yara\n{body}\n```")

    assert len(output.rules) == 1
    assert output.rules[0].body == body
    assert "\n" not in output.rules[0].body


def test_incomplete_rule_goes_to_uncertainties_not_rules() -> None:
    result = parse_q2_proposals_markdown(
        """# RULES

## yara: Broken
```yara
rule Broken {
  condition: true
```
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.rules == []
    assert "rule_truncated_not_promoted" in result.value.uncertainties


def test_cached_full_projection_keeps_rules_for_ioc_rules() -> None:
    output = Q2SourceOutput(
        rules=[Q2RuleProposal(rule_type="yara", body="rule R { condition: true }", name="R")]
    )

    projected = project_q2_source_output(output, ExtractionProfile.IOC_RULES)
    assert projected.rules == output.rules


def test_rule_canonical_body_hash_is_based_on_literal_body() -> None:
    body = "rule R { condition: true }"
    rule = DetectionRule(
        rule_type=DetectionRuleType.YARA,
        name="R",
        body=body,
        source_ids=("S1",),
        context="",
        evidence_quote="",
        supported=True,
        model_run_ids=(),
        sha256=hashlib.sha256(body.encode()).hexdigest(),
    )
    assert rule.sha256 == hashlib.sha256(body.encode()).hexdigest()
