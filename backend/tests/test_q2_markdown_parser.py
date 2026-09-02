from __future__ import annotations

import hashlib

import pytest

from cti_app.application.production_artifact_verification import (
    ProposalStatus,
    Q2ProposalSubmission,
    verify_q2_proposals,
)
from cti_app.application.production_parsers import (
    Q2_MARKDOWN_PARSER_VERSION,
    DetectionRule,
    Q2ArtifactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
    project_q2_source_output,
    q2_source_output_from_json,
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
        "RedKitten",
        source_id="S1",
        source_title="Source",
        source_url="https://source.example/report",
        profile=ExtractionProfile.FULL,
    )

    assert "FACT <category>" in prompt
    assert "IOC <confirmed|contextual> <type>" in prompt
    assert "RULE <yara|sigma|suricata|snort>" in prompt
    assert "# FACTS" not in prompt
    assert "# IOCS" not in prompt
    assert "# RULES" not in prompt
    assert "indicator-status" not in prompt
    assert "<literal body>\n```\n\nUNCERTAINTIES" in prompt
    assert " ".join(prompt.split()).count("Perform an exhaustive subject-relevant IOC pass:") == 1
    assert "Perform an exhaustive IOC pass:" not in " ".join(prompt.split())
    assert prompt.count("**Subject**: RedKitten") == 1
    assert "https://source.example/report" in prompt
    assert "<ARCHIVED_SOURCE>" not in prompt
    assert "images/screenshots" in prompt
    one_line = " ".join(prompt.split())
    assert "Do not repeat the input source URL merely as provenance." in one_line
    assert (
        "This restriction does not apply to IOC values: extract URL indicators normally "
        "when they are actually published by this source."
    ) in one_line
    assert "Emit no source id, URL, provenance" not in one_line
    assert "Never repeat its URL" not in one_line
    assert "url, email, md5, sha1, sha256, sha512" in one_line
    assert EXTRACTION_PROMPT_VERSION == "16"
    assert Q2_MARKDOWN_PARSER_VERSION == "q2-markdown-v5"


def test_ioc_rules_prompt_forbids_facts_and_narrative_extraction() -> None:
    prompt = ProductionPromptTemplates.get_extraction_prompt(
        "RedKitten",
        source_title="Source",
        source_url="https://source.example/report",
        profile=ExtractionProfile.IOC_RULES,
    )

    assert "IOC <confirmed|contextual> <type>" in prompt
    assert "RULE <yara|sigma|suricata|snort>" in prompt
    assert "FACT <category>" not in prompt
    assert "# IOCS" not in prompt
    assert "# RULES" not in prompt
    assert "do not extract FACTS" in prompt
    assert "narrative" in prompt
    assert "<literal body>\n```\n\nUNCERTAINTIES" in prompt
    assert " ".join(prompt.split()).count("Perform an exhaustive subject-relevant IOC pass:") == 1
    assert "Perform an exhaustive IOC pass:" not in " ".join(prompt.split())
    assert prompt.count("**Subject**: RedKitten") == 1
    assert "https://source.example/report" in prompt
    assert "<ARCHIVED_SOURCE>" not in prompt
    assert "images/screenshots" in prompt
    one_line = " ".join(prompt.split())
    assert "Do not repeat the input source URL merely as provenance." in one_line
    assert (
        "This restriction does not apply to IOC values: extract URL indicators normally "
        "when they are actually published by this source."
    ) in one_line
    assert "Emit no source id, URL, provenance" not in one_line
    assert "Never repeat its URL" not in one_line
    assert "url, email, md5, sha1, sha256, sha512" in one_line
    assert IOC_RULES_PROMPT_VERSION == "9"
    # The transport format stays a bare value line: filtering happens during
    # extraction, not through a new annotated wire format.
    assert "IOC <confirmed|contextual> <type>\n- <value>" in prompt
    assert " :: " not in prompt.split("Rules:")[0]


@pytest.mark.parametrize("profile", [ExtractionProfile.FULL, ExtractionProfile.IOC_RULES])
def test_q2_prompt_states_the_multi_actor_relevance_contract(profile: ExtractionProfile) -> None:
    """The prompt, not a post-Q2 pass, carries the subject-selection contract."""
    prompt = ProductionPromptTemplates.get_extraction_prompt(
        "RedKitten",
        source_title="Multi-actor report",
        source_url="https://source.example/report",
        profile=profile,
    )
    one_line = " ".join(prompt.split())

    assert "**Subject**: RedKitten" in prompt
    # relevant publication != every IOC is relevant
    assert (
        "Relevance of the publication does not imply relevance of every indicator"
        " contained in it." in one_line
    )
    assert "Do NOT emit an IOC merely because it appears elsewhere in the same" in one_line
    # other actor / campaign / operation / multi-actor table row -> excluded
    for excluded in (
        "another actor;",
        "another campaign or operation;",
        "another unrelated malware family;",
        "another row/group of a multi-actor IOC table.",
    ):
        assert excluded in one_line
    # ambiguous attribution -> not confirmed
    assert (
        "When an IOC's relationship to the subject is ambiguous, do not emit it as"
        " confirmed." in one_line
    )
    # shared generic service -> not an IOC on its own, but a discriminating
    # subject-specific artifact stays allowed
    assert "Shared legitimate infrastructure is not a useful IOC by itself." in one_line
    assert "must not be emitted solely because the subject used the service" in one_line
    assert (
        "A campaign-specific repository, account, URL, subdomain or other discriminating"
        " artifact may be emitted when explicitly supported." in one_line
    )
    # detection rules follow the same boundary
    assert "A detection rule must also be relevant to the requested Subject." in one_line
    # exhaustive, but only after filtering
    assert (
        "Exhaustiveness applies after relevance filtering: find every subject-relevant"
        " IOC, not every IOC in the publication." in one_line
    )
    assert (
        "Never increase coverage by importing indicators belonging to other activities"
        " mentioned in the source." in one_line
    )
    # existing safety rules are not weakened
    for preserved in ("placeholder", "masked", "truncated", "REDACTED", "FUZZ", "IPv6"):
        assert preserved in prompt
    assert "UNAVAILABLE" in prompt and "EMPTY" in prompt


def test_full_prompt_allows_only_clarifying_facts_about_another_activity() -> None:
    prompt = ProductionPromptTemplates.get_extraction_prompt(
        "RedKitten",
        source_title="Multi-actor report",
        source_url="https://source.example/report",
        profile=ExtractionProfile.FULL,
    )
    one_line = " ".join(prompt.split())

    assert (
        "Facts about another activity may be emitted only when they materially clarify the"
        " requested subject's attribution, malware sharing, infrastructure sharing,"
        " technical relationship or uncertainty." in one_line
    )
    assert "Do not extract unrelated parallel activity as standalone subject facts." in one_line


def test_ioc_group_parses_100_confirmed_iocs() -> None:
    values = "\n".join(f"- 192.0.2.{index + 1}" for index in range(100))
    output = _parse(f"IOC confirmed ip\n{values}\n")

    assert len(output.artifacts) == 100
    assert all(item.indicator_status == "confirmed_ioc" for item in output.artifacts)
    assert all(item.context == "" and item.evidence_quote == "" for item in output.artifacts)


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

    assert [(fact.category, fact.value, fact.context) for fact in output.facts] == [
        ("malware", "ExampleRAT", "payload family"),
        ("ttps", "T1059", "shell"),
    ]
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
def test_terminal_marker_mixed_with_groups_is_rejected(marker: str) -> None:
    result = parse_q2_proposals_markdown(
        f"""{marker}
FACT malware
- ExampleRAT
"""
    )

    assert not result.usable
    assert result.errors == ["q2_terminal_marker_mixed"]


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
        (None, "3"),
        ("q2-source-extraction-v2", "3"),
        ("q2-source-extraction-v3", None),
        ("q2-source-extraction-v3", "2"),
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
