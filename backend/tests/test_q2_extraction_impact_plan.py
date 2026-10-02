"""AW-011 extraction planning impact: deterministic order, identity and progress.

The canonical planner replaces the retired Q1 impact plan. What matters now is
that the plan is stable, that the input hash ignores execution identities, and
that the per-source progress the desk reads is a faithful projection of the
canonical extraction.
"""

from __future__ import annotations

import hashlib
from datetime import date
from uuid import UUID, uuid4

from cti_app.application.production_extraction import (
    build_extraction_plan,
    extraction_input_hash,
    references_corpus_hash,
)
from cti_app.application.production_workflow import (
    _canonical_extraction_progress,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    DetectionRuleType,
    ExtractionProfile,
    ProductionEvidenceBasis,
)
from cti_app.domain.production_extraction import (
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ExtractionRuleV1,
    ProductionExtractionOmissionReason,
    ProductionExtractionOmissionV1,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
)
from cti_app.domain.production_references import (
    ProductionEditorialRole,
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
    is_eligible_for_extraction,
)
from cti_app.domain.publication import ArtifactType

SUBJECT_ID = uuid4()


def _source(
    url: str,
    *,
    tier: ProductionReferenceTier,
    document_id: UUID | None = None,
    sha256: str | None = None,
    role: SourceRole = SourceRole.PRIMARY,
    kind: ProductionReferenceKind | None = None,
    editorial_role: ProductionEditorialRole | None = None,
    state: CollectionState = CollectionState.ARCHIVED,
) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=kind
        or (
            ProductionReferenceKind.TECHNICAL_RESOURCE
            if tier is ProductionReferenceTier.TECHNICAL
            else ProductionReferenceKind.PUBLICATION
        ),
        role=role,
        editorial_role=editorial_role,
        title=f"Archived {url}",
        publisher="Publisher",
        published_at=date(2026, 7, 10),
        source_collection_id=None,
        source_document_id=document_id,
        discovery_candidate_ids=(),
        collection_state=state,
        content_sha256=sha256,
        relevance_reason=None,
        proposed_by_model=False,
        eligible_for_extraction=is_eligible_for_extraction(
            collection_state=state,
            source_document_id=document_id,
            content_sha256=sha256,
        ),
    )


def _corpus(*sources: ProductionReferenceSourceV1) -> ProductionReferenceCorpusV1:
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=SUBJECT_ID,
        research_date=date(2026, 8, 1),
        production_input_hash="a" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )


def _full_source(**overrides: object) -> ProductionReferenceSourceV1:
    defaults: dict[str, object] = {
        "url": "https://example.test/core",
        "tier": ProductionReferenceTier.CORE,
        "document_id": uuid4(),
        "sha256": "c" * 64,
    }
    defaults.update(overrides)
    return _source(**defaults)  # type: ignore[arg-type]


def test_plan_order_is_tier_then_url_then_document_identity() -> None:
    late = _full_source(url="https://example.test/z-core")
    early = _full_source(url="https://example.test/a-core")
    support = _full_source(
        url="https://example.test/b-support",
        tier=ProductionReferenceTier.SUPPORTING,
        document_id=uuid4(),
        sha256="d" * 64,
    )
    technical = _full_source(
        url="https://example.test/a-technical",
        tier=ProductionReferenceTier.TECHNICAL,
        document_id=uuid4(),
        sha256="e" * 64,
    )

    plan = build_extraction_plan(_corpus(support, late, technical, early))

    assert [source.canonical_url for source in plan.sources] == [
        "https://example.test/a-core",
        "https://example.test/z-core",
        "https://example.test/b-support",
        "https://example.test/a-technical",
    ]
    assert [source.position for source in plan.sources] == [0, 1, 2, 3]
    assert [source.profile for source in plan.sources] == [
        ExtractionProfile.FULL,
        ExtractionProfile.FULL,
        ExtractionProfile.FULL,
        ExtractionProfile.IOC_RULES,
    ]


def test_plan_and_input_hash_do_not_depend_on_python_or_execution_identity() -> None:
    first = _full_source(url="https://example.test/core")
    second = _full_source(
        url="https://example.test/other",
        tier=ProductionReferenceTier.SUPPORTING,
        document_id=uuid4(),
        sha256="f" * 64,
    )

    forwards = build_extraction_plan(_corpus(first, second))
    backwards = build_extraction_plan(_corpus(second, first))

    assert forwards == backwards
    assert forwards is not backwards
    assert forwards.input_hash == backwards.input_hash
    assert forwards.references_corpus_hash == references_corpus_hash(_corpus(first, second))
    assert (
        extraction_input_hash(references_corpus_hash=forwards.references_corpus_hash)
        == forwards.input_hash
    )


def test_progress_reports_every_corpus_source_with_its_canonical_status() -> None:
    fresh = _full_source(url="https://example.test/core-a")
    reused = _full_source(url="https://example.test/core-b", document_id=uuid4(), sha256="1" * 64)
    duplicate = _full_source(
        url="https://example.test/core-c", document_id=uuid4(), sha256="1" * 64
    )
    unavailable = _source(
        "https://example.test/unavailable",
        tier=ProductionReferenceTier.TECHNICAL,
        state=CollectionState.UNAVAILABLE,
    )
    plan = build_extraction_plan(_corpus(fresh, reused, duplicate, unavailable))
    by_url = {source.canonical_url: source for source in plan.sources}

    indicator = ExtractionIndicatorV1(
        value="evil.security-lab.io",
        artifact_type=ArtifactType.DOMAIN,
        indicator_status=ExtractionIndicatorStatus.CONFIRMED_IOC,
        context="C2",
        evidence_quote="evil.security-lab.io",
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(by_url["https://example.test/core-a"].source_document_id,),
    )
    rule_body = "rule ExampleRAT { condition: true }"
    rule = ExtractionRuleV1(
        rule_type=DetectionRuleType.YARA,
        name="ExampleRAT",
        body=rule_body,
        sha256=hashlib.sha256(rule_body.encode("utf-8")).hexdigest(),
        context="",
        evidence_quote=rule_body,
        evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
        source_document_ids=(by_url["https://example.test/core-a"].source_document_id,),
    )

    def _entry(url: str, reuse_state: ExtractionReuseState) -> ProductionSourceExtractionV1:
        source = by_url[url]
        return ProductionSourceExtractionV1(
            source_document_id=source.source_document_id,
            canonical_url=source.canonical_url,
            content_sha256=source.content_sha256,
            tier=source.tier,
            kind=source.kind,
            role=source.role,
            editorial_role=source.editorial_role,
            profile=source.profile,
            profile_reason_code=source.profile_reason_code,
            checkpoint_id=uuid4(),
            reuse_state=reuse_state,
            facts=(),
            events=(),
            indicators=(indicator,) if url.endswith("core-a") else (),
            rules=(rule,) if url.endswith("core-a") else (),
            uncertainties=("uncertain",) if url.endswith("core-a") else (),
        )

    extraction = ProductionExtractionV1(
        schema_version=1,
        subject_id=SUBJECT_ID,
        production_input_hash=plan.production_input_hash,
        references_corpus_hash=plan.references_corpus_hash,
        profile_policy_version=plan.profile_policy_version,
        sources=(
            _entry("https://example.test/core-a", ExtractionReuseState.FRESH),
            _entry("https://example.test/core-b", ExtractionReuseState.REUSED),
            _entry("https://example.test/core-c", ExtractionReuseState.CONTENT_DUPLICATE),
        ),
        omitted_sources=(
            ProductionExtractionOmissionV1(
                canonical_url="https://example.test/unavailable",
                tier=ProductionReferenceTier.TECHNICAL,
                collection_state=CollectionState.UNAVAILABLE,
                reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
                error_code=None,
            ),
        ),
        warnings=(),
    )

    progress = _canonical_extraction_progress(plan, extraction=extraction, model_calls=1)
    statuses = {entry["canonical_url"]: entry["status"] for entry in progress["sources"]}

    # No corpus source disappears, and no source is reported twice.
    assert len(progress["sources"]) == 4
    assert statuses["https://example.test/core-a"] == "succeeded"
    assert statuses["https://example.test/core-b"] == "cached"
    assert statuses["https://example.test/core-c"] == "cached"
    assert statuses["https://example.test/unavailable"] == "omitted"
    assert progress["completed_sources"] == 3
    assert progress["model_calls"] == 1
    assert progress["cache_hits"] == 2
    assert progress["full_total"] == 3
    assert progress["full_completed"] == 3
    # The technical source was never eligible: it stays visible but planned for
    # nothing, and no IOC_RULES profile was assigned.
    assert progress["ioc_rules_total"] == 0
    assert progress["skipped_sources"] == 1
    assert progress["confirmed_iocs"] == 1
    assert progress["rules_total"] == 1
    assert progress["yara_rules"] == 1
    assert progress["profile_policy_version"] == "production-reference-role-depth-v2"


def test_a_blocking_source_is_failed_and_the_others_stay_pending() -> None:
    fresh = _full_source(url="https://example.test/core-a")
    missing = _full_source(url="https://example.test/core-b", document_id=uuid4(), sha256="3" * 64)
    plan = build_extraction_plan(_corpus(fresh, missing))
    blocking = next(source for source in plan.sources if source.canonical_url.endswith("core-b"))

    progress = _canonical_extraction_progress(
        plan, failed_source_id=str(blocking.source_document_id)
    )
    statuses = {entry["canonical_url"]: entry["status"] for entry in progress["sources"]}

    assert statuses["https://example.test/core-b"] == "failed"
    assert statuses["https://example.test/core-a"] == "pending"
    assert progress["completed_sources"] == 0
