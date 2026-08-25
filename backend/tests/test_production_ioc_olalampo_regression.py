"""Golden regression for IOC loss between an archived publication and Q1."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.extraction import extract_indicators, parse_document
from cti_app.application.production_ioc_candidates import build_candidate_pack
from cti_app.application.production_ioc_qualification import (
    merge_qualified_candidates,
    parse_ioc_qualifications,
)
from cti_app.application.production_parsers import (
    parse_reference_report,
    parse_technical_extraction,
)
from cti_app.application.production_rendering import collect_indicators, render_brief
from cti_app.application.production_stages import compute_input_hash
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    CollectionState,
    DerivedArtifact,
    DetectedMimeType,
    SourceCollection,
    SourceOriginKind,
)
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument

ROOT_URL = "https://archive.test/operation-s1"
ANNEX_URL = "https://archive.test/operation-s1/technical-iocs.json"
RESEARCH_DATE = date(2026, 8, 25)
NOW = datetime(2026, 8, 25, tzinfo=UTC)

ROOT_HTML = """
<html><head><title>S1 synthetic incident note</title></head><body>
<h1>Operation S1</h1>
<p>A synthetic intrusion affected a test organization and used a short-lived
loader. The narrative intentionally omits most technical indicators.</p>
<h2>Indicators of Compromise</h2>
<h3>Domains</h3>
<ul>
  <li>miniquest[.]org</li><li>codefusiontech[.]org</li>
  <li>Promoverse[.]org</li><li>jerusalemsolutions[.]com</li>
  <li>example[.]com is a legitimate service</li>
</ul>
<h3>IPv4</h3>
<ul>
  <li>162.0.230[.]185 (C2 / IOC)</li><li>209.74.87[.]100</li>
  <li>143.198.5[.]41</li><li>209.74.87[.]67</li>
  <li>198.51.100[.]42 is the victim address</li><li>0.0.0[.]0 is a TEST_SENTINEL</li>
</ul>
<h3>SHA-1</h3>
<ul>
  <li>1111111111111111111111111111111111111111</li>
  <li>2222222222222222222222222222222222222222</li>
  <li>abcdefabcdefabcdefabcdefabcdefabcdefabcd</li>
</ul>
<p>See the <a href="/operation-s1/technical-iocs.json">technical IOC annex JSON</a>.</p>
<p>Related metadata: CVE-2026-12345 and ATT&amp;CK T1059.001.</p>
</body></html>
"""

ANNEX_JSON = """
{
  "sha1": [
    "1111111111111111111111111111111111111111",
    "3333333333333333333333333333333333333333"
  ],
  "domain": "annex-command[.]net",
  "editorial_note": "Technical evidence for S1, not a separate publication"
}
"""

Q1 = f"""# REFERENCE REPORT
## SOURCE S1
title: Synthetic Operation S1
url: {ROOT_URL}
role: primary

## EVENT R1
date: 2026-08-20
sources: S1
text: A synthetic loader was observed during the operation; the timeline deliberately
omits the IOC inventory.
"""

Q2 = """# EXTRACTION CTI
## NETWORK ARTIFACTS
### ITEM N1
type: domain
value: invented-q2.example
context: claimed C2
references: R1
sources: S1
"""


def _persisted_source(
    *,
    subject_id: UUID,
    edition_id: UUID,
    group_id: UUID,
    url: str,
    content: str,
    mime: DetectedMimeType,
    parent: SourceCollection | None = None,
) -> tuple[SourceCollection, SourceDocument, DerivedArtifact, str]:
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        requested_url=url,
        canonical_url=url,
        proposed_role=SourceRole.PRIMARY,
        origin_kind=(
            SourceOriginKind.REFERENCED_EVIDENCE if parent else SourceOriginKind.DISCOVERY
        ),
        parent_source_collection_id=parent.id if parent else None,
        state=CollectionState.ARCHIVED,
    )
    document = SourceDocument(
        subject_id=subject_id,
        blob_id=uuid4(),
        original_name=url.rsplit("/", 1)[-1],
        origin="synthetic archived fixture",
        acquired_at=NOW,
        license_restriction=None,
        tlp=TLP.CLEAR,
        do_not_submit=False,
        external_llm_allowed=True,
        source_collection_id=collection.id,
        final_url=url,
        declared_mime_type=mime.value,
        detected_mime_type=mime.value,
    )
    artifact = DerivedArtifact(
        source_document_id=document.id,
        text_blob_id=uuid4(),
        parser_name="cti-safe-text",
        parser_version="2.1.0",
        text_length=len(content),
        publication_metadata={},
    )
    collection.source_document_id = document.id
    collection.derived_artifact_id = artifact.id
    return collection, document, artifact, content


def _qualification_text(pack: Any) -> str:
    contextual = {"example.com", "198.51.100.42"}
    excluded = {"0.0.0.0"}
    blocks: list[str] = []
    for candidate in pack.candidates:
        if candidate.normalized_value in contextual:
            status = "contextual"
            reason = "Legitimate service or victim address, contextualized in S1."
        elif candidate.normalized_value in excluded:
            status = "excluded"
            reason = "TEST_SENTINEL is explicitly a fixture value, not an IOC."
        elif candidate.normalized_value == "162.0.230.185":
            status = "confirmed_ioc"
            reason = "S1 explicitly qualifies this address as C2 / IOC."
        else:
            status = "confirmed_ioc"
            reason = "Explicit IOC in the archived S1 evidence or its technical annex."
        blocks.append(f"candidate-id: {candidate.candidate_id}\nstatus: {status}\nreason: {reason}")
    return "\n\n".join(blocks)


def test_operation_olalampo_ioc_loss_is_recovered_end_to_end() -> None:
    subject_id, edition_id, group_id = uuid4(), uuid4(), uuid4()
    root = _persisted_source(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        url=ROOT_URL,
        content=ROOT_HTML,
        mime=DetectedMimeType.HTML,
    )
    annex = _persisted_source(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        url=ANNEX_URL,
        content=ANNEX_JSON,
        mime=DetectedMimeType.JSON,
        parent=root[0],
    )
    collections = (root[0], annex[0])
    documents = (root[1], annex[1])
    artifacts = (root[2], annex[2])

    parsed_root = parse_document(ROOT_HTML.encode(), DetectedMimeType.HTML)
    assert [(link.href, link.anchor_text) for link in parsed_root.links] == [
        ("/operation-s1/technical-iocs.json", "technical IOC annex JSON")
    ]
    parsed_annex = parse_document(ANNEX_JSON.encode(), DetectedMimeType.JSON)
    root_indicators = extract_indicators(
        parsed_root.text,
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        source_document_id=root[1].id,
        artifact_id=root[2].id,
    )
    annex_indicators = extract_indicators(
        parsed_annex.text,
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        source_document_id=annex[1].id,
        artifact_id=annex[2].id,
    )
    indicators = (*root_indicators, *annex_indicators)

    report_result = parse_reference_report(Q1, RESEARCH_DATE)
    assert report_result.usable and report_result.value is not None
    report = report_result.value
    q2_result = parse_technical_extraction(Q2, report)
    assert q2_result.usable and q2_result.value is not None

    pack = build_candidate_pack(
        indicators,
        collections=collections,
        source_documents=documents,
        artifacts=artifacts,
        reference_report=report,
        artifact_texts={root[2].id: parsed_root.text, annex[2].id: parsed_annex.text},
    )
    by_value = {candidate.normalized_value: candidate for candidate in pack.candidates}

    assert {
        "miniquest.org",
        "codefusiontech.org",
        "promoverse.org",
        "jerusalemsolutions.com",
    } <= by_value.keys()
    assert {"162.0.230.185", "209.74.87.100", "143.198.5.41", "209.74.87.67"} <= by_value.keys()
    assert {
        "1111111111111111111111111111111111111111",
        "2222222222222222222222222222222222222222",
        "abcdefabcdefabcdefabcdefabcdefabcdefabcd",
        "3333333333333333333333333333333333333333",
    } <= by_value.keys()
    assert by_value["annex-command.net"].source_ids == ("S1",)
    assert by_value["annex-command.net"].evidence[0].source_ids == ("S1",)
    assert all(candidate.source_ids == ("S1",) for candidate in pack.candidates)
    assert len(by_value["1111111111111111111111111111111111111111"].evidence) == 2
    assert {candidate.artifact_type.value for candidate in pack.candidates} == {
        "domain",
        "ip",
        "hash",
    }
    assert pack.sources_unmapped == 0

    # Main regression: S1 is absent from the Q1 narrative, but its archived
    # body still reaches the final IOC corpus through persisted indicators.
    assert "162.0.230.185" not in report.events[0].text
    assert "162.0.230.185" in by_value

    qualification = parse_ioc_qualifications(_qualification_text(pack), pack.batches[0])
    assert qualification.usable
    assert len(qualification.qualifications) == pack.total_candidates
    assert len(qualification.missing_candidate_ids) == 0

    incomplete = parse_ioc_qualifications(
        _qualification_text(pack).rsplit("\n\n", 1)[0], pack.batches[0]
    )
    assert not incomplete.usable
    assert "ioc_candidate_coverage_incomplete" in incomplete.errors
    repaired_incomplete = parse_ioc_qualifications(
        _qualification_text(pack).rsplit("\n\n", 1)[0], pack.batches[0]
    )
    assert not repaired_incomplete.usable
    assert repaired_incomplete.missing_candidate_ids

    final_extraction = merge_qualified_candidates(
        ProductionWorkflowOrchestrator._suppress_unbacked_q2_literals(
            q2_result.value, pack.candidates
        ),
        qualification.qualifications,
        pack.candidates,
    )
    confirmed = {item.normalized_value for item in collect_indicators(final_extraction)}
    assert confirmed == {
        candidate.normalized_value
        for candidate in pack.candidates
        if candidate.normalized_value not in {"example.com", "198.51.100.42", "0.0.0.0"}
    }
    assert "invented-q2.example" not in confirmed
    assert "example.com" not in confirmed
    assert "198.51.100.42" not in confirmed
    assert "0.0.0.0" not in confirmed
    assert not {"CVE-2026-12345", "T1059.001"} & by_value.keys()

    rendered = render_brief(
        subject_title="Synthetic Operation S1",
        report=report,
        extraction=final_extraction,
        synthesis_text="The timeline confirms the campaign [S1].",
        numbering={"S1": 1},
    )
    assert "162.0.230[.]185" in rendered
    assert "example.com" not in rendered
    assert "198.51.100.42" not in rendered
    assert "0.0.0.0" not in rendered

    diagnostics = {
        "candidate_total": pack.total_candidates,
        "qualified_total": len(qualification.qualifications),
        "confirmed_total": sum(
            q.status.value == "confirmed_ioc" for q in qualification.qualifications
        ),
        "contextual_total": sum(
            q.status.value == "contextual" for q in qualification.qualifications
        ),
        "excluded_total": sum(q.status.value == "excluded" for q in qualification.qualifications),
        "linked_evidence_count": pack.linked_evidence_occurrences,
        "unmapped_source_count": pack.sources_unmapped,
    }
    assert diagnostics == {
        "candidate_total": 16,
        "qualified_total": 16,
        "confirmed_total": 13,
        "contextual_total": 2,
        "excluded_total": 1,
        "linked_evidence_count": 17,
        "unmapped_source_count": 0,
    }

    original_input_hash = compute_input_hash(
        {"candidate_pack_hash": pack.pack_hash, "stage": "extraction"}
    )
    changed_indicator = replace(
        root_indicators[0],
        original_value="new-archive-ioc[.]net",
        normalized_value="new-archive-ioc.net",
        span=root_indicators[0].span,
        id=uuid4(),
    )
    changed_pack = build_candidate_pack(
        (*indicators, changed_indicator),
        collections=collections,
        source_documents=documents,
        artifacts=artifacts,
        reference_report=report,
        artifact_texts={root[2].id: parsed_root.text, annex[2].id: parsed_annex.text},
    )
    assert changed_pack.pack_hash != pack.pack_hash
    assert (
        compute_input_hash({"candidate_pack_hash": changed_pack.pack_hash, "stage": "extraction"})
        != original_input_hash
    )
