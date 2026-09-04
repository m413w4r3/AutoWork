from __future__ import annotations

from cti_app.application.extraction import parse_document
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
)
from cti_app.application.production_source_evidence import (
    SOURCE_EVIDENCE_VERSION,
    source_evidence_document_from_html,
    verify_ioc_rules_output_against_source,
    verify_q2_output_against_source,
)
from cti_app.domain.collection import DetectedMimeType


def _artifact(
    value: str, artifact_type: str, *, context: str = "model context"
) -> Q2ArtifactProposal:
    return Q2ArtifactProposal(
        value=value,
        artifact_type=artifact_type,
        indicator_status="confirmed_ioc",
        context=context,
        evidence_quote="model quote",
    )


def test_version_and_ioc_from_same_source_are_kept_with_value_only() -> None:
    output = Q2SourceOutput(artifacts=[_artifact("evil[.]com", "domain")])

    result = verify_ioc_rules_output_against_source(output, "The domain is evil.com.")

    assert SOURCE_EVIDENCE_VERSION == "6"
    assert result.output.artifacts[0].value == "evil[.]com"
    assert result.output.artifacts[0].context == ""
    assert result.output.artifacts[0].evidence_quote == ""
    assert result.rejections == ()


def test_full_gate_preserves_facts_but_filters_artifacts_and_rules() -> None:
    fact = Q2FactProposal(category="malware", value="ExampleRAT", context="context")
    output = Q2SourceOutput(
        facts=[fact],
        artifacts=[_artifact("missing.example", "domain")],
        rules=[
            Q2RuleProposal(
                rule_type="sigma",
                name="kept",
                body="title: Kept\nlogsource:\n  product: windows",
            )
        ],
    )

    result = verify_q2_output_against_source(
        output,
        "ExampleRAT\ntitle: Kept\nlogsource:\n  product: windows",
    )

    assert result.output.facts == [fact]
    assert result.output.artifacts == []
    assert len(result.output.rules) == 1
    assert result.rejections[0].reason_code == "source_evidence_missing"


def test_html_safe_fallback_keeps_visible_alt_text_but_not_tracking_or_scripts() -> None:
    html = (
        b'<article><img alt="visual-ioc.security-lab.io" '
        b'src="https://tracking.example/pixel?id=visual-ioc.security-lab.io">'
        b'<a href="https://linked.example/linked-ioc.example">linked</a>'
        b"<script>script-ioc.example</script>"
        b'<meta name="description" content="metadata-ioc.example"></article>'
    )
    parsed = parse_document(html, DetectedMimeType.HTML)
    assert "visual-ioc.security-lab.io" not in parsed.text

    document = source_evidence_document_from_html(
        parsed.text,
        html.decode("utf-8"),
    )
    kept = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("visual-ioc.security-lab.io", "domain")]),
        document,
    )
    assert len(kept.output.artifacts) == 1
    assert "tracking.example" not in document.decoded_source_view
    assert "script-ioc.example" not in document.decoded_source_view
    assert "metadata-ioc.example" not in document.decoded_source_view
    assert "linked-ioc.example" not in document.decoded_source_view


def test_html_image_without_text_has_a_distinct_non_text_diagnostic() -> None:
    html = '<img src="https://cdn.example/screenshot.png">'
    document = source_evidence_document_from_html("Screenshot below", html)

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("visual-ioc.security-lab.io", "domain")]),
        document,
    )

    assert result.output.artifacts == []
    assert result.rejections[0].reason_code == "source_evidence_not_text_verifiable"


def test_ioc_present_only_in_another_source_is_rejected() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("other.example", "domain")]),
        "This source contains current.example only.",
    )

    assert result.output.artifacts == []
    assert result.rejections[0].reason_code == "source_evidence_missing"


def test_explicit_defanging_and_transport_escaping_are_equivalent() -> None:
    output = Q2SourceOutput(
        artifacts=[
            _artifact(r"hxxps\://evil[.]com/a", "url"),
            _artifact("User[at]Example[.]COM", "email"),
        ]
    )

    result = verify_ioc_rules_output_against_source(
        output,
        "https://evil.com/a User@example.com",
    )

    assert len(result.output.artifacts) == 2
    assert result.rejections == ()


def test_defanged_url_in_the_middle_of_a_sentence_is_proven() -> None:
    value = "https://evil.com/a"

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact(value, "url")]),
        "Observed hxxps://evil[.]com/a in the report.",
    )

    assert result.output.artifacts[0].value == value
    assert result.rejections == ()


def test_defanged_http_url_in_the_middle_of_a_sentence_is_proven() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("http://evil.com/a", "url")]),
        "Observed hxxp://evil[.]com/a in the report.",
    )

    assert len(result.output.artifacts) == 1
    assert result.rejections == ()


def test_escaped_defanged_url_in_the_middle_of_a_sentence_is_proven() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("https://evil.com/a", "url")]),
        r"See hxxps\://evil[.]com/a in the report.",
    )

    assert len(result.output.artifacts) == 1
    assert result.rejections == ()


def test_defanged_candidate_and_defanged_source_are_equivalent() -> None:
    value = r"hxxps\://evil[.]com/a"

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact(value, "url")]),
        "Observed hxxps://evil[.]com/a in the report.",
    )

    assert result.output.artifacts[0].value == value
    assert result.rejections == ()


def test_defanged_scheme_glued_to_alphanumeric_token_is_not_transformed() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("https://evil.example", "url")]),
        "The value foohxxps://evil.example is not a URL token.",
    )

    assert result.output.artifacts == []
    assert result.rejections[0].reason_code == "source_evidence_missing"


def test_nbsp_and_narrow_nbsp_are_spaces_only_in_the_comparison_view() -> None:
    value = "dropper name.exe"
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact(value, "filename")]),
        "The files were dropper\u00a0name.exe and dropper\u202fname.exe.",
    )

    assert result.output.artifacts[0].value == value
    assert result.rejections == ()


def test_hash_comparison_is_case_insensitive() -> None:
    value = "a" * 64
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact(value, "hash")]),
        "SHA256: " + value.upper(),
    )

    assert len(result.output.artifacts) == 1


def test_domain_inside_a_longer_domain_is_not_proof() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("evil.com", "domain")]),
        "The observed host is foo.evil.com.",
    )

    assert result.output.artifacts == []
    assert result.rejections[0].reason_code == "source_evidence_missing"


def test_wrapped_domain_is_kept_with_unwrap_warning() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("exemple.com", "domain")]),
        "Observed exemp\nle.com in the report.",
    )

    assert len(result.output.artifacts) == 1
    assert "artifact_proven_after_unwrap" in result.warnings
    assert result.rejections == ()


def test_artifact_absent_from_both_source_views_is_rejected() -> None:
    document = source_evidence_document_from_html(
        "present.example",
        "<p>decoded.example</p>",
    )

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("missing.example", "domain")]),
        document,
    )

    assert result.output.artifacts == []
    assert result.rejections[0].reason_code == "source_evidence_missing"


def test_ipv4_is_proven_without_ip_reformatting() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("192[.]0[.]2[.]10", "ip")]),
        "Connection to 192.0.2.10:443 was blocked.",
    )

    assert len(result.output.artifacts) == 1


def test_ipv6_requires_the_same_literal_representation() -> None:
    exact = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("2001:DB8::1", "ip")]),
        "The address 2001:DB8::1 was observed.",
    )
    different_spelling = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("2001:DB8::1", "ip")]),
        "The address 2001:0db8:0:0:0:0:0:1 was observed.",
    )

    assert len(exact.output.artifacts) == 1
    assert different_spelling.output.artifacts == []


def test_url_path_and_query_case_are_preserved() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("https://example.com/Case?Key=Value", "url")]),
        "https://example.com/case?Key=Value",
    )

    assert result.output.artifacts == []
    assert result.rejections[0].reason_code == "source_evidence_missing"


def test_email_local_part_is_case_sensitive_but_domain_is_not() -> None:
    accepted = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("Alice@example.com", "email")]),
        "Contact Alice@EXAMPLE.COM.",
    )
    rejected = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("Alice@example.com", "email")]),
        "Contact alice@EXAMPLE.COM.",
    )

    assert len(accepted.output.artifacts) == 1
    assert rejected.output.artifacts == []


def test_filename_and_filepath_are_literal() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(
            artifacts=[
                _artifact("payload.exe", "filename"),
                _artifact(r"/tmp/payload.exe", "filepath"),
            ]
        ),
        r"Dropped /tmp/payload.exe and retained payload.exe.",
    )

    assert len(result.output.artifacts) == 2


def test_exact_rule_is_kept_and_narrative_fields_are_removed() -> None:
    rule = Q2RuleProposal(
        rule_type="sigma",
        name="Example",
        body="title: Example\nlogsource:\n  product: windows",
        context="model context",
        evidence_quote="model quote",
    )

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(rules=[rule]),
        "Preamble\r\ntitle: Example\r\nlogsource:\r\n  product: windows\r\nend",
    )

    assert result.output.rules[0].body == rule.body
    assert result.output.rules[0].context == ""
    assert result.output.rules[0].evidence_quote == ""
    assert result.rejections == ()


def test_rule_with_changed_whitespace_is_kept() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(
            rules=[
                Q2RuleProposal(
                    rule_type="yara",
                    body="rule R {\n    condition: true\n}",
                )
            ]
        ),
        "rule R {\n\tcondition: true\n}",
    )

    assert len(result.output.rules) == 1
    assert result.rejections == ()


def test_rule_present_only_in_another_source_is_rejected() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(
            rules=[Q2RuleProposal(rule_type="yara", body="rule Other { condition: true }")]
        ),
        "rule Current { condition: true }",
    )

    assert result.output.rules == []
    assert result.rejections[0].reason_code == "source_rule_evidence_missing"


def test_facts_are_always_removed_with_a_warning() -> None:
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(
            facts=[
                Q2FactProposal(
                    category="malware",
                    value="ExampleRAT",
                    context="narrative",
                    evidence_quote="quote",
                )
            ]
        ),
        "ExampleRAT is mentioned.",
    )

    assert result.output.facts == []
    assert "fact_not_allowed" in result.warnings


def test_one_invalid_item_does_not_remove_other_items() -> None:
    valid = _artifact("good.example", "domain")
    invalid = _artifact("missing.example", "domain")

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[valid, invalid]),
        "Only good.example is present.",
    )

    assert [artifact.value for artifact in result.output.artifacts] == ["good.example"]
    assert len(result.rejections) == 1
    assert result.rejections[0].reason_code == "source_evidence_missing"
