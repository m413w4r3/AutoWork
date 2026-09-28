"""Deterministic prompt evidence projections for production synthesis."""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, date, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.model_gateway import ModelRoutingHint
from cti_app.application.production_synthesis import (
    MAX_TECHNICAL_EVIDENCE_V1,
    SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION,
    SynthesisClaimProposalV1,
    SynthesisProposalControlError,
    SynthesisProposalV1,
    SynthesisSectionProposalV1,
    build_synthesis_access_policy,
    build_synthesis_delta,
    build_synthesis_evidence_pack,
    build_synthesis_model_request,
    build_synthesis_timeline,
    build_synthesis_uncertainties,
    canonical_extraction_hash,
    draft_synthesis_proposal,
    synthesis_access_policy_hash,
    synthesis_evidence_pack_hash,
    synthesis_input_hash,
    synthesis_model_run_id,
    validate_synthesis_proposal,
)
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
    SynthesisMode,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionEventV1,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ExtractionRuleV1,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
)
from cti_app.domain.production_references import ProductionReferenceKind, ProductionReferenceTier
from cti_app.domain.production_synthesis import (
    EvidenceKind,
    SynthesisSectionKind,
    extraction_evidence_refs_v1,
)
from cti_app.domain.publication import ArtifactType


def make_snapshot(subject_id: UUID) -> ProductionInputSnapshot:
    ids = [uuid4() for _ in range(7)]
    return ProductionInputSnapshot(
        production_run_id=ids[0],
        edition_id=ids[1],
        subject_id=subject_id,
        subject_version=1,
        subject_title="Frozen subject title",
        subject_tlp=TLP.CLEAR,
        selection_decision_id=ids[2],
        origin_discovery_subject_id=ids[3],
        canonical_discovery_subject_id=ids[4],
        discovery_snapshot_id=ids[5],
        discovery_snapshot_version=1,
        member_candidate_ids=(ids[6],),
        discovery_summary="A discovery summary.",
        actor_or_campaign="Example actor",
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        publication_language="fr",
        research_date=date(2026, 3, 1),
    )


def make_document(
    subject_id: UUID,
    source_id: UUID,
    *,
    tlp: TLP = TLP.CLEAR,
    external_llm_allowed: bool = True,
    do_not_submit: bool = False,
) -> SourceDocument:
    return SourceDocument(
        id=source_id,
        subject_id=subject_id,
        blob_id=uuid4(),
        original_name="source.pdf",
        origin="test",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=tlp,
        external_llm_allowed=external_llm_allowed,
        do_not_submit=do_not_submit,
    )


class MemorySourceDocuments:
    def __init__(self, documents: tuple[SourceDocument, ...]) -> None:
        self.documents = {document.id: document for document in documents}
        self.requested: list[UUID] = []

    async def get(self, document_id: UUID) -> SourceDocument | None:
        self.requested.append(document_id)
        return self.documents.get(document_id)


def make_fact(source_id: UUID, value: str, *, context: str = "") -> ExtractionFactV1:
    return ExtractionFactV1(
        category="malware",
        value=value,
        attack_id=None,
        context=context,
        evidence_quote=f"The report identifies {value}.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def make_event(
    source_id: UUID,
    text: str,
    event_date: date | None,
    *,
    date_text: str | None = None,
) -> ExtractionEventV1:
    return ExtractionEventV1(
        event_date=event_date,
        date_text=date_text,
        text=text,
        context="Campaign chronology.",
        evidence_quote=f"The report states: {text}",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def make_indicator(source_id: UUID, value: str) -> ExtractionIndicatorV1:
    return ExtractionIndicatorV1(
        value=value,
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="Published as command infrastructure.",
        evidence_quote=f"Domain: {value}",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )


def make_source(
    source_id: UUID,
    *,
    tier: ProductionReferenceTier = ProductionReferenceTier.CORE,
    facts: tuple[ExtractionFactV1, ...] = (),
    events: tuple[ExtractionEventV1, ...] = (),
    indicators: tuple[ExtractionIndicatorV1, ...] = (),
    rules: tuple[ExtractionRuleV1, ...] = (),
    uncertainties: tuple[str, ...] = (),
    url_suffix: str = "core",
) -> ProductionSourceExtractionV1:
    return ProductionSourceExtractionV1(
        source_document_id=source_id,
        canonical_url=f"https://example.com/{url_suffix}",
        content_sha256=hashlib.sha256(url_suffix.encode()).hexdigest(),
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=(
            ExtractionProfile.FULL
            if tier is ProductionReferenceTier.CORE
            else ExtractionProfile.IOC_RULES
        ),
        checkpoint_id=None,
        reuse_state=ExtractionReuseState.FRESH,
        facts=facts,
        events=events,
        indicators=indicators,
        rules=rules,
        uncertainties=uncertainties,
    )


def make_extraction(
    subject_id: UUID, sources: tuple[ProductionSourceExtractionV1, ...]
) -> ProductionExtractionV1:
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=subject_id,
        production_input_hash="a" * 64,
        references_corpus_hash="b" * 64,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        sources=sources,
        omitted_sources=(),
        warnings=(),
    )


def make_proposal(
    text: str,
    handle: str,
    *,
    section_kind: str = "overview",
    heading: str = "Overview",
) -> dict[str, object]:
    return {
        "lead": [{"text": text, "evidence_handles": [handle]}],
        "sections": [
            {
                "kind": section_kind,
                "heading": heading,
                "claims": [{"text": text, "evidence_handles": [handle]}],
            }
        ],
    }


def assert_proposal_error(call: Callable[[], object], expected_code: str) -> None:
    with pytest.raises(SynthesisProposalControlError) as error:
        call()
    assert error.value.code == expected_code


def test_refs_hash_and_prompt_handles_are_stable_across_source_load_order():
    subject_id = uuid4()
    core_id, technical_id = uuid4(), uuid4()
    core = make_source(
        core_id,
        facts=(make_fact(core_id, "FooRAT"), make_fact(core_id, "BarRAT")),
        url_suffix="a-core",
    )
    technical = make_source(
        technical_id,
        tier=ProductionReferenceTier.TECHNICAL,
        facts=(make_fact(technical_id, "Technical-only claim"),),
        indicators=(make_indicator(technical_id, "infra.example"),),
        url_suffix="z-technical",
    )
    first = make_extraction(subject_id, (core, technical))
    second = make_extraction(subject_id, (technical, core))

    assert canonical_extraction_hash(first) == canonical_extraction_hash(second)
    assert extraction_evidence_refs_v1(first) == extraction_evidence_refs_v1(second)
    first_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), first)
    second_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), second)
    assert first_pack.narrative_evidence == second_pack.narrative_evidence
    assert first_pack.technical_evidence == second_pack.technical_evidence
    assert tuple(record["handle"] for record in first_pack.narrative_evidence) == tuple(
        record["handle"] for record in second_pack.narrative_evidence
    )
    assert first_pack.policy_version == SYNTHESIS_EVIDENCE_PACK_POLICY_VERSION
    assert all("source_document_id" not in record for record in first_pack.narrative_evidence)
    assert "technical-only claim" not in str(first_pack.narrative_evidence)

    ref = first_pack.resolve_handle(first_pack.narrative_evidence[0]["handle"])
    assert ref.source_document_id == core_id
    try:
        first_pack.resolve_handle("E999")
    except ValueError as exc:
        assert str(exc) == "synthesis_unknown_evidence"
    else:
        raise AssertionError("unknown handles must not be approximated")


def test_changed_fact_payload_changes_its_evidence_key():
    subject_id, source_id = uuid4(), uuid4()
    original_fact = make_fact(source_id, "FooRAT")
    changed_fact = replace(original_fact, value="BarRAT")
    original = make_extraction(
        subject_id,
        (make_source(source_id, facts=(original_fact,)),),
    )
    changed = make_extraction(
        subject_id,
        (make_source(source_id, facts=(changed_fact,)),),
    )

    original_ref = next(
        ref for ref in extraction_evidence_refs_v1(original) if ref.kind is EvidenceKind.FACT
    )
    changed_ref = next(
        ref for ref in extraction_evidence_refs_v1(changed) if ref.kind is EvidenceKind.FACT
    )
    assert original_ref.source_document_id == changed_ref.source_document_id
    assert original_ref.evidence_key != changed_ref.evidence_key


def test_technical_pack_is_contextual_body_free_deterministic_and_bounded():
    subject_id, source_id = uuid4(), uuid4()
    indicators = tuple(
        make_indicator(source_id, f"host-{index:03d}.example") for index in range(140)
    )
    body = "rule body must remain in extraction only"
    rule = ExtractionRuleV1(
        rule_type=DetectionRuleType.YARA,
        name="Example rule",
        body=body,
        sha256=hashlib.sha256(body.encode()).hexdigest(),
        context="Detects the published sample.",
        evidence_quote="A YARA rule is provided.",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(source_id,),
    )
    source = make_source(
        source_id,
        tier=ProductionReferenceTier.SUPPORTING,
        facts=(make_fact(source_id, "Supporting campaign claim"),),
        indicators=indicators,
        rules=(rule,),
    )
    extraction = make_extraction(subject_id, (source,))
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    reordered = make_extraction(
        subject_id,
        (replace(source, indicators=tuple(reversed(indicators))),),
    )
    reordered_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), reordered)

    assert pack.narrative_evidence == ()
    assert len(pack.technical_evidence) == MAX_TECHNICAL_EVIDENCE_V1
    assert pack.technical_evidence == reordered_pack.technical_evidence
    assert all("body" not in record for record in pack.technical_evidence)
    assert body not in str(pack.technical_evidence)
    rule_only_source = replace(source, facts=(), indicators=())
    rule_only = make_extraction(subject_id, (rule_only_source,))
    rule_pack = build_synthesis_evidence_pack(make_snapshot(subject_id), rule_only)
    rule_record = rule_pack.technical_evidence[0]
    assert set(rule_record) == {"handle", "kind", "type", "name", "sha256", "context", "evidence"}
    assert body not in str(rule_pack.technical_evidence)


def test_timeline_deduplicates_events_and_sorts_dated_before_undated():
    subject_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    shared_date = date(2026, 7, 2)
    shared_text = "The campaign began."
    first = make_source(
        first_id,
        events=(
            make_event(first_id, "Undated activity", None),
            make_event(first_id, shared_text, shared_date, date_text="2 July 2026"),
            make_event(first_id, "Later activity", date(2026, 7, 4)),
        ),
        url_suffix="a-event",
    )
    second = make_source(
        second_id,
        tier=ProductionReferenceTier.SUPPORTING,
        events=(make_event(second_id, shared_text, shared_date, date_text="2 July 2026"),),
        url_suffix="b-event",
    )
    timeline = build_synthesis_timeline(make_extraction(subject_id, (second, first)))

    assert [entry.event_date for entry in timeline] == [
        date(2026, 7, 2),
        date(2026, 7, 4),
        None,
    ]
    assert timeline[0].text == shared_text
    assert len(timeline[0].evidence_refs) == 2
    assert timeline == build_synthesis_timeline(make_extraction(subject_id, (first, second)))


def test_uncertainties_union_provenance_and_delta_compares_exact_refs():
    subject_id = uuid4()
    first_id, second_id = uuid4(), uuid4()
    shared = "Attribution remains uncertain."
    previous = make_extraction(
        subject_id,
        (
            make_source(
                first_id,
                facts=(make_fact(first_id, "OldRAT"),),
                uncertainties=(shared, "Scope is unknown."),
                url_suffix="a-uncertainty",
            ),
            make_source(
                second_id,
                tier=ProductionReferenceTier.SUPPORTING,
                uncertainties=(shared,),
                url_suffix="b-uncertainty",
            ),
        ),
    )
    current = make_extraction(
        subject_id,
        (
            make_source(
                first_id,
                facts=(make_fact(first_id, "NewRAT"),),
                uncertainties=(shared, "Scope is unknown."),
                url_suffix="a-uncertainty",
            ),
            make_source(
                second_id,
                tier=ProductionReferenceTier.SUPPORTING,
                uncertainties=(shared,),
                url_suffix="b-uncertainty",
            ),
        ),
    )

    uncertainties = build_synthesis_uncertainties(previous)
    shared_uncertainty = next(item for item in uncertainties if item.text == shared)
    assert shared_uncertainty.source_document_ids == tuple(sorted((first_id, second_id), key=str))
    assert tuple(item.text for item in uncertainties) == (
        "Attribution remains uncertain.",
        "Scope is unknown.",
    )

    delta = build_synthesis_delta(previous, current)
    previous_refs = set(extraction_evidence_refs_v1(previous))
    current_refs = set(extraction_evidence_refs_v1(current))
    assert set(delta.added_evidence) == current_refs - previous_refs
    assert set(delta.removed_evidence) == previous_refs - current_refs
    assert set(delta.unchanged_evidence) == previous_refs & current_refs


def test_proposal_resolves_exact_handles_to_canonical_evidence_refs():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    lead, sections = validate_synthesis_proposal(
        make_proposal("FooRAT was identified in the report.", handle), pack, extraction
    )

    assert lead[0].text == "FooRAT was identified in the report."
    assert lead[0].evidence_refs == (pack.resolve_handle(handle),)
    assert sections[0].kind is SynthesisSectionKind.OVERVIEW
    assert not hasattr(lead[0], "evidence_handles")


def test_proposal_rejects_unknown_handle_and_malformed_schema():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("FooRAT was identified.", "E999"), pack, extraction
        ),
        "synthesis_unknown_evidence",
    )
    malformed = make_proposal("FooRAT was identified.", str(pack.narrative_evidence[0]["handle"]))
    malformed["subject_id"] = str(subject_id)
    assert_proposal_error(
        lambda: validate_synthesis_proposal(malformed, pack, extraction),
        "synthesis_output_invalid",
    )
    empty_refs = make_proposal("FooRAT was identified.", "unused")
    empty_refs["lead"] = [{"text": "FooRAT was identified.", "evidence_handles": []}]
    assert_proposal_error(
        lambda: validate_synthesis_proposal(empty_refs, pack, extraction),
        "synthesis_output_invalid",
    )


@pytest.mark.parametrize(
    "text",
    (
        "# Campaign overview",
        "| Source | Value |\n| --- | --- |",
        "A claim [S1] was reported.",
        "<p>A claim was reported.</p>",
        "---\ntitle: Campaign\n---",
    ),
)
def test_proposal_rejects_markdown_html_and_source_markers(text: str):
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    assert_proposal_error(
        lambda: validate_synthesis_proposal(make_proposal(text, handle), pack, extraction),
        "synthesis_output_invalid",
    )


def test_proposal_rejects_markup_in_section_heading():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("FooRAT was identified.", handle, heading="# Overview"),
            pack,
            extraction,
        ),
        "synthesis_output_invalid",
    )


def test_proposal_rejects_technical_literals_missing_from_extraction():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("FooRAT exploits CVE-2026-9999.", handle), pack, extraction
        ),
        "synthesis_unknown_technical_value",
    )


def test_technical_literal_scanner_accepts_extracted_value_classes():
    subject_id, source_id = uuid4(), uuid4()
    extracted_values = (
        "CVE-2026-1234",
        "192.0.2.7",
        "2001:db8::1",
        "evil.example",
        "https://evil.example/path",
        "a" * 64,
        "analyst@evil.example",
        "T1059.001",
    )
    text = "The report lists " + ", ".join(extracted_values) + "."
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, " ".join(extracted_values)),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])

    lead, _ = validate_synthesis_proposal(make_proposal(text, handle), pack, extraction)

    assert lead[0].text == text


def test_proposal_requires_technical_literal_to_be_covered_by_claim_refs():
    subject_id, source_id = uuid4(), uuid4()
    source = make_source(
        source_id,
        facts=(make_fact(source_id, "FooRAT"),),
        indicators=(make_indicator(source_id, "evil.example"),),
    )
    extraction = make_extraction(subject_id, (source,))
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    fact_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.FACT.value
    )
    proposal = make_proposal(
        "The domain evil.example supports the operation.",
        fact_handle,
        section_kind="technical",
        heading="Technical findings",
    )

    assert_proposal_error(
        lambda: validate_synthesis_proposal(proposal, pack, extraction),
        "synthesis_unknown_technical_value",
    )


def test_proposal_rejects_technical_only_evidence_for_campaign_narrative():
    subject_id, source_id = uuid4(), uuid4()
    source = make_source(
        source_id,
        tier=ProductionReferenceTier.TECHNICAL,
        indicators=(make_indicator(source_id, "evil.example"),),
    )
    extraction = make_extraction(subject_id, (source,))
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.technical_evidence[0]["handle"])

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("The campaign used evil.example.", handle, section_kind="campaign"),
            pack,
            extraction,
        ),
        "synthesis_output_invalid",
    )


def test_proposal_accepts_event_and_fact_supported_dates_and_rejects_unsupported_dates():
    subject_id, source_id = uuid4(), uuid4()
    event = make_event(source_id, "The campaign began.", date(2026, 7, 2))
    textual_date_event = make_event(
        source_id, "Another operation was reported.", None, date_text="4 July 2026"
    )
    dated_fact = make_fact(source_id, "FooRAT was reported on 2 July 2026")
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(dated_fact,), events=(event, textual_date_event)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    event_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.EVENT.value and record["event_date"] == "2026-07-02"
    )
    fact_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.FACT.value
    )
    textual_date_handle = next(
        str(record["handle"])
        for record in pack.narrative_evidence
        if record["kind"] == EvidenceKind.EVENT.value and record["date_text"] == "4 July 2026"
    )
    event_result = validate_synthesis_proposal(
        make_proposal("The campaign began on 2026-07-02.", event_handle), pack, extraction
    )
    fact_result = validate_synthesis_proposal(
        make_proposal("FooRAT was reported on 2 July 2026.", fact_handle), pack, extraction
    )
    textual_date_result = validate_synthesis_proposal(
        make_proposal("Another operation was reported on 4 July 2026.", textual_date_handle),
        pack,
        extraction,
    )
    assert event_result[0][0].evidence_refs == (pack.resolve_handle(event_handle),)
    assert fact_result[0][0].evidence_refs == (pack.resolve_handle(fact_handle),)
    assert textual_date_result[0][0].evidence_refs == (pack.resolve_handle(textual_date_handle),)
    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("The campaign began on 2026-07-03.", event_handle), pack, extraction
        ),
        "synthesis_unknown_date",
    )


def test_proposal_rejects_removed_revision_evidence():
    subject_id, source_id = uuid4(), uuid4()
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(make_snapshot(subject_id), extraction)
    handle = str(pack.narrative_evidence[0]["handle"])
    removed_ref = pack.resolve_handle(handle)

    assert_proposal_error(
        lambda: validate_synthesis_proposal(
            make_proposal("FooRAT was identified.", handle),
            pack,
            extraction,
            removed_evidence=(removed_ref,),
        ),
        "synthesis_unknown_evidence",
    )


def test_proposal_schema_objects_are_strict_and_validate_section_kinds():
    claim = SynthesisClaimProposalV1(text="FooRAT was reported.", evidence_handles=("E001",))
    section = SynthesisSectionProposalV1(
        kind=SynthesisSectionKind.OVERVIEW, heading="Overview", claims=(claim,)
    )
    assert section.claims == (claim,)
    with pytest.raises(ValueError):
        SynthesisClaimProposalV1(text="Claim", evidence_handles=())
    with pytest.raises(ValueError):
        SynthesisSectionProposalV1(kind="unknown", heading="Heading", claims=(claim,))  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_access_policy_folds_subject_and_exact_source_metadata_conservatively():
    subject_id = uuid4()
    snapshot = make_snapshot(subject_id)
    source_ids = (uuid4(), uuid4(), uuid4())
    extraction = make_extraction(
        subject_id,
        tuple(
            make_source(source_id, url_suffix=f"source-{index}")
            for index, source_id in enumerate(source_ids)
        ),
    )
    docs = (
        make_document(subject_id, source_ids[0], tlp=TLP.GREEN),
        make_document(
            subject_id,
            source_ids[1],
            tlp=TLP.RED,
            external_llm_allowed=False,
            do_not_submit=True,
        ),
        make_document(subject_id, source_ids[2], tlp=TLP.AMBER),
    )
    repo = MemorySourceDocuments(tuple(reversed(docs)))

    policy = await build_synthesis_access_policy(snapshot, extraction, repo)
    repeated = await build_synthesis_access_policy(
        snapshot, extraction, MemorySourceDocuments(docs)
    )

    assert policy.effective_tlp is TLP.RED
    assert policy.external_llm_allowed is False
    assert policy.do_not_submit is True
    assert tuple(item.source_document_id for item in policy.sources) == tuple(
        sorted(source_ids, key=str)
    )
    assert repo.requested == sorted(source_ids, key=str)
    assert synthesis_access_policy_hash(policy) == synthesis_access_policy_hash(repeated)


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["missing", "wrong_subject", "missing_field"])
async def test_access_policy_fails_closed_for_unavailable_or_invalid_exact_document(failure: str):
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(subject_id, (make_source(source_id),))
    if failure == "missing":
        document = None
    elif failure == "wrong_subject":
        document = make_document(uuid4(), source_id)
    else:
        document = SimpleNamespace(
            id=source_id,
            subject_id=subject_id,
            tlp=TLP.CLEAR,
            external_llm_allowed=True,
        )

    class Repository:
        async def get(self, requested_id: UUID) -> object | None:
            assert requested_id == source_id
            return document

    with pytest.raises(ValueError, match="synthesis_access_policy_unavailable"):
        await build_synthesis_access_policy(snapshot, extraction, Repository())  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_synthesis_hash_request_and_model_identity_are_functional_and_stateless():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    evidence_pack = build_synthesis_evidence_pack(snapshot, extraction)
    document = make_document(
        subject_id,
        source_id,
        tlp=TLP.AMBER,
        external_llm_allowed=True,
    )
    policy = await build_synthesis_access_policy(
        snapshot, extraction, MemorySourceDocuments((document,))
    )
    run = ProductionRun(
        id=snapshot.production_run_id,
        subject_id=subject_id,
        edition_id=snapshot.edition_id,
        pipeline_generation=4,
    )
    request = build_synthesis_model_request(
        run, snapshot, extraction, evidence_pack, policy, SynthesisMode.FRESH
    )

    assert request.web_search is False
    assert request.conversation is None
    assert request.background is False
    assert request.external_llm_allowed is True
    assert request.sensitivity == TLP.AMBER.value
    assert request.routing_hint is ModelRoutingHint.PREMIUM_SYNTHESIS
    assert request.evidence_pack_hash == synthesis_evidence_pack_hash(evidence_pack)
    assert all(str(source_id) not in request.text for source_id in (source_id, document.blob_id))
    assert all(record["handle"] in request.text for record in evidence_pack.narrative_evidence)
    assert request.run_id == synthesis_model_run_id(
        run,
        synthesis_input_hash(
            snapshot,
            extraction,
            evidence_pack,
            synthesis_access_policy_hash(policy),
        ),
        SynthesisMode.FRESH,
    )
    assert request.run_id == synthesis_model_run_id(
        run, request.metadata["synthesis_input_hash"], SynthesisMode.FRESH
    )
    changed_generation = replace(run, pipeline_generation=5)
    assert request.run_id != synthesis_model_run_id(
        changed_generation, request.metadata["synthesis_input_hash"], SynthesisMode.FRESH
    )


@pytest.mark.asyncio
async def test_synthesis_input_hash_changes_with_policy_but_ignores_run_ids_and_timestamps():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(
        subject_id,
        (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),),
    )
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    permissive = await build_synthesis_access_policy(
        snapshot,
        extraction,
        MemorySourceDocuments((make_document(subject_id, source_id),)),
    )
    restricted = await build_synthesis_access_policy(
        snapshot,
        extraction,
        MemorySourceDocuments((make_document(subject_id, source_id, external_llm_allowed=False),)),
    )
    original_hash = synthesis_input_hash(
        snapshot, extraction, pack, synthesis_access_policy_hash(permissive)
    )
    changed_hash = synthesis_input_hash(
        snapshot, extraction, pack, synthesis_access_policy_hash(restricted)
    )
    other_run_snapshot = replace(
        snapshot,
        production_run_id=uuid4(),
        id=uuid4(),
        captured_at=datetime(2030, 1, 1, tzinfo=UTC),
    )
    other_run_pack = build_synthesis_evidence_pack(other_run_snapshot, extraction)

    assert original_hash != changed_hash
    assert original_hash == synthesis_input_hash(
        other_run_snapshot,
        extraction,
        other_run_pack,
        synthesis_access_policy_hash(permissive),
    )


@pytest.mark.asyncio
async def test_model_gateway_receives_synthesis_proposal_as_structured_schema():
    subject_id, source_id = uuid4(), uuid4()
    snapshot = make_snapshot(subject_id)
    extraction = make_extraction(
        subject_id, (make_source(source_id, facts=(make_fact(source_id, "FooRAT"),)),)
    )
    pack = build_synthesis_evidence_pack(snapshot, extraction)
    policy = await build_synthesis_access_policy(
        snapshot, extraction, MemorySourceDocuments((make_document(subject_id, source_id),))
    )
    run = ProductionRun(
        id=snapshot.production_run_id, subject_id=subject_id, edition_id=snapshot.edition_id
    )
    request = build_synthesis_model_request(
        run, snapshot, extraction, pack, policy, SynthesisMode.FRESH
    )

    class Gateway:
        request_seen: object | None = None
        schema_seen: object | None = None

        async def draft(self, model_request: object, output_schema: object) -> str:
            self.request_seen = model_request
            self.schema_seen = output_schema
            return "drafted"

    gateway = Gateway()
    result = await draft_synthesis_proposal(gateway, request)  # type: ignore[arg-type]
    assert result == "drafted"
    assert gateway.request_seen is request
    assert gateway.schema_seen is SynthesisProposalV1
