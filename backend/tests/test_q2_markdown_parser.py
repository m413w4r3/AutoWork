from __future__ import annotations

import hashlib
from datetime import date
from typing import get_args

import pytest

from cti_app.application.production_artifact_verification import (
    ProposalStatus,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_parsers import (
    Q2_EXTRACTION_CONTRACT_VERSION,
    Q2_MARKDOWN_PARSER_VERSION,
    Q2_SCHEMA_VERSION,
    DetectionRule,
    Q2ArtifactProposal,
    Q2EventProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
    project_q2_source_output,
    q2_source_output_from_json,
    q2_source_output_to_json,
    sanitize_bridge_output_text,
)
from cti_app.application.production_prompts import (
    CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE,
    CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.domain.production import DetectionRuleType, ExtractionProfile
from cti_app.domain.production_extraction import EXTRACTION_FACT_CATEGORIES


def _parse(text: str):
    result = parse_q2_proposals_markdown(text)
    assert result.usable, result.errors
    assert result.value is not None
    return result.value


def test_canonical_prompts_carry_only_the_archived_capture() -> None:
    full = ProductionPromptTemplates.get_canonical_archive_extraction_prompt(
        "ExampleRAT reaches evil.example.", profile=ExtractionProfile.FULL
    )
    light = ProductionPromptTemplates.get_canonical_archive_extraction_prompt(
        "ExampleRAT reaches evil.example.", profile=ExtractionProfile.IOC_RULES
    )

    for prompt in (full, light):
        assert "ExampleRAT reaches evil.example." in prompt
        assert "Ne naviguez pas sur le Web" in prompt
        assert "http" not in prompt.replace("evil.example", "")
    assert "Utilisez les groupes FACT" in full and "Utilisez EVENT" in full
    assert "N'utilisez pas de groupes" in light
    assert CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE == {
        ExtractionProfile.FULL: "archive-full-v3",
        ExtractionProfile.IOC_RULES: "archive-ioc-rules-v3",
    }
    assert CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION == "archive-ioc-rules-batch-v3"
    assert Q2_MARKDOWN_PARSER_VERSION == "q2-markdown-v9"
    full_one_line = " ".join(full.split())
    assert "JSON" not in full and "schema" not in full
    assert "Chaque puce FACT ou EVENT" in full_one_line
    assert "extrait littéral exact de la capture" in full_one_line
    assert "Rédigez chaque description en français" in full_one_line
    assert "formulation absolue incluant l'année" in full_one_line
    assert "ordonnez-les chronologiquement" in full_one_line
    assert "captures ou segments incomplets" in full_one_line
    assert "limites de types" in full_one_line
    assert "classement de fichiers" in full_one_line


def test_ioc_group_parses_100_confirmed_iocs() -> None:
    values = "\n".join(f"- 192.0.2.{index + 1}" for index in range(100))
    output = _parse(f"IOC confirmed ip\n{values}\n")

    assert len(output.artifacts) == 100
    assert all(item.indicator_status == "confirmed_ioc" for item in output.artifacts)
    assert all(item.context == "" and item.evidence_quote == "" for item in output.artifacts)


def test_prompt_header_forms_are_accepted_by_the_wire_parser() -> None:
    output = _parse(
        "FACT malware\n- Exemple ExampleRAT :: ExampleRAT is published.\n"
        "EVENT T2 2026\n"
        "- Les opérateurs changent de serveur. :: In Q2 2026, operators changed servers.\n"
        "IOC contextual domain\n- evil.example :: infrastructure connexe\n"
        "RULE yara: Example\n```yara\nrule Example { condition: true }\n```\n"
        "UNCERTAINTIES\n- L'attribution demeure incertaine.\n"
    )

    assert [fact.category for fact in output.facts] == ["malware"]
    assert output.events[0].date_text == "T2 2026"
    assert output.artifacts[0].context == "infrastructure connexe"
    assert output.rules[0].name == "Example"
    assert output.uncertainties == ["L'attribution demeure incertaine."]


def test_ioc_status_is_header_data_and_optional_context_is_supported() -> None:
    output = _parse(
        """IOC confirmed domain
- evil.example :: C2

IOC contextual domain
- provider.example
"""
    )

    assert output.artifacts[0].indicator_status == "confirmed_ioc"
    assert output.artifacts[0].context == "C2"
    assert output.artifacts[1].indicator_status == "contextual"
    assert output.artifacts[1].context == ""


def test_blank_lines_after_headers_between_bullets_and_groups_are_neutral() -> None:
    compact = _parse(
        "IOC confirmed domain\n- evil.example\n- second.example\nFACT malware\n- ExampleRAT\n"
    )
    spaced = parse_q2_proposals_markdown(
        "IOC confirmed domain\n\n- evil.example\n\n- second.example\n\n"
        "FACT malware\n\n- ExampleRAT\n"
    )

    assert spaced.usable, spaced.errors
    assert spaced.value == compact
    assert spaced.warnings == []


def test_structural_tokens_are_case_insensitive_but_payload_is_literal() -> None:
    output = _parse(
        "ioc Confirmed DOMAIN\n"
        "- Evil.Example :: MiXeD Context\n\n"
        "fact Malware\n"
        "- CamelCase Fact\n\n"
        "RULE YARA: MiXeD Rule\n"
        "```YARA\n"
        "rule MiXeD {\n  condition: true\n}\n"
        "```\n\n"
        "uNcErTaInTiEs\n"
        "- Model Supplied Case\n"
    )

    assert [(item.artifact_type, item.value, item.context) for item in output.artifacts] == [
        ("domain", "Evil.Example", "MiXeD Context")
    ]
    assert [(fact.category, fact.value) for fact in output.facts] == [("malware", "CamelCase Fact")]
    assert output.rules[0].rule_type is DetectionRuleType.YARA
    assert output.rules[0].name == "MiXeD Rule"
    assert output.rules[0].body == "rule MiXeD {\n  condition: true\n}"
    assert output.uncertainties == ["Model Supplied Case"]


def test_fact_groups_are_self_contained_without_required_evidence() -> None:
    output = _parse(
        """FACT malware
- ExampleRAT :: payload family

FACT ttps
- T1059 :: shell
"""
    )

    assert [(fact.category, fact.value, fact.evidence_quote) for fact in output.facts] == [
        ("malware", "ExampleRAT", "payload family"),
        ("ttps", "T1059", "shell"),
    ]
    assert all(fact.context == "" for fact in output.facts)
    assert output.facts[1].attack_id == "T1059"


def test_groups_are_order_independent_and_omitted_groups_stay_empty() -> None:
    output = _parse(
        """IOC contextual domain
- contextual.example

FACT malware
- ExampleRAT

IOC confirmed ip
- 192.0.2.10
"""
    )

    assert [fact.value for fact in output.facts] == ["ExampleRAT"]
    assert [artifact.value for artifact in output.artifacts] == [
        "contextual.example",
        "192.0.2.10",
    ]
    assert output.rules == []


def test_markdown_hashes_are_optional_for_complete_headers() -> None:
    without_hashes = _parse("IOC confirmed domain\n- evil.example\n")
    with_hashes = _parse("## IOC confirmed domain\n- evil.example\n")

    assert with_hashes.artifacts == without_hashes.artifacts


def test_all_ioc_types_are_exact_and_hash_types_map_to_internal_hash() -> None:
    values = {
        "domain": "evil.example",
        "ip": "192.0.2.10",
        "url": "https://evil.example/path",
        "email": "analyst@evil.example",
        "md5": "a" * 32,
        "sha1": "b" * 40,
        "sha256": "c" * 64,
        "sha512": "d" * 128,
        "filename": "dropper.exe",
        "filepath": r"C:\\Windows\\dropper.exe",
        "cve": "CVE-2026-1234",
    }
    output = _parse(
        "\n\n".join(
            f"IOC confirmed {type_token}\n- {value}" for type_token, value in values.items()
        )
    )

    assert len(output.artifacts) == len(values)
    assert [artifact.artifact_type for artifact in output.artifacts] == [
        "hash" if type_token in {"md5", "sha1", "sha256", "sha512"} else type_token
        for type_token in values
    ]


def test_ipv6_is_not_split_as_context() -> None:
    output = _parse("IOC confirmed ip\n- 2001:db8::1\n")

    assert output.artifacts[0].value == "2001:db8::1"
    assert output.artifacts[0].context == ""


def test_unknown_heading_terminates_only_the_current_group() -> None:
    result = parse_q2_proposals_markdown(
        """FACT malware
- kept
## UNKNOWN
- ignored
IOC confirmed domain
- next.example
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [fact.value for fact in result.value.facts] == ["kept"]
    assert [artifact.value for artifact in result.value.artifacts] == ["next.example"]
    assert "q2_unknown_heading" in result.warnings


def test_partial_garbage_line_does_not_discard_later_items() -> None:
    result = parse_q2_proposals_markdown(
        """FACT malware
- ExampleRAT :: The capture names ExampleRAT.
This line is not a Q2 item.
EVENT 2024-03-02
- Les opérateurs déploient ExampleRAT.
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [fact.value for fact in result.value.facts] == ["ExampleRAT"]
    assert [event.text for event in result.value.events] == ["Les opérateurs déploient ExampleRAT."]
    assert result.warnings.count("q2_unexpected_structure") == 1


def test_fact_and_event_quotes_split_once_and_strip_bridge_markers() -> None:
    result = parse_q2_proposals_markdown(
        "FACT malware\n"
        "- Famille ExampleRAT :: The report states A :: B and names ExampleRAT "
        ':chatgpt-content-reference{index="0"}\n'
        "EVENT 2024-03-02\n"
        "- Les opérateurs déploient ExampleRAT :: On 2024-03-02, A :: B "
        "involved ExampleRAT.\n"
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.facts[0].value == "Famille ExampleRAT"
    assert result.value.facts[0].evidence_quote == "The report states A :: B and names ExampleRAT"
    assert result.value.facts[0].context == ""
    assert result.value.events[0].evidence_quote == "On 2024-03-02, A :: B involved ExampleRAT."
    assert result.value.events[0].context == ""


@pytest.mark.parametrize(
    ("header", "warning"),
    [
        ("FACT unknown_category", "q2_unknown_fact_category"),
        ("IOC unknown domain", "q2_unknown_ioc_status"),
        ("IOC confirmed unknown", "q2_unknown_ioc_type"),
    ],
)
def test_unknown_group_metadata_drops_locally_and_does_not_inherit(
    header: str, warning: str
) -> None:
    result = parse_q2_proposals_markdown(
        f"""{header}
- ignored.example
IOC contextual domain
- valid.example
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [artifact.value for artifact in result.value.artifacts] == ["valid.example"]
    assert [fact.value for fact in result.value.facts] == []
    assert warning in result.warnings


def test_malformed_item_is_dropped_and_later_items_in_group_survive() -> None:
    result = parse_q2_proposals_markdown(
        """FACT malware
- :: missing value
- ExampleRAT
IOC confirmed unsupported_type
- ignored.example
IOC contextual domain
- clearview.ai :: Reconnaissance target :chatgpt-content-reference{index="0"}
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [fact.value for fact in result.value.facts] == ["ExampleRAT"]
    assert [(artifact.value, artifact.context) for artifact in result.value.artifacts] == [
        ("clearview.ai", "Reconnaissance target")
    ]
    assert "q2_bullet_without_value" in result.warnings
    assert "q2_unknown_ioc_type" in result.warnings


def test_real_bridge_ui_marker_sample_is_sanitized_before_parsing() -> None:
    bridge_json = (
        '{"artifacts":[{"value":"clearview.ai","artifact_type":"domain",'
        '"indicator_status":"contextual","context":"Reconnaissance target enumerated with '
        'subfinder. :chatgpt-content-reference{index="0"}",'
        '"evidence_quote":"clearview.ai"}], ...}'
    )

    cleaned = sanitize_bridge_output_text(bridge_json)
    assert ':chatgpt-content-reference{index="0"}' not in cleaned
    assert "Reconnaissance target enumerated with subfinder." in cleaned

    result = parse_q2_proposals_markdown(
        "IOC contextual domain\n"
        "- clearview.ai :: Reconnaissance target enumerated with subfinder. "
        ':chatgpt-content-reference{index="0"}\n'
    )
    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.artifacts[0].context == "Reconnaissance target enumerated with subfinder."


def test_other_visible_chat_ui_markers_are_removed() -> None:
    cleaned = sanitize_bridge_output_text(
        'before cite[1] entity[organization] :chatgpt-image{alt="x"} '
        "citeturn0search0 【turn3†source】 after"
    )

    for marker in (
        "cite[1]",
        "entity[organization]",
        ":chatgpt-image",
        "turn0search0",
        "turn3",
    ):
        assert marker not in cleaned
    assert "before" in cleaned and "after" in cleaned


def test_real_unescaped_quote_sample_is_plain_text_not_a_json_error() -> None:
    result = parse_q2_proposals_markdown(
        "UNCERTAINTIES\n"
        '- The value "362091310" appears only embedded in the filename '
        '"outputIPandport362091310.txt" and is not explicitly identified '
        "in the capture as a hash\n"
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.uncertainties == [
        'The value "362091310" appears only embedded in the filename '
        '"outputIPandport362091310.txt" and is not explicitly identified in the capture as a hash'
    ]


def test_offline_dirty_wire_replay_counts_retained_and_rejected_proposals() -> None:
    raw = (
        "FACT unknown_category\n- ignored proposal\n"
        "FACT malware\n- ExampleRAT\n"
        "IOC confirmed domain\n"
        '- clearview.ai :: Reconnaissance target :chatgpt-content-reference{index="0"}\n'
        "IOC confirmed domain\n- <redacted>\n"
        "UNCERTAINTIES\n"
        '- The value "362091310" appears in "outputIPandport362091310.txt".\n'
    )
    raw_items = [line for line in raw.splitlines() if line.startswith("- ")]

    parsed = parse_q2_proposals_markdown(raw)

    assert len(raw_items) == 5
    assert parsed.usable, parsed.errors
    assert parsed.value is not None
    parsed_items = (
        len(parsed.value.facts)
        + len(parsed.value.events)
        + len(parsed.value.artifacts)
        + len(parsed.value.rules)
        + len(parsed.value.uncertainties)
    )
    assert parsed_items == 4
    assert parsed.warnings == ["q2_unknown_fact_category"]
    assert len(parsed.dropped_blocks) == 1
    assert parsed.value.artifacts[0].context == "Reconnaissance target"
    assert parsed.value.uncertainties == [
        'The value "362091310" appears in "outputIPandport362091310.txt".'
    ]

    verified = verify_q2_proposals((Q2ProposalSubmission(output=parsed.value, source_ids=("S1",)),))
    assert len(verified.diagnostics) == 3
    assert sum(item.status is ProposalStatus.VERIFIED for item in verified.diagnostics) == 2
    assert len(verified.rejected) == 1
    assert [item.reason_code for item in verified.rejected] == ["redacted_placeholder"]


def test_unexpected_structure_ends_group_and_bullets_do_not_inherit_metadata() -> None:
    result = parse_q2_proposals_markdown(
        """IOC confirmed domain
- kept.example
type: domain
- ignored.example
FACT tools
* ToolName
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [artifact.value for artifact in result.value.artifacts] == ["kept.example"]
    assert [fact.value for fact in result.value.facts] == ["ToolName"]
    assert "q2_unexpected_structure" in result.warnings


def test_rule_without_fence_drops_locally_and_next_group_is_parsed() -> None:
    result = parse_q2_proposals_markdown(
        """RULE yara: Broken
not a fence
IOC confirmed domain
- valid.example
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.rules == []
    assert [artifact.value for artifact in result.value.artifacts] == ["valid.example"]
    assert "rule_without_body_fence" in result.warnings


@pytest.mark.parametrize(
    "text",
    [
        "IOC confirmed domain",
        "FACT malware\n\nIOC confirmed ip",
        "EVENT 2024-03-02",
        "UNCERTAINTIES",
    ],
)
def test_headers_without_accepted_payload_are_not_usable(text: str) -> None:
    result = parse_q2_proposals_markdown(text)

    assert not result.usable
    assert result.value is None
    assert result.errors == ["q2_no_payload"]


def test_model_supplied_uncertainty_is_accepted_payload() -> None:
    result = parse_q2_proposals_markdown("UNCERTAINTIES\n- The source only partially loaded\n")

    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.uncertainties == ["The source only partially loaded"]


def test_empty_is_a_usable_empty_source_output() -> None:
    result = parse_q2_proposals_markdown("  EMPTY\n")

    assert result.usable
    assert result.value == Q2SourceOutput()


def test_unavailable_is_non_usable_with_a_specific_error() -> None:
    result = parse_q2_proposals_markdown("\nUNAVAILABLE\n")

    assert not result.usable
    assert result.value is None
    assert result.errors == ["q2_source_unavailable"]


@pytest.mark.parametrize(
    ("text", "usable", "error"),
    [("eMpTy", True, None), ("uNaVaIlAbLe", False, "q2_source_unavailable")],
)
def test_terminal_responses_are_case_insensitive(
    text: str, usable: bool, error: str | None
) -> None:
    result = parse_q2_proposals_markdown(text)

    assert result.usable is usable
    assert result.errors == ([] if error is None else [error])


@pytest.mark.parametrize("marker", ["EMPTY", "UNAVAILABLE"])
def test_terminal_marker_mixed_with_groups_keeps_recognized_items(marker: str) -> None:
    result = parse_q2_proposals_markdown(
        f"""{marker}
FACT malware
- ExampleRAT
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [fact.value for fact in result.value.facts] == ["ExampleRAT"]


def test_terminal_marker_inside_rule_body_is_not_a_terminal_response() -> None:
    result = parse_q2_proposals_markdown(
        """RULE sigma: Literal
```yaml
title: EMPTY
```
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert result.value.rules[0].body == "title: EMPTY"


def test_terminal_marker_inside_an_unrelated_fence_is_not_mixed() -> None:
    result = parse_q2_proposals_markdown(
        """IOC confirmed domain
- kept.example
```text example
EMPTY
```
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [artifact.value for artifact in result.value.artifacts] == ["kept.example"]


def test_headers_inside_an_unrelated_fence_are_not_parsed() -> None:
    result = parse_q2_proposals_markdown(
        """```markdown
FACT malware
- not-a-proposal
```
"""
    )

    assert not result.usable
    assert result.errors == ["q2_compact_sections_missing"]


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


def test_old_q2_v3_grouped_sections_are_not_supported() -> None:
    result = parse_q2_proposals_markdown(
        """# FACTS
## malware
- ExampleRAT
"""
    )

    assert not result.usable
    assert result.value is None


@pytest.mark.parametrize(
    ("contract_version", "schema_version"),
    [
        (None, Q2_SCHEMA_VERSION),
        ("q2-source-extraction-v3", Q2_SCHEMA_VERSION),
        (Q2_EXTRACTION_CONTRACT_VERSION, None),
        (Q2_EXTRACTION_CONTRACT_VERSION, "3"),
    ],
)
def test_q2_checkpoint_requires_current_versions(
    contract_version: str | None, schema_version: str | None
) -> None:
    payload = {
        "contract_version": contract_version,
        "schema_version": schema_version,
        "facts": [],
        "artifacts": [],
        "rules": [],
        "uncertainties": [],
    }

    with pytest.raises(ValueError, match="Q2 source extraction"):
        q2_source_output_from_json(payload)


def test_source_ids_are_attached_by_verifier_not_required_from_model() -> None:
    output = _parse("IOC confirmed ip\n- 192.0.2.10\n")
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
        """IOC confirmed domain
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
    output = _parse(f"IOC confirmed url\n- {visible}\n")
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
    text = "\n\n".join(
        f"RULE {rule_type}: Example\n```{rule_type}\n{body}\n```"
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
    output = _parse(f"RULE yara: Flat\n```yara\n{body}\n```")

    assert len(output.rules) == 1
    assert output.rules[0].body == body
    assert "\n" not in output.rules[0].body


def test_malformed_rule_alone_is_not_usable() -> None:
    result = parse_q2_proposals_markdown(
        """RULE yara: Broken
```yara
rule Broken {
  condition: true
```
"""
    )

    assert not result.usable
    assert result.value is None
    assert result.errors == ["q2_no_payload"]
    assert "rule_truncated_not_promoted" in result.warnings


def test_valid_ioc_keeps_a_malformed_rule_local() -> None:
    result = parse_q2_proposals_markdown(
        """IOC confirmed domain
- evil.example

RULE yara: Broken
```yara
rule Broken {
  condition: true
```
"""
    )

    assert result.usable, result.errors
    assert result.value is not None
    assert [artifact.value for artifact in result.value.artifacts] == ["evil.example"]
    assert result.value.rules == []
    assert "rule_truncated_not_promoted" in result.warnings


def test_cached_full_projection_keeps_rules_for_ioc_rules() -> None:
    output = Q2SourceOutput(
        rules=[Q2RuleProposal(rule_type="yara", body="rule R { condition: true }", name="R")]
    )

    projected = project_q2_source_output(output, ExtractionProfile.IOC_RULES)
    assert projected.rules == output.rules


def test_full_fact_categories_match_the_canonical_contract() -> None:
    categories = set(get_args(Q2FactProposal.model_fields["category"].annotation))

    assert categories == set(EXTRACTION_FACT_CATEGORIES)
    assert {"products", "sectors", "countries"} <= categories


def test_full_extraction_covers_products_sectors_and_countries() -> None:
    output = _parse(
        "FACT products\n- Acme EDR\nFACT sectors\n- healthcare\nFACT countries\n- France\n"
    )

    assert [(fact.category, fact.value) for fact in output.facts] == [
        ("products", "Acme EDR"),
        ("sectors", "healthcare"),
        ("countries", "France"),
    ]


def test_event_group_carries_a_precise_date_or_temporal_text() -> None:
    output = _parse(
        """EVENT 2024-03-02
- Actor deployed ExampleRAT :: first stage

EVENT early March 2024
- Victim reported the intrusion

EVENT
- Attribution remained unconfirmed
"""
    )

    assert [(event.event_date, event.date_text) for event in output.events] == [
        (date(2024, 3, 2), None),
        (None, "early March 2024"),
        (None, None),
    ]
    assert [event.text for event in output.events] == [
        "Actor deployed ExampleRAT",
        "Victim reported the intrusion",
        "Attribution remained unconfirmed",
    ]
    assert output.events[0].evidence_quote == "first stage"
    assert output.events[0].context == ""


def test_event_headers_never_invent_a_date() -> None:
    output = _parse("EVENT 2024-13-45\n- Broken date stayed textual\n")

    assert output.events[0].event_date is None
    assert output.events[0].date_text == "2024-13-45"


def test_q2_checkpoint_round_trip_preserves_events() -> None:
    output = _parse("EVENT 2024-03-02\n- Actor deployed ExampleRAT\n")

    payload = q2_source_output_to_json(output)

    assert payload["events"][0]["event_date"] == "2024-03-02"
    assert q2_source_output_from_json(payload) == output


def test_ioc_rules_projection_drops_facts_and_events() -> None:
    output = Q2SourceOutput(
        facts=[Q2FactProposal(category="malware", value="ExampleRAT")],
        events=[Q2EventProposal(event_date=date(2024, 1, 2), text="Deployed")],
        artifacts=[
            Q2ArtifactProposal(
                value="evil.example", artifact_type="domain", indicator_status="confirmed_ioc"
            )
        ],
        rules=[Q2RuleProposal(rule_type="yara", body="rule R { condition: true }", name="R")],
        uncertainties=["partial load"],
    )

    projected = project_q2_source_output(output, ExtractionProfile.IOC_RULES)

    assert projected.facts == []
    assert projected.events == []
    assert projected.artifacts == output.artifacts
    assert projected.rules == output.rules
    assert projected.uncertainties == ["partial load"]
    assert project_q2_source_output(output, ExtractionProfile.FULL) is output


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
