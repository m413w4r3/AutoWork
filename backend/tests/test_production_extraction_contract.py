from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import date
from uuid import UUID, uuid4

import pytest

from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionEvidenceBasis,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_FACT_CATEGORIES,
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionEventV1,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionProfileReasonCode,
    ExtractionReuseState,
    ExtractionRuleV1,
    ProductionExtractionOmissionReason,
    ProductionExtractionOmissionV1,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    production_extraction_from_json,
    production_extraction_to_json,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.publication import ArtifactType

_SUBJECT_ID = uuid4()
_DOCUMENT_ID = uuid4()
_CHECKPOINT_ID = uuid4()
_SHA = "a" * 64


def _rule(body: str, document_id: UUID) -> ExtractionRuleV1:
    return ExtractionRuleV1(
        rule_type=DetectionRuleType.YARA,
        name="R",
        body=body,
        sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
        context="",
        evidence_quote=body,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(document_id,),
    )


def _source(
    *,
    document_id: UUID = _DOCUMENT_ID,
    url: str = "https://example.test/report",
    tier: ProductionReferenceTier = ProductionReferenceTier.CORE,
    profile: ExtractionProfile | None = None,
    reuse_state: ExtractionReuseState = ExtractionReuseState.FRESH,
) -> ProductionSourceExtractionV1:
    return ProductionSourceExtractionV1(
        source_document_id=document_id,
        canonical_url=url,
        content_sha256=_SHA,
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.PRIMARY,
        profile=profile
        or (
            ExtractionProfile.FULL
            if tier is ProductionReferenceTier.CORE
            else ExtractionProfile.IOC_RULES
        ),
        checkpoint_id=_CHECKPOINT_ID,
        reuse_state=reuse_state,
        facts=(
            ExtractionFactV1(
                category="malware",
                value="FooRAT",
                attack_id=None,
                context="",
                evidence_quote="FooRAT",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(document_id,),
            ),
        ),
        events=(
            ExtractionEventV1(
                event_date=date(2024, 3, 2),
                date_text=None,
                text="FooRAT was deployed",
                context="",
                evidence_quote="2024-03-02 FooRAT was deployed",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(document_id,),
            ),
        ),
        indicators=(
            ExtractionIndicatorV1(
                value="evil.example",
                artifact_type=ArtifactType.DOMAIN,
                indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
                context="",
                evidence_quote="evil.example",
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=(document_id,),
            ),
        ),
        rules=(_rule("rule R { condition: true }", document_id),),
        uncertainties=("partial source load",),
    )


def _extraction(
    *sources: ProductionSourceExtractionV1,
    profile_policy_version: str = EXTRACTION_PROFILE_POLICY_VERSION,
) -> ProductionExtractionV1:
    return ProductionExtractionV1(
        schema_version=1,
        subject_id=_SUBJECT_ID,
        production_input_hash=_SHA,
        references_corpus_hash="b" * 64,
        profile_policy_version=profile_policy_version,
        sources=sources or (_source(),),
        omitted_sources=(),
        warnings=(),
    )


def test_round_trip_is_stable_and_strict() -> None:
    extraction = _extraction()

    payload = production_extraction_to_json(extraction)
    reloaded = production_extraction_from_json(payload)

    assert reloaded == extraction
    assert production_extraction_to_json(reloaded) == payload
    assert payload["schema_version"] == 1
    assert payload["profile_policy_version"] == EXTRACTION_PROFILE_POLICY_VERSION
    assert payload["sources"][0]["facts"][0]["source_document_ids"] == [str(_DOCUMENT_ID)]
    assert payload["sources"][0]["events"][0]["event_date"] == "2024-03-02"
    assert (
        payload["sources"][0]["rules"][0]["sha256"]
        == hashlib.sha256(b"rule R { condition: true }").hexdigest()
    )


def test_extra_top_level_field_is_rejected() -> None:
    payload = production_extraction_to_json(_extraction())
    payload["production_run_id"] = str(uuid4())

    with pytest.raises(ValueError, match="invalid shape"):
        production_extraction_from_json(payload)


def test_extra_nested_field_is_rejected() -> None:
    payload = production_extraction_to_json(_extraction())
    payload["sources"][0]["facts"][0]["model_run_id"] = "run-1"

    with pytest.raises(ValueError, match="invalid shape"):
        production_extraction_from_json(payload)


@pytest.mark.parametrize(
    "missing",
    ["schema_version", "subject_id", "production_input_hash", "profile_policy_version", "sources"],
)
def test_missing_field_is_rejected(missing: str) -> None:
    payload = production_extraction_to_json(_extraction())
    del payload[missing]

    with pytest.raises(ValueError, match="invalid shape"):
        production_extraction_from_json(payload)


def test_wrong_schema_version_is_rejected() -> None:
    payload = production_extraction_to_json(_extraction())
    payload["schema_version"] = 2

    with pytest.raises(ValueError, match="schema version"):
        production_extraction_from_json(payload)


def test_bad_source_hash_is_rejected() -> None:
    payload = production_extraction_to_json(_extraction())
    payload["sources"][0]["content_sha256"] = "A" * 64

    with pytest.raises(ValueError, match="content_sha256"):
        production_extraction_from_json(payload)


def test_bad_corpus_hash_is_rejected() -> None:
    payload = production_extraction_to_json(_extraction())
    payload["references_corpus_hash"] = "not-a-hash"

    with pytest.raises(ValueError, match="references_corpus_hash"):
        production_extraction_from_json(payload)


def test_bad_uuid_is_rejected() -> None:
    payload = production_extraction_to_json(_extraction())
    payload["sources"][0]["source_document_id"] = "S1"

    with pytest.raises(ValueError, match="source_document_id"):
        production_extraction_from_json(payload)


def test_non_canonical_uuid_is_rejected() -> None:
    payload = production_extraction_to_json(_extraction())
    payload["subject_id"] = str(_SUBJECT_ID).replace("-", "")

    with pytest.raises(ValueError, match="subject_id"):
        production_extraction_from_json(payload)


def test_duplicate_source_document_is_rejected() -> None:
    source = _source()

    with pytest.raises(ValueError, match="repeat a source document"):
        _extraction(source, source)


def test_duplicate_source_url_is_rejected() -> None:
    first = _source(document_id=uuid4(), url="https://example.test/a")
    second = _source(document_id=uuid4(), url="https://example.test/a")

    with pytest.raises(ValueError, match="repeat a source URL"):
        _extraction(first, second)


def test_legacy_supporting_full_profile_reason_still_loads() -> None:
    source = ProductionSourceExtractionV1(
        source_document_id=_DOCUMENT_ID,
        canonical_url="https://example.test/independent-analysis",
        content_sha256=_SHA,
        tier=ProductionReferenceTier.SUPPORTING,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        profile=ExtractionProfile.FULL,
        profile_reason_code=ExtractionProfileReasonCode.INDEPENDENT_CORROBORATION,
        checkpoint_id=_CHECKPOINT_ID,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )

    legacy = _extraction(source, profile_policy_version="production-reference-tier-v2")
    payload = production_extraction_to_json(legacy)["sources"][0]

    assert payload["tier"] == ProductionReferenceTier.SUPPORTING.value
    assert payload["profile"] == ExtractionProfile.FULL.value
    assert payload["editorial_role"] == ProductionEditorialRole.CORROBORATION.value
    assert (
        payload["profile_reason_code"]
        == ExtractionProfileReasonCode.INDEPENDENT_CORROBORATION.value
    )
    assert production_extraction_from_json(production_extraction_to_json(legacy)) == legacy


def test_current_policy_rejects_supporting_full_profile_and_defaults_support_reason() -> None:
    supporting_full = ProductionSourceExtractionV1(
        source_document_id=_DOCUMENT_ID,
        canonical_url="https://example.test/supporting-full",
        content_sha256=_SHA,
        tier=ProductionReferenceTier.SUPPORTING,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.INDEPENDENT,
        editorial_role=ProductionEditorialRole.CORROBORATION,
        profile=ExtractionProfile.FULL,
        checkpoint_id=_CHECKPOINT_ID,
        reuse_state=ExtractionReuseState.FRESH,
        facts=(),
        events=(),
        indicators=(),
        rules=(),
        uncertainties=(),
    )
    with pytest.raises(ValueError, match="profile does not match its reference tier"):
        _extraction(supporting_full)

    supporting_ioc_rules = replace(
        supporting_full,
        canonical_url="https://example.test/supporting-ioc-rules",
        profile=ExtractionProfile.IOC_RULES,
        profile_reason_code=None,
    )
    assert (
        supporting_ioc_rules.profile_reason_code is ExtractionProfileReasonCode.SUPPORTING_CONTEXT
    )


def test_provenance_must_include_the_owning_document() -> None:
    foreign = uuid4()
    with pytest.raises(ValueError, match="must include its own document"):
        ProductionSourceExtractionV1(
            source_document_id=_DOCUMENT_ID,
            canonical_url="https://example.test/report",
            content_sha256=_SHA,
            tier=ProductionReferenceTier.CORE,
            kind=ProductionReferenceKind.PUBLICATION,
            role=SourceRole.PRIMARY,
            profile=ExtractionProfile.FULL,
            checkpoint_id=None,
            reuse_state=ExtractionReuseState.FRESH,
            facts=(
                ExtractionFactV1(
                    category="malware",
                    value="FooRAT",
                    attack_id=None,
                    context="",
                    evidence_quote="FooRAT",
                    evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                    source_document_ids=(foreign,),
                ),
            ),
            events=(),
            indicators=(),
            rules=(),
            uncertainties=(),
        )


def test_source_verified_proposals_require_an_evidence_quote() -> None:
    with pytest.raises(ValueError, match="evidence quote"):
        ExtractionFactV1(
            category="malware",
            value="FooRAT",
            attack_id=None,
            context="",
            evidence_quote="",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(_DOCUMENT_ID,),
        )


def test_rule_hash_must_match_its_body() -> None:
    with pytest.raises(ValueError, match="hash does not match"):
        ExtractionRuleV1(
            rule_type=DetectionRuleType.YARA,
            name="R",
            body="rule R { condition: true }",
            sha256="c" * 64,
            context="",
            evidence_quote="rule R { condition: true }",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(_DOCUMENT_ID,),
        )


def test_provenance_is_sorted_deterministically() -> None:
    first, second = sorted((uuid4(), uuid4()), key=str)
    fact = ExtractionFactV1(
        category="malware",
        value="FooRAT",
        attack_id=None,
        context="",
        evidence_quote="FooRAT",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(second, first),
    )

    assert fact.source_document_ids == (first, second)
    with pytest.raises(ValueError, match="repeat a source document"):
        ExtractionFactV1(
            category="malware",
            value="FooRAT",
            attack_id=None,
            context="",
            evidence_quote="FooRAT",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(first, first),
        )


def test_sources_are_ordered_by_tier_url_and_document() -> None:
    core_b = _source(document_id=uuid4(), url="https://example.test/b")
    core_a = _source(document_id=uuid4(), url="https://example.test/a")
    technical = _source(
        document_id=uuid4(),
        url="https://example.test/c",
        tier=ProductionReferenceTier.TECHNICAL,
    )

    extraction = _extraction(technical, core_b, core_a)

    assert [source.canonical_url for source in extraction.sources] == [
        "https://example.test/a",
        "https://example.test/b",
        "https://example.test/c",
    ]
    assert [source["tier"] for source in production_extraction_to_json(extraction)["sources"]] == [
        "core",
        "core",
        "technical",
    ]


def test_omitted_sources_keep_their_diagnostic() -> None:
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://example.test/unavailable",
        tier=ProductionReferenceTier.TECHNICAL,
        collection_state=CollectionState.UNAVAILABLE,
        reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
        error_code=None,
    )
    extraction = _extraction()
    payload = production_extraction_to_json(extraction)
    payload["omitted_sources"] = [
        {
            "canonical_url": omission.canonical_url,
            "tier": omission.tier.value,
            "collection_state": omission.collection_state.value,
            "reason": omission.reason.value,
            "error_code": None,
        }
    ]

    reloaded = production_extraction_from_json(payload)

    assert reloaded.omitted_sources == (omission,)


def test_a_failed_source_omission_carries_its_code_and_never_concerns_core() -> None:
    failed = ProductionExtractionOmissionV1(
        canonical_url="https://example.test/supporting",
        tier=ProductionReferenceTier.SUPPORTING,
        collection_state=CollectionState.ARCHIVED,
        reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
        error_code="extraction_source_output_invalid",
    )
    assert failed.error_code == "extraction_source_output_invalid"
    with pytest.raises(ValueError):
        ProductionExtractionOmissionV1(
            canonical_url="https://example.test/supporting",
            tier=ProductionReferenceTier.SUPPORTING,
            collection_state=CollectionState.ARCHIVED,
            reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
            error_code=None,
        )
    with pytest.raises(ValueError):
        ProductionExtractionOmissionV1(
            canonical_url="https://example.test/core",
            tier=ProductionReferenceTier.CORE,
            collection_state=CollectionState.ARCHIVED,
            reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
            error_code="extraction_source_output_invalid",
        )


def test_a_source_cannot_be_extracted_and_omitted() -> None:
    omission = ProductionExtractionOmissionV1(
        canonical_url="https://example.test/report",
        tier=ProductionReferenceTier.CORE,
        collection_state=CollectionState.UNAVAILABLE,
        reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
        error_code=None,
    )

    with pytest.raises(ValueError, match="both extracted and omitted"):
        ProductionExtractionV1(
            schema_version=1,
            subject_id=_SUBJECT_ID,
            production_input_hash=_SHA,
            references_corpus_hash="b" * 64,
            profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
            sources=(_source(),),
            omitted_sources=(omission,),
            warnings=(),
        )


def test_empty_extraction_is_rejected() -> None:
    with pytest.raises(ValueError, match="at least one extracted source"):
        ProductionExtractionV1(
            schema_version=1,
            subject_id=_SUBJECT_ID,
            production_input_hash=_SHA,
            references_corpus_hash="b" * 64,
            profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
            sources=(),
            omitted_sources=(),
            warnings=(),
        )


def test_old_profile_policy_version_still_loads() -> None:
    legacy = _extraction(profile_policy_version="production-reference-tier-v2")

    assert production_extraction_from_json(production_extraction_to_json(legacy)) == legacy


def test_canonical_fact_categories_cover_full_extraction() -> None:
    assert {
        "actors",
        "campaigns",
        "malware",
        "tools",
        "products",
        "infection_chain",
        "ttps",
        "victimology",
        "protocols",
        "infrastructure",
        "files",
        "commands",
        "persistence",
        "detections",
        "sectors",
        "countries",
        "other_technical",
    } == set(EXTRACTION_FACT_CATEGORIES)

    with pytest.raises(ValueError, match="category"):
        ExtractionFactV1(
            category="editorial_title",
            value="FooRAT",
            attack_id=None,
            context="",
            evidence_quote="FooRAT",
            evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
            source_document_ids=(_DOCUMENT_ID,),
        )
