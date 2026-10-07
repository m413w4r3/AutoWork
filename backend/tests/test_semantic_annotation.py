from __future__ import annotations

from types import SimpleNamespace

from cti_app.application.semantic_annotation import (
    EnglishTermDetector,
    SemanticAnnotator,
    semantic_entities_from_extraction,
)
from cti_app.domain.production import ProductionEvidenceBasis
from cti_app.domain.semantic_annotation import SemanticRole


def test_typed_entities_win_over_english_terms_and_preserve_every_character() -> None:
    text = (
        "APT Étoile, a campaign report names Cobalt Strike and Cobalt Strike; "
        "lateral movement followed. A loader is mentioned, but downloaders are not."
    )
    entities = (
        (SemanticRole.ACTOR, "APT Étoile"),
        (SemanticRole.TOOL, "Cobalt Strike"),
        (SemanticRole.TECHNICAL, "lateral movement"),
        (SemanticRole.ACTOR, "Étoile"),
    )
    annotated = SemanticAnnotator(
        EnglishTermDetector(("cobalt strike", "lateral movement", "loader"))
    ).annotate_paragraph(anchor="lead:0001", text=text, entities=entities)

    assert "".join(span.text for span in annotated.spans) == text
    actor_spans = [span.text for span in annotated.spans if span.role is SemanticRole.ACTOR]
    assert actor_spans == ["APT Étoile"]
    assert sum(span.role is SemanticRole.TOOL for span in annotated.spans) == 2
    assert any(
        span.text == "lateral movement" and span.role is SemanticRole.TECHNICAL
        for span in annotated.spans
    )
    assert any(
        span.text == "loader" and span.role is SemanticRole.ENGLISH_TERM for span in annotated.spans
    )
    assert not any(
        "downloaders" in span.text for span in annotated.spans if span.role is not SemanticRole.TEXT
    )
    assert any(
        span.text.startswith(",") and span.role is SemanticRole.TEXT for span in annotated.spans
    )


def test_accents_repeated_terms_and_inline_proof_have_stable_priority() -> None:
    text = "APT Étoile and apt étoile [S7]."
    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="section:0:paragraph:0001",
        text=text,
        entities=((SemanticRole.ACTOR, "APT Étoile"), (SemanticRole.TOOL, "[S7]")),
    )

    assert "".join(span.text for span in annotated.spans) == text
    assert [span.text for span in annotated.spans if span.role is SemanticRole.ACTOR] == [
        "APT Étoile",
        "apt étoile",
    ]
    assert [span.role for span in annotated.spans if span.text == "[S7]"] == [SemanticRole.SOURCE]
    assert annotated.spans[-1].text == "."
    assert annotated.spans[-1].role is SemanticRole.TEXT


def test_typed_extraction_entities_are_used_without_model_proposals() -> None:
    extraction = SimpleNamespace(
        sources=(
            SimpleNamespace(
                facts=(
                    SimpleNamespace(
                        category="actors",
                        value="APT Étoile",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                    ),
                ),
                indicators=(),
            ),
        )
    )
    entities = semantic_entities_from_extraction(extraction)  # type: ignore[arg-type]
    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="lead:0001",
        text="The operator APT Étoile was observed.",
        entities=entities,
    )

    assert any(
        span.text == "APT Étoile" and span.role is SemanticRole.ACTOR for span in annotated.spans
    )


def test_source_verified_ports_and_parameters_get_monospace_literal_roles() -> None:
    extraction = SimpleNamespace(
        sources=(
            SimpleNamespace(
                facts=(
                    SimpleNamespace(
                        category="other_technical",
                        value="TCP port 443 and retry_count=4",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                    ),
                ),
                indicators=(),
            ),
        )
    )
    entities = semantic_entities_from_extraction(extraction)  # type: ignore[arg-type]
    text = "TCP port 443 and retry_count=4 were documented."
    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="lead:0001", text=text, entities=entities
    )

    assert "".join(span.text for span in annotated.spans) == text
    assert any(
        span.text == "443" and span.role is SemanticRole.TECHNICAL_LITERAL
        for span in annotated.spans
    )
    assert any(
        span.text == "retry_count=4" and span.role is SemanticRole.TECHNICAL_LITERAL
        for span in annotated.spans
    )


def test_attached_angle_placeholders_remain_in_full_technical_literal_spans() -> None:
    text = (
        r"NetSync_<username> C:\Users\<user>\AppData "
        r"/home/<user>/.config %USERPROFILE%\<name>.dll"
    )
    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="lead:0001", text=text, entities=()
    )

    assert annotated.text == text
    assert "".join(span.text for span in annotated.spans) == text
    literals = [
        span.text for span in annotated.spans if span.role is SemanticRole.TECHNICAL_LITERAL
    ]
    assert literals == [
        "NetSync_<username>",
        r"C:\Users\<user>\AppData",
        r"/home/<user>/.config",
        r"%USERPROFILE%\<name>.dll",
    ]


def test_standalone_placeholders_and_wildcard_domains_are_technical_literals() -> None:
    text = "Commande <deno_path> vers *.cloudfront.net et fichier *.exe."
    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="lead:0001", text=text, entities=()
    )

    literals = [
        span.text for span in annotated.spans if span.role is SemanticRole.TECHNICAL_LITERAL
    ]
    assert "<deno_path>" in literals
    assert "*.cloudfront.net" in literals
    assert "*.exe" in literals


def test_model_segment_resolves_every_exact_occurrence_with_punctuation_local() -> None:
    from cti_app.domain.semantic_annotation import SemanticAnnotationProposalV1

    text = "Cobalt Strike, then Cobalt Strike."
    proposal = SemanticAnnotationProposalV1(
        paragraph_anchor="lead:0001",
        role=SemanticRole.TOOL,
        text="Cobalt Strike",
    )
    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="lead:0001",
        text=text,
        entities=(),
        proposals=(proposal,),
    )

    assert "".join(span.text for span in annotated.spans) == text
    assert [span.text for span in annotated.spans if span.role is SemanticRole.TOOL] == [
        "Cobalt Strike",
        "Cobalt Strike",
    ]
    assert annotated.spans[1].text.endswith(", then ")


def test_command_path_ioc_protocol_literal_and_technical_roles_are_distinct() -> None:
    text = "curl /tmp/payload 203.0.113.8 User-Agent C2 profile"
    entities = (
        (SemanticRole.COMMAND, "curl"),
        (SemanticRole.PATH, "/tmp/payload"),
        (SemanticRole.IOC, "203.0.113.8"),
        (SemanticRole.PROTOCOL_FIELD, "User-Agent"),
        (SemanticRole.TECHNICAL, "C2 profile"),
    )
    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="lead:0001", text=text, entities=entities
    )

    assert "".join(span.text for span in annotated.spans) == text
    for literal, role in (
        ("curl", SemanticRole.COMMAND),
        ("/tmp/payload", SemanticRole.PATH),
        ("203.0.113.8", SemanticRole.IOC),
        ("User-Agent", SemanticRole.PROTOCOL_FIELD),
        ("C2 profile", SemanticRole.TECHNICAL),
    ):
        assert any(span.text == literal and span.role is role for span in annotated.spans)


def test_extended_english_lexicon_contains_conservative_cti_phrases() -> None:
    spans = EnglishTermDetector().spans("Le lateral movement suit initial access.")
    assert len(spans) == 2
