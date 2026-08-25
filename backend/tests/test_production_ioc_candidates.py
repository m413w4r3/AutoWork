from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_ioc_candidates import (
    DiscoveryPublicationEvidence,
    Q2LiteralCandidate,
    build_candidate_pack,
)
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import ParsedSource, ReferenceReport
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    DerivedArtifact,
    Indicator,
    IndicatorKind,
    SourceCollection,
    SourceOriginKind,
    SourceSpan,
)
from cti_app.domain.discovery import (
    DiscoveryIocType,
    ProvisionalDiscoveryIoc,
    ProvisionalIocPublicationRelation,
    SourceRole,
)
from cti_app.domain.entities import SourceDocument
from cti_app.domain.publication import ArtifactType

NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _records(
    value: str,
    *,
    kind: IndicatorKind = IndicatorKind.DOMAIN,
    source_url: str = "https://news.test/a",
    parent=None,
):
    subject_id, edition_id, group_id = uuid4(), uuid4(), uuid4()
    collection_id = uuid4()
    document_id, artifact_id = uuid4(), uuid4()
    collection = SourceCollection(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        requested_url=source_url,
        canonical_url=source_url,
        proposed_role=SourceRole.PRIMARY,
        origin_kind=SourceOriginKind.REFERENCED_EVIDENCE if parent else SourceOriginKind.DISCOVERY,
        parent_source_collection_id=parent,
        id=collection_id,
        source_document_id=document_id,
    )
    document = SourceDocument(
        subject_id=subject_id,
        blob_id=uuid4(),
        original_name="source.txt",
        origin="test",
        acquired_at=NOW,
        license_restriction=None,
        tlp=TLP.CLEAR,
        do_not_submit=False,
        external_llm_allowed=True,
        source_collection_id=collection_id,
        final_url=source_url,
        id=document_id,
    )
    artifact = DerivedArtifact(
        source_document_id=document_id,
        text_blob_id=uuid4(),
        parser_name="test-parser",
        parser_version="test-parser-v1",
        text_length=200,
        publication_metadata={},
        id=artifact_id,
    )
    indicator = Indicator(
        subject_id=subject_id,
        edition_id=edition_id,
        group_id=group_id,
        source_document_id=document_id,
        derived_artifact_id=artifact_id,
        kind=kind,
        original_value=value,
        normalized_value=value,
        span=SourceSpan(10, 10 + len(value)),
    )
    return indicator, collection, document, artifact


def _report(*urls: str) -> ReferenceReport:
    return ReferenceReport(
        sources=tuple(
            ParsedSource(f"S{i}", "title", url, url, None, None, SourceRole.PRIMARY)
            for i, url in enumerate(urls, 1)
        ),
        events=(),
    )


def _pack(rows, report, texts=True, **kwargs):
    indicators = [row[0] for row in rows]
    collections = [row[1] for row in rows]
    documents = [row[2] for row in rows]
    artifacts = [row[3] for row in rows]
    artifact_texts = (
        {row[3].id: "x" * 10 + row[0].original_value + " x" * 100 for row in rows} if texts else {}
    )
    return build_candidate_pack(
        indicators,
        collections=collections,
        source_documents=documents,
        artifacts=artifacts,
        reference_report=report,
        artifact_texts=artifact_texts,
        **kwargs,
    )


def test_same_source_two_occurrences_are_one_candidate_with_two_evidence():
    first = _records("evil[.]example")
    second = (
        replace(first[0], original_value="evil.example", span=SourceSpan(30, 42), id=uuid4()),
        *first[1:],
    )
    pack = _pack([first, second], _report("https://news.test/a"))
    assert len(pack.candidates) == 1
    assert len(pack.candidates[0].evidence) == 2
    assert pack.candidates[0].source_ids == ("S1",)


def test_same_ioc_in_two_sources_and_child_map_to_s_numbers():
    first = _records("evil.example", source_url="https://news.test/a")
    second = _records("evil[.]example", source_url="https://tech.test/ioc")
    third = _records("evil.example", source_url="https://tech.test/child", parent=first[1].id)
    # The child points to the parent's publication for source mapping.
    third = (third[0], third[1], replace(third[2], final_url="https://tech.test/child"), third[3])
    pack = _pack([first, second, third], _report("https://news.test/a", "https://tech.test/ioc"))
    assert pack.candidates[0].source_ids == ("S1", "S2")


def test_unmappable_source_warns_without_fabricated_source():
    row = _records("evil.example")
    pack = _pack([row], _report("https://other.test/report"))
    assert pack.candidates[0].source_ids == ()
    assert any("source_not_mapped" in warning for warning in pack.warnings)


@pytest.mark.parametrize(
    ("final_url", "report_url"),
    [
        ("https://vendor.test/report/", "https://vendor.test/report"),
        ("https://VENDOR.test/report?utm_source=x", "https://vendor.test/report"),
    ],
)
def test_root_final_url_is_canonicalized_for_source_mapping(final_url, report_url):
    row = _records("evil.example", source_url="https://vendor.test/report")
    row = (row[0], row[1], replace(row[2], final_url=final_url), row[3])
    pack = _pack([row], _report(report_url))
    assert pack.candidates[0].source_ids == ("S1",)


def test_invalid_final_url_falls_back_to_collection_and_child_uses_root_source():
    root = _records("evil.example", source_url="https://vendor.test/report")
    root = (root[0], root[1], replace(root[2], final_url="https://vendor.test:bad/report"), root[3])
    child = _records(
        "2001:db8::1",
        kind=IndicatorKind.IP,
        source_url="https://vendor.test/report/iocs.json",
        parent=root[1].id,
    )
    pack = _pack([root, child], _report("https://vendor.test/report"))
    assert {candidate.normalized_value: candidate.source_ids for candidate in pack.candidates} == {
        "evil.example": ("S1",),
        "2001:db8::1": ("S1",),
    }


def test_olalampo_redirect_keeps_document_iocs_on_s1():
    url = "https://www.group-ib.com/blog/muddywater-operation-olalampo"
    domain = _records("olalampo[.]example", source_url=url)
    domain = (domain[0], domain[1], replace(domain[2], final_url=f"{url}/"), domain[3])
    ip = _records("203.0.113.9", kind=IndicatorKind.IP, source_url=url)
    pack = _pack([domain, ip], _report(url))
    assert {candidate.normalized_value: candidate.source_ids for candidate in pack.candidates} == {
        "olalampo.example": ("S1",),
        "203.0.113.9": ("S1",),
    }


def test_invalid_indicator_is_skipped_without_losing_valid_candidate():
    invalid = _records("http://example.test:bad/path", kind=IndicatorKind.URL)
    valid = _records("evil{.}example")
    pack = _pack([invalid, valid], _report("https://news.test/a"))
    assert [candidate.normalized_value for candidate in pack.candidates] == ["evil.example"]
    assert any("invalid_indicator" in warning for warning in pack.warnings)


def test_equivalent_literals_share_candidate_id_and_pack_hash():
    one = _records("evil{.}example")
    two = (replace(one[0], original_value="evil[.]example"), *one[1:])
    first = _pack([one], _report("https://news.test/a"), texts=False)
    second = _pack([two], _report("https://news.test/a"), texts=False)
    assert first.candidates[0].candidate_id == second.candidates[0].candidate_id
    assert first.pack_hash == second.pack_hash


@pytest.mark.parametrize("kind", [IndicatorKind.CVE, IndicatorKind.ATTACK_ID])
def test_general_cti_types_are_excluded(kind):
    row = _records("CVE-2025-0001" if kind is IndicatorKind.CVE else "T1059", kind=kind)
    assert _pack([row], _report("https://news.test/a")).candidates == ()


def test_candidate_and_pack_hash_are_canonical_and_evidence_changes_hash():
    one = _records("evil.example")
    two = (replace(one[0], id=UUID("00000000-0000-0000-0000-000000000001")), *one[1:])
    # Input order does not affect the canonical result when records are fixed.
    first = _pack([one], _report("https://news.test/a"))
    second = _pack([one], _report("https://news.test/a"))
    assert first.pack_hash == second.pack_hash
    assert first.candidates[0].candidate_id == second.candidates[0].candidate_id
    changed = _pack([two], _report("https://news.test/a"))
    assert changed.pack_hash != first.pack_hash


def test_candidate_pack_is_stable():
    rows = [_records(f"host-{i}.example") for i in range(5)]
    pack = _pack(rows[::-1], _report("https://news.test/a"))
    assert pack.total_candidates == 5


def test_input_order_does_not_change_canonical_result():
    rows = [_records("z.example"), _records("a.example"), _records("m.example")]
    forward = _pack(rows, _report("https://news.test/a"))
    reverse = _pack(rows[::-1], _report("https://news.test/a"))
    assert forward == reverse


def test_q2_literal_is_recovered_from_archived_text_with_sourced_evidence() -> None:
    row = _records("ignored.example")
    collection = replace(row[1], derived_artifact_id=row[3].id)
    literal = Q2LiteralCandidate(
        artifact_type=ArtifactType.DOMAIN,
        raw_value="evil[.]example",
        normalized_value=normalize_indicator_value("evil[.]example", ArtifactType.DOMAIN),
        context="C2 cité par le rapport.",
    )
    pack = build_candidate_pack(
        (),
        collections=(collection,),
        source_documents=(row[2],),
        artifacts=(row[3],),
        reference_report=_report("https://news.test/a"),
        artifact_texts={row[3].id: "Le C2 est evil.example."},
        q2_literals=(literal,),
    )
    candidate = pack.candidates[0]
    assert candidate.source_ids == ("S1",)
    assert candidate.evidence[0].original_value == "evil.example"
    assert candidate.q2_contexts == ("C2 cité par le rapport.",)
    initial = build_candidate_pack((), collections=(), reference_report=_report("https://news.test/a"))
    diagnostics = ProductionWorkflowOrchestrator._q2_literal_diagnostics(
        (literal,), initial, pack
    )
    assert diagnostics == {
        "q2_literal_total": 1,
        "q2_literal_matched_candidates": 0,
        "q2_literal_recovered_from_source": 1,
        "q2_literal_unresolved": 0,
    }


def test_unresolved_q2_literal_is_retained_without_source_backing() -> None:
    literal = Q2LiteralCandidate(
        artifact_type=ArtifactType.DOMAIN,
        raw_value="missing.example",
        normalized_value="missing.example",
        context="non retrouvé",
    )
    pack = build_candidate_pack(
        (), collections=(), reference_report=_report("https://news.test/a"), q2_literals=(literal,)
    )
    assert not pack.candidates[0].source_backed
    assert any("q2_literal_unresolved" in warning for warning in pack.warnings)
    initial = build_candidate_pack((), collections=(), reference_report=_report("https://news.test/a"))
    diagnostics = ProductionWorkflowOrchestrator._q2_literal_diagnostics(
        (literal,), initial, pack
    )
    assert diagnostics["q2_literal_unresolved"] == 1


def test_q2_literal_already_in_initial_pack_is_matched() -> None:
    row = _records("evil.example")
    literal = Q2LiteralCandidate(
        artifact_type=ArtifactType.DOMAIN,
        raw_value="evil[.]example",
        normalized_value="evil.example",
        context="C2",
    )
    initial = build_candidate_pack(
        (row[0],),
        collections=(row[1],),
        source_documents=(row[2],),
        artifacts=(row[3],),
        reference_report=_report("https://news.test/a"),
    )
    final = build_candidate_pack(
        (row[0],),
        collections=(row[1],),
        source_documents=(row[2],),
        artifacts=(row[3],),
        reference_report=_report("https://news.test/a"),
        q2_literals=(literal,),
    )
    diagnostics = ProductionWorkflowOrchestrator._q2_literal_diagnostics(
        (literal,), initial, final
    )
    assert diagnostics["q2_literal_matched_candidates"] == 1


def _provisional(value: str, publication_id: UUID, kind=DiscoveryIocType.DOMAIN):
    return ProvisionalDiscoveryIoc(
        raw_value=value,
        normalized_value=None,
        declared_type=kind.value,
        proposed_type=kind,
        publication_relations=(
            ProvisionalIocPublicationRelation(publication_id, "P1", value, value),
        ),
        model_run_id=None,
        markdown_block=value,
    )


def test_discovery_existing_candidate_is_augmented_without_duplicate():
    row = _records("evil.example")
    provisional = _provisional("evil[.]example", row[1].id)
    publication = DiscoveryPublicationEvidence(row[2].id, row[3].id, ("S1",), "x evil.example x")
    pack = _pack(
        [row],
        _report("https://news.test/a"),
        provisional_iocs=(provisional,),
        discovery_publications={row[1].id: publication},
    )
    assert len(pack.candidates) == 1
    assert pack.candidates[0].discovery_provenance[0].provisional_ioc_id == provisional.id
    assert pack.discovery_augmented_candidates == 1


def test_discovery_value_found_in_source_gets_real_evidence():
    row = _records("other.example")
    provisional = _provisional("evil[.]example", row[1].id)
    publication = DiscoveryPublicationEvidence(row[2].id, row[3].id, ("S1",), "evil.example")
    pack = _pack(
        [row],
        _report("https://news.test/a"),
        provisional_iocs=(provisional,),
        discovery_publications={row[1].id: publication},
    )
    candidate = next(item for item in pack.candidates if item.normalized_value == "evil.example")
    assert candidate.source_ids == ("S1",)
    assert candidate.evidence[0].derived_artifact_id == row[3].id
    assert candidate.source_backed


def test_discovery_only_is_retained_but_unknown_publication_never_fabricates_source():
    provisional = _provisional("only.example", uuid4())
    pack = _pack([], _report(), provisional_iocs=(provisional,), discovery_publications={})
    candidate = pack.candidates[0]
    assert candidate.source_ids == ()
    assert pack.discovery_only_candidates == 1
    assert pack.discovery_unmatched == 1
    assert any("discovery_publication_unresolved" in warning for warning in pack.warnings)


def test_discovery_defanged_duplicate_hashes_and_unsupported_type_is_ignored():
    row = _records("evil.example")
    first = _provisional("evil[.]example", row[1].id)
    second = _provisional("evil.example", row[1].id)
    ignored = _provisional("CVE-2025-1", row[1].id, DiscoveryIocType.CVE)
    publication = DiscoveryPublicationEvidence(row[2].id, row[3].id, ("S1",), "evil.example")
    one = _pack(
        [row],
        _report("https://news.test/a"),
        provisional_iocs=(first,),
        discovery_publications={row[1].id: publication},
    )
    two = _pack(
        [row],
        _report("https://news.test/a"),
        provisional_iocs=(second,),
        discovery_publications={row[1].id: publication},
    )
    ignored_pack = _pack([row], _report("https://news.test/a"), provisional_iocs=(ignored,))
    assert one.pack_hash != two.pack_hash
    assert len(two.candidates) == 1
    assert ignored_pack.discovery_only_candidates == 0
    assert any("discovery_type_ignored" in warning for warning in ignored_pack.warnings)
