from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import cast
from uuid import uuid4

import pytest

from cti_app.application import production_editorial_enrichment as enrichment_module
from cti_app.application.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_GENERATOR_VERSION,
    ProductionEditorialEnrichmentService,
    build_editorial_enrichment_evidence_pack,
    build_editorial_enrichment_model_request,
    build_semantic_annotation_model_request,
    compute_editorial_enrichment_input_hash,
    parse_semantic_annotation_wire,
    semantic_annotation_anchor_texts,
    semantic_annotation_input_hash,
)
from cti_app.application.production_prompts import (
    EDITORIAL_ENRICHMENT_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
    SEMANTIC_ANNOTATION_CONTRACT_VERSION,
    SEMANTIC_ANNOTATION_PROMPT_VERSION,
    SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION,
)
from cti_app.application.production_synthesis import (
    SynthesisAccessPolicyV1,
    SynthesisAccessSourceV1,
)
from cti_app.domain.production import TLP, ProductionRun
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ANNOTATION_POLICY_VERSION,
    SemanticAnnotationProposalV1,
    SemanticRole,
)
from tests.test_production_editorial_enrichment_application import (
    _SUBJECT_ID,
    _extraction,
    _snapshot,
    _synthesis,
)
from tests.test_publication_builder_v4 import (
    _canonical_inputs,
    _diagram,
    _enrichment_with,
    _table,
)


def _annotate(text: str):
    from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator

    return SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="lead:0001", text=text, entities=()
    )


@pytest.mark.parametrize(
    ("text", "literal", "role"),
    (
        ("CVE-2024-12345 was published.", "CVE-2024-12345", SemanticRole.TECHNICAL_LITERAL),
        (
            "MITRE ATT&CK T1102.002.",
            "MITRE ATT&CK T1102.002",
            SemanticRole.TECHNICAL_LITERAL,
        ),
        ("Observed 203.0.113.8.", "203.0.113.8", SemanticRole.IOC),
        ("Observed 2001:db8::1.", "2001:db8::1", SemanticRole.IOC),
        (
            "MD5 0123456789abcdef0123456789abcdef.",
            "0123456789abcdef0123456789abcdef",
            SemanticRole.IOC,
        ),
        (
            "SHA-1 0123456789abcdef0123456789abcdef01234567.",
            "0123456789abcdef0123456789abcdef01234567",
            SemanticRole.IOC,
        ),
        (
            "SHA-256 0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef.",
            "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
            SemanticRole.IOC,
        ),
        ("port: 443 is used.", "443", SemanticRole.TECHNICAL_LITERAL),
        ("TCP/8443 is used.", "8443", SemanticRole.TECHNICAL_LITERAL),
        ("53/udp is used.", "53", SemanticRole.TECHNICAL_LITERAL),
        (
            r"C:\Users\Public\payload.exe was launched.",
            r"C:\Users\Public\payload.exe",
            SemanticRole.PATH,
        ),
        ("/tmp/dropper.sh was created.", "/tmp/dropper.sh", SemanticRole.PATH),
        (
            "The operator ran `powershell -nop -w hidden`.",
            "`powershell -nop -w hidden`",
            SemanticRole.COMMAND,
        ),
        ("OP_RETURN and JSON-RPC were observed.", "OP_RETURN", SemanticRole.TECHNICAL_LITERAL),
        ("OP_RETURN and JSON-RPC were observed.", "JSON-RPC", SemanticRole.TECHNICAL_LITERAL),
    ),
)
def test_technical_literal_pass_recognizes_conservative_patterns(
    text: str, literal: str, role: SemanticRole
) -> None:
    annotated = _annotate(text)

    assert "".join(span.text for span in annotated.spans).encode() == text.encode()
    assert any(span.text == literal and span.role is role for span in annotated.spans)


@pytest.mark.parametrize(
    "text",
    (
        "T2 2026",
        "mi-2025",
        "5.2x",
        "C2",
        "BDD",
        "La maison reste calme.",
        "Le 2026-10-05 est une date.",
        "999.2.1.1 is not a valid IPv4 address.",
        "port 99999 is outside the port range.",
        "`ordinary words` are not a command.",
    ),
)
def test_technical_literal_pass_rejects_ordinary_text_and_invalid_values(text: str) -> None:
    annotated = _annotate(text)

    assert not any(span.role is not SemanticRole.TEXT for span in annotated.spans)


def test_short_source_verified_fact_values_are_entity_terms_but_sentences_are_not() -> None:
    from types import SimpleNamespace

    from cti_app.application.semantic_annotation import semantic_entities_from_extraction
    from cti_app.domain.production import ProductionEvidenceBasis

    extraction = SimpleNamespace(
        sources=(
            SimpleNamespace(
                facts=(
                    SimpleNamespace(
                        category="malware",
                        value="Necurs est cité comme botnet ayant utilisé Namecoin.",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                    ),
                    SimpleNamespace(
                        category="malware",
                        value="Necurs",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                    ),
                    SimpleNamespace(
                        category="malware",
                        value="Necurs est actif",
                        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                    ),
                ),
                indicators=(),
            ),
        )
    )

    assert semantic_entities_from_extraction(extraction) == ((SemanticRole.MALWARE, "Necurs"),)  # type: ignore[arg-type]


def test_extraction_indicators_remain_semantic_entities() -> None:
    from types import SimpleNamespace

    from cti_app.application.semantic_annotation import semantic_entities_from_extraction
    from cti_app.domain.production_extraction import ExtractionIndicatorStatus
    from cti_app.domain.publication import ArtifactType

    extraction = SimpleNamespace(
        sources=(
            SimpleNamespace(
                facts=(),
                indicators=(
                    SimpleNamespace(
                        artifact_type=ArtifactType.FILEPATH,
                        indicator_status=ExtractionIndicatorStatus.CONTEXTUAL,
                        value="C:\\Windows\\Temp\\dropper.exe",
                    ),
                    SimpleNamespace(
                        artifact_type=ArtifactType.URL,
                        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
                        value="hxxps://evil[.]example/path",
                    ),
                ),
            ),
        )
    )

    assert semantic_entities_from_extraction(extraction) == (
        (SemanticRole.PATH, "C:\\Windows\\Temp\\dropper.exe"),
        (SemanticRole.IOC, "hxxps://evil[.]example/path"),
    )  # type: ignore[arg-type]


def test_document_lexicon_ignores_provenance_anchor_and_resolves_conflicting_roles() -> None:
    text = "Necurs used JSON-RPC; Necurs used JSON-RPC."
    proposals = (
        SemanticAnnotationProposalV1("lead:0001", SemanticRole.MALWARE, "Necurs"),
        SemanticAnnotationProposalV1("timeline:0001", SemanticRole.TECHNICAL, "JSON-RPC"),
        SemanticAnnotationProposalV1(
            "section:0:paragraph:0001", SemanticRole.TECHNICAL_LITERAL, "JSON-RPC"
        ),
    )
    from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator

    annotated = SemanticAnnotator(EnglishTermDetector(())).annotate_paragraph(
        anchor="timeline:0009", text=text, entities=(), proposals=proposals
    )

    assert "".join(span.text for span in annotated.spans) == text
    assert sum(span.role is SemanticRole.MALWARE for span in annotated.spans) == 2
    assert sum(span.role is SemanticRole.TECHNICAL_LITERAL for span in annotated.spans) == 2
    assert not any(span.role is SemanticRole.TECHNICAL for span in annotated.spans)


def _publication_fixture() -> dict[str, object]:
    path = Path(__file__).parent / "fixtures" / "semantic_annotation_l13.json"
    return cast(dict[str, object], json.loads(path.read_text(encoding="utf-8")))


def test_l13_real_run_fixture_has_full_document_and_timeline_coverage() -> None:
    from cti_app.application.semantic_annotation import EnglishTermDetector, SemanticAnnotator

    fixture = _publication_fixture()
    anchors: dict[str, str] = {}
    lead = fixture["lead"]
    sections = fixture["sections"]
    timeline = fixture["timeline"]
    assert isinstance(lead, list) and isinstance(sections, list) and isinstance(timeline, list)
    anchors.update({f"lead:{index:04d}": text for index, text in enumerate(lead, start=1)})
    for section_index, section_paragraphs in enumerate(sections):
        assert isinstance(section_paragraphs, list)
        anchors.update(
            {
                f"section:{section_index}:paragraph:{paragraph_index:04d}": text
                for paragraph_index, text in enumerate(section_paragraphs, start=1)
            }
        )
    anchors.update({f"timeline:{index:04d}": text for index, text in enumerate(timeline, start=1)})
    terms = (
        ("Chainalysis", SemanticRole.ACTOR),
        ("Ministry of Intelligence iranien", SemanticRole.ACTOR),
        ("Ministry of Intelligence", SemanticRole.ACTOR),
        ("Necurs", SemanticRole.MALWARE),
        ("Glupteba", SemanticRole.MALWARE),
        ("Namecoin", SemanticRole.TECHNICAL),
        ("Bitcoin", SemanticRole.TECHNICAL),
        ("Ethereum", SemanticRole.TECHNICAL),
        ("EtherHiding", SemanticRole.TECHNICAL),
        ("OP_RETURN", SemanticRole.PROTOCOL_FIELD),
        ("JSON-RPC", SemanticRole.TECHNICAL_LITERAL),
        ("infostealers", SemanticRole.TECHNICAL),
        ("remote access trojans", SemanticRole.TECHNICAL),
        ("MITRE ATT&CK T1102.002", SemanticRole.TECHNICAL_LITERAL),
        ("blockchains", SemanticRole.TECHNICAL),
        ("BDD", SemanticRole.TECHNICAL),
        ("LLM", SemanticRole.TECHNICAL),
        ("Satoshis", SemanticRole.TECHNICAL),
    )
    wire: list[str] = []
    for index, (term, role) in enumerate(terms, start=1):
        anchor = next(key for key, text in anchors.items() if term in text)
        wire.extend(
            (
                f"TERM A{index:03d}",
                f"TERM: {term}",
                f"ROLE: {role.value}",
                f"PARAGRAPH_ANCHOR: {anchor}",
                "END TERM",
            )
        )
    parsed = parse_semantic_annotation_wire("\n".join(wire), anchors)
    assert parsed.error_code is None and not parsed.rejections
    assert len(parsed.proposals) == len(terms)

    annotator = SemanticAnnotator(EnglishTermDetector(()))
    annotated = {
        anchor: annotator.annotate_paragraph(
            anchor=anchor,
            text=text,
            entities=(),
            proposals=parsed.proposals,
        )
        for anchor, text in anchors.items()
    }
    for anchor, paragraph in annotated.items():
        expected = anchors[anchor]
        assert "".join(span.text for span in paragraph.spans).encode("utf-8") == expected.encode(
            "utf-8"
        )
    timeline_results = [annotated[f"timeline:{index:04d}"] for index in range(1, len(timeline) + 1)]
    assert len(timeline_results) == 9
    assert all(
        any(span.role is not SemanticRole.TEXT for span in item.spans) for item in timeline_results
    )
    assert (
        sum(
            span.text == "OP_RETURN" and span.role is SemanticRole.PROTOCOL_FIELD
            for paragraph in annotated.values()
            for span in paragraph.spans
        )
        >= 2
    )
    for term, _role in terms:
        for anchor, text in anchors.items():
            start = 0
            while True:
                start = text.find(term, start)
                if start < 0:
                    break
                end = start + len(term)
                spans = annotated[anchor].spans
                offset = 0
                covered = False
                for span in spans:
                    next_offset = offset + len(span.text)
                    if (
                        offset <= start
                        and next_offset >= end
                        and span.role is not SemanticRole.TEXT
                    ):
                        covered = True
                        break
                    offset = next_offset
                assert covered, f"{term!r} is unstyled in {anchor}"
                start += 1


def test_annotation_wire_rejects_items_independently_and_keeps_valid_terms() -> None:
    parsed = parse_semantic_annotation_wire(
        """TERM A001
TERM: Chainalysis
ROLE: organization
PARAGRAPH_ANCHOR: lead:0001
END TERM

TERM A002
TERM: Necurs
ROLE: malware
PARAGRAPH_ANCHOR: timeline:0001
END TERM
""",
        {"lead:0001": "Chainalysis wrote the report.", "timeline:0001": "Necurs stored domains."},
    )

    assert parsed.error_code is None
    assert [(item.text, item.role) for item in parsed.proposals] == [
        ("Necurs", SemanticRole.MALWARE)
    ]
    assert parsed.rejections == (("A001", "semantic_annotation_role_unknown"),)


def test_annotation_anchor_inventory_matches_publication_text_fields_and_captions() -> None:
    from cti_app.application.publication_builder import build_publication_document_v4
    from cti_app.domain.production_synthesis import extraction_evidence_refs_v1
    from cti_app.domain.publication_document import publication_document_text_anchors

    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    enrichment = _enrichment_with(
        extraction=extraction,
        synthesis=synthesis,
        tables=(_table(evidence_ref),),
        diagrams=(_diagram(evidence_ref),),
    )
    publication = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )

    semantic_anchors = semantic_annotation_anchor_texts(synthesis, enrichment)
    publication_anchors = publication_document_text_anchors(publication)
    assert "title" not in semantic_anchors
    assert semantic_anchors == {
        anchor: text for anchor, text in publication_anchors.items() if anchor != "title"
    }
    assert "diagram:infection_chain:title" in semantic_anchors


def _access_policy(snapshot, extraction) -> SynthesisAccessPolicyV1:
    return SynthesisAccessPolicyV1(
        subject_tlp=snapshot.subject_tlp,
        effective_tlp=snapshot.subject_tlp,
        external_llm_allowed=True,
        do_not_submit=False,
        sources=(
            SynthesisAccessSourceV1(
                source_document_id=extraction.sources[0].source_document_id,
                tlp=TLP.CLEAR,
                external_llm_allowed=True,
                do_not_submit=False,
            ),
        ),
    )


def test_annotation_request_is_separate_from_the_table_diagram_request_and_versioned() -> None:
    run = ProductionRun(subject_id=_SUBJECT_ID, edition_id=uuid4())
    snapshot = _snapshot(run_id=run.id)
    extraction = _extraction(input_hash=snapshot.input_hash)
    synthesis = _synthesis(extraction)
    policy = _access_policy(snapshot, extraction)
    evidence_pack = build_editorial_enrichment_evidence_pack(snapshot, extraction, synthesis)
    table_diagram_request = build_editorial_enrichment_model_request(
        run, snapshot, extraction, synthesis, evidence_pack, policy
    )
    anchors = semantic_annotation_anchor_texts(synthesis)
    annotation_request = build_semantic_annotation_model_request(
        run,
        snapshot,
        policy,
        anchors,
        enrichment_input_hash=compute_editorial_enrichment_input_hash(
            extraction=extraction,
            synthesis=synthesis,
            evidence_pack_hash="1" * 64,
            access_policy_hash="2" * 64,
        ),
    )

    assert table_diagram_request.prompt_template_id == "production-editorial-enrichment"
    assert "ANNOTATION A001" not in table_diagram_request.text
    assert annotation_request.prompt_template_id == "production-semantic-annotation"
    assert annotation_request.prompt_template_id != table_diagram_request.prompt_template_id
    assert annotation_request.routing_hint is table_diagram_request.routing_hint
    assert "FINAL PUBLICATION TEXT ANCHORS" in annotation_request.text
    assert annotation_request.prompt_template_version == SEMANTIC_ANNOTATION_PROMPT_VERSION
    assert (
        SEMANTIC_ANNOTATION_CONTRACT_VERSION == "semantic-annotation-term-role-blocks-v2-no-title"
    )
    assert SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION == "semantic-annotation-wire-v3-no-title"
    assert EDITORIAL_ENRICHMENT_PROMPT_VERSION.endswith("source-figure-selection")
    assert EDITORIAL_ENRICHMENT_PROPOSAL_CONTRACT_VERSION.endswith("source-figure-captions")
    assert EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION.endswith("source-figure-captions")
    assert "@@ANCHOR title@@" not in annotation_request.text
    assert "@@ANCHOR section:0:paragraph:0001@@" in annotation_request.text
    assert "@@ANCHOR diagram:" not in annotation_request.text
    assert EDITORIAL_ENRICHMENT_GENERATOR_VERSION.endswith("dedicated-annotations")
    assert SEMANTIC_ANNOTATION_POLICY_VERSION.endswith("document-lexicon")


def test_annotation_call_failure_returns_warning_without_aborting_enrichment() -> None:
    class FailingGateway:
        async def get_run(self, _run_id):
            return None

        async def draft(self, _request):
            raise RuntimeError("fake model failure")

    run = ProductionRun(subject_id=_SUBJECT_ID, edition_id=uuid4())
    snapshot = _snapshot(run_id=run.id)
    extraction = _extraction(input_hash=snapshot.input_hash)
    policy = _access_policy(snapshot, extraction)
    service = object.__new__(ProductionEditorialEnrichmentService)
    service._model_gateway = FailingGateway()
    input_hash = "f" * 64

    proposals, calls, details = asyncio.run(
        service._propose_semantic_annotations(
            run=run,
            snapshot=snapshot,
            access_policy=policy,
            enrichment_input_hash=input_hash,
            anchor_texts={"lead:0001": "Chainalysis reported the activity."},
        )
    )

    assert proposals == ()
    assert calls == 0
    assert details["status"] == "model_failed"
    assert details["attempt_count"] == 2
    assert "semantic_annotation_call_failed:RuntimeError" in details["warnings"]


def test_semantic_annotation_versions_change_enrichment_input_hash(monkeypatch) -> None:
    extraction = _extraction()
    synthesis = _synthesis(extraction)
    common = {
        "extraction": extraction,
        "synthesis": synthesis,
        "evidence_pack_hash": "1" * 64,
        "access_policy_hash": "2" * 64,
    }
    baseline = compute_editorial_enrichment_input_hash(**common)
    for name in (
        "SEMANTIC_ANNOTATION_PROMPT_VERSION",
        "SEMANTIC_ANNOTATION_CONTRACT_VERSION",
        "SEMANTIC_ANNOTATION_WIRE_PARSER_VERSION",
        "SEMANTIC_ANNOTATION_POLICY_VERSION",
    ):
        current = getattr(enrichment_module, name)
        monkeypatch.setattr(enrichment_module, name, f"previous-{name.lower()}")
        assert compute_editorial_enrichment_input_hash(**common) != baseline
        monkeypatch.setattr(enrichment_module, name, current)

    assert semantic_annotation_input_hash(
        anchor_texts={"lead:0001": "Chainalysis."},
        access_policy_hash="2" * 64,
    ) != semantic_annotation_input_hash(
        anchor_texts={"lead:0001": "Chainalysis updated."},
        access_policy_hash="2" * 64,
    )


def test_publication_assembly_hash_changes_with_semantic_annotation_policy(monkeypatch) -> None:
    from cti_app.application import publication_builder
    from cti_app.application.publication_builder import compute_assembly_input_hash

    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    kwargs = {
        "snapshot": snapshot,
        "references": references,
        "extraction": extraction,
        "synthesis": synthesis,
        "editorial_enrichment": enrichment,
    }
    current = publication_builder.SEMANTIC_ANNOTATION_POLICY_VERSION
    actual = compute_assembly_input_hash(**kwargs)
    monkeypatch.setattr(publication_builder, "SEMANTIC_ANNOTATION_POLICY_VERSION", "old-policy")

    assert compute_assembly_input_hash(**kwargs) != actual
    assert publication_builder.SEMANTIC_ANNOTATION_POLICY_VERSION != current


def test_publication_qa_reports_semantic_annotation_sparse_as_warning() -> None:
    from dataclasses import replace

    from cti_app.application.publication_builder import build_publication_document_v5
    from cti_app.application.publication_qa import qa_publication_v5
    from cti_app.domain.production_synthesis import (
        SynthesisParagraphV1,
        extraction_evidence_refs_v1,
    )

    snapshot, references, extraction, synthesis = _canonical_inputs()
    evidence_ref = extraction_evidence_refs_v1(extraction)[0]
    long_synthesis = replace(
        synthesis,
        lead=(SynthesisParagraphV1("A" * 350, (evidence_ref,)),),
    )
    enrichment = _enrichment_with(extraction=extraction, synthesis=long_synthesis)
    publication = build_publication_document_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=long_synthesis,
        editorial_enrichment=enrichment,
    )

    result = qa_publication_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=long_synthesis,
        editorial_enrichment=enrichment,
        publication=publication,
    )

    assert result["passed"] is True
    assert result["checks"]["semantic_annotation_coverage"] is True
    assert result["warnings"] == ["semantic_annotation_sparse"]


def test_publication_qa_fails_when_one_annotated_term_occurrence_is_unstyled() -> None:
    from cti_app.application.publication_builder import build_publication_document_v4
    from cti_app.application.publication_qa import qa_publication_v5
    from cti_app.domain.publication_document import (
        PublicationDocumentV5,
        publication_document_text_anchors,
    )
    from cti_app.domain.semantic_annotation import (
        SEMANTIC_ANNOTATION_SCHEMA_VERSION,
        SemanticParagraphV1,
        SemanticTextSpanV1,
        SemanticTextV1,
    )

    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = _enrichment_with(extraction=extraction, synthesis=synthesis)
    base = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    spans = []
    for anchor, text in publication_document_text_anchors(base).items():
        if anchor == "title":
            spans.append(
                SemanticParagraphV1(
                    anchor,
                    (
                        SemanticTextSpanV1(SemanticRole.TEXT, "["),
                        SemanticTextSpanV1(SemanticRole.ACTOR, "Example"),
                        SemanticTextSpanV1(SemanticRole.TEXT, text[len("[Example") :]),
                    ),
                )
            )
        else:
            spans.append(
                SemanticParagraphV1(anchor, (SemanticTextSpanV1(SemanticRole.TEXT, text),))
            )
    publication = PublicationDocumentV5(
        document=base,
        semantic_text=SemanticTextV1(
            schema_version=SEMANTIC_ANNOTATION_SCHEMA_VERSION,
            policy_version=SEMANTIC_ANNOTATION_POLICY_VERSION,
            paragraphs=tuple(spans),
        ),
    )

    result = qa_publication_v5(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
        publication=publication,
    )

    assert result["checks"]["semantic_annotation_coverage"] is False
    assert any("semantic annotation" in error.lower() for error in result["errors"])


def test_semantic_annotation_wire_accepts_anchors_copied_with_their_prompt_frame() -> None:
    # Real 2026-10-05 output: the model copied `@@ANCHOR <id>@@` verbatim and every
    # one of 30 items was rejected as anchor_unknown.
    parsed = parse_semantic_annotation_wire(
        "TERM A001\nTERM: OP_RETURN\nROLE: protocol_field\n"
        "PARAGRAPH_ANCHOR: @@ANCHOR lead:0001@@\nEND TERM\n",
        {"lead:0001": "Données OP_RETURN encodées."},
    )

    assert parsed.rejections == ()
    assert [(item.paragraph_anchor, item.text) for item in parsed.proposals] == [
        ("lead:0001", "OP_RETURN")
    ]
