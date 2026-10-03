"""AW-011 extraction policy and the architecture cutover."""

from __future__ import annotations

import ast
import inspect
import re
from datetime import date
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application import production_extraction, production_workflow
from cti_app.application.production_extraction import (
    build_extraction_plan,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    ExtractionProfile,
)
from cti_app.domain.production_extraction import (
    ExtractionProfileReasonCode,
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

CANONICAL_MODULE = Path(production_extraction.__file__)


def _record(
    url: str,
    *,
    tier: ProductionReferenceTier,
    role: SourceRole,
    document_id: UUID | None,
    sha256: str | None,
    kind: ProductionReferenceKind = ProductionReferenceKind.PUBLICATION,
    editorial_role: ProductionEditorialRole | None = None,
    state: CollectionState = CollectionState.ARCHIVED,
) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=kind,
        role=role,
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
        editorial_role=editorial_role,
    )


def _corpus(*sources: ProductionReferenceSourceV1) -> ProductionReferenceCorpusV1:
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=uuid4(),
        research_date=date(2026, 8, 1),
        production_input_hash="a" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )


PROFILE_POLICY = (
    (
        ProductionReferenceTier.CORE,
        ProductionReferenceKind.PUBLICATION,
        SourceRole.PRIMARY,
        ProductionEditorialRole.PRIMARY,
        ExtractionProfile.FULL,
        ExtractionProfileReasonCode.CORE_PRIMARY_SOURCE,
    ),
    # Only CORE sources are the main subject of an article; every complementary
    # source adds IOCs, rules and their context whatever its editorial role.
    *(
        (
            ProductionReferenceTier.SUPPORTING,
            ProductionReferenceKind.PUBLICATION,
            source_role,
            editorial_role,
            ExtractionProfile.IOC_RULES,
            ExtractionProfileReasonCode.SUPPORTING_CONTEXT,
        )
        for source_role, editorial_role in (
            (SourceRole.INDEPENDENT, ProductionEditorialRole.CORROBORATION),
            (SourceRole.PRIMARY, ProductionEditorialRole.COUNTER_ANALYSIS),
            (SourceRole.PRIMARY, ProductionEditorialRole.CONTEXT),
        )
    ),
    *(
        (
            ProductionReferenceTier.TECHNICAL,
            ProductionReferenceKind.TECHNICAL_RESOURCE,
            source_role,
            editorial_role,
            ExtractionProfile.IOC_RULES,
            ExtractionProfileReasonCode.TECHNICAL_ANNEX,
        )
        for source_role, editorial_role in (
            (SourceRole.PRIMARY, ProductionEditorialRole.CONTEXT),
            (SourceRole.INDEPENDENT, ProductionEditorialRole.CORROBORATION),
            (SourceRole.INDEPENDENT, ProductionEditorialRole.CONTEXT),
        )
    ),
)


@pytest.mark.parametrize(
    ("tier", "kind", "role", "editorial_role", "expected", "reason_code"), PROFILE_POLICY
)
def test_only_core_sources_receive_full_extraction(
    tier: ProductionReferenceTier,
    kind: ProductionReferenceKind,
    role: SourceRole,
    editorial_role: ProductionEditorialRole,
    expected: ExtractionProfile,
    reason_code: ExtractionProfileReasonCode,
) -> None:
    corpus = _corpus(
        _record(
            "https://example.test/declared-core",
            tier=ProductionReferenceTier.CORE,
            role=SourceRole.PRIMARY,
            document_id=uuid4(),
            sha256="c" * 64,
        ),
        _record(
            f"https://example.test/{tier.value}-{role.value}",
            tier=tier,
            role=role,
            document_id=uuid4(),
            sha256="b" * 64,
            kind=kind,
            editorial_role=editorial_role,
        ),
    )
    plan = build_extraction_plan(corpus)

    planned = next(
        source
        for source in plan.sources
        if source.canonical_url == f"https://example.test/{tier.value}-{role.value}"
    )
    assert planned.profile is expected
    assert planned.profile_reason_code is reason_code
    assert planned.editorial_role is editorial_role


def test_policy_version_participates_in_the_plan() -> None:
    corpus = _corpus(
        _record(
            "https://example.test/core",
            tier=ProductionReferenceTier.CORE,
            role=SourceRole.PRIMARY,
            document_id=uuid4(),
            sha256="b" * 64,
        )
    )

    plan = build_extraction_plan(corpus)

    assert plan.profile_policy_version == EXTRACTION_PROFILE_POLICY_VERSION


def test_policy_version_change_invalidates_source_checkpoint_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    identity_before = production_extraction.source_checkpoint_identity(
        content_sha256="b" * 64,
        profile=ExtractionProfile.FULL,
        prompt_version=production_extraction.source_prompt_version(ExtractionProfile.FULL),
    )

    monkeypatch.setattr(
        production_extraction,
        "EXTRACTION_PROFILE_POLICY_VERSION",
        "production-reference-tier-core-only-v4-test",
    )
    identity_after = production_extraction.source_checkpoint_identity(
        content_sha256="b" * 64,
        profile=ExtractionProfile.FULL,
        prompt_version=production_extraction.source_prompt_version(ExtractionProfile.FULL),
    )

    assert identity_before["profile_policy_version"] != identity_after["profile_policy_version"]
    assert identity_before != identity_after


def test_same_source_capture_keeps_subject_independent_extraction_identity() -> None:
    shared_document_id = uuid4()
    shared_hash = "d" * 64
    core_source = _record(
        "https://example.test/core",
        tier=ProductionReferenceTier.CORE,
        role=SourceRole.PRIMARY,
        document_id=uuid4(),
        sha256="c" * 64,
        editorial_role=ProductionEditorialRole.PRIMARY,
    )
    shared_source = _record(
        "https://example.test/independent-analysis",
        tier=ProductionReferenceTier.SUPPORTING,
        role=SourceRole.INDEPENDENT,
        document_id=shared_document_id,
        sha256=shared_hash,
        editorial_role=ProductionEditorialRole.CORROBORATION,
    )

    first_plan = build_extraction_plan(_corpus(core_source, shared_source))
    second_plan = build_extraction_plan(_corpus(core_source, shared_source))
    first = next(source for source in first_plan.sources if source.content_sha256 == shared_hash)
    second = next(source for source in second_plan.sources if source.content_sha256 == shared_hash)
    first_identity = production_extraction.source_checkpoint_identity(
        content_sha256=first.content_sha256,
        profile=first.profile,
        prompt_version=production_extraction.source_prompt_version(first.profile),
    )
    second_identity = production_extraction.source_checkpoint_identity(
        content_sha256=second.content_sha256,
        profile=second.profile,
        prompt_version=production_extraction.source_prompt_version(second.profile),
    )

    assert first_plan.subject_id != second_plan.subject_id
    assert first.computation_key == second.computation_key
    assert first_identity == second_identity
    assert "subject_id" not in first_identity


def test_ineligible_source_never_reaches_the_plan() -> None:
    corpus = _corpus(
        _record(
            "https://example.test/core",
            tier=ProductionReferenceTier.CORE,
            role=SourceRole.PRIMARY,
            document_id=uuid4(),
            sha256="b" * 64,
        ),
        _record(
            "https://example.test/unavailable",
            tier=ProductionReferenceTier.SUPPORTING,
            role=SourceRole.PRIMARY,
            document_id=None,
            sha256=None,
            state=CollectionState.UNAVAILABLE,
        ),
    )

    plan = build_extraction_plan(corpus)

    assert [source.canonical_url for source in plan.sources] == ["https://example.test/core"]
    assert [omission.reason.value for omission in plan.omitted_sources] == [
        "reference_not_eligible"
    ]
    assert plan.omitted_sources[0].canonical_url == "https://example.test/unavailable"
    assert plan.omitted_sources[0].tier is ProductionReferenceTier.SUPPORTING
    assert plan.omitted_sources[0].collection_state is CollectionState.UNAVAILABLE


# --- architecture guards ----------------------------------------------------


def _canonical_source() -> str:
    return CANONICAL_MODULE.read_text(encoding="utf-8")


def test_canonical_module_has_no_legacy_references_dependency() -> None:
    source = _canonical_source()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            imported.update(alias.asname or alias.name for alias in node.names)
    forbidden = {
        "ReferenceReport",
        "ParsedSource",
        "parse_reference_report",
        "load_reference_projection",
    }

    assert imported.isdisjoint(forbidden)
    for name in forbidden:
        assert re.search(rf"\b{name}\b", source) is None


def test_canonical_module_uses_the_versioned_profile_policy() -> None:
    source = _canonical_source()

    assert "extraction_profile_decision" in source
    assert "extraction_profile_for_tier" not in source
    assert "EXTRACTION_PROFILE_POLICY_VERSION" in source


def test_live_extraction_stage_no_longer_calls_the_legacy_planner() -> None:
    stage = inspect.getsource(
        production_workflow.ProductionWorkflowOrchestrator._execute_extraction_stage
    )

    assert "load_reference_projection" not in stage
    assert "ReferenceReport" not in stage
    assert "service.execute" in stage
    # The retired live-URL Q2 path is gone, not merely unused.
    for retired in (
        "_execute_direct_url_extraction",
        "plan_q2_extraction_profiles",
        "_q2_archive_fallback_identity",
    ):
        assert not hasattr(production_workflow, retired)
        assert not hasattr(production_workflow.ProductionWorkflowOrchestrator, retired)


def test_no_production_module_derives_full_from_the_source_role() -> None:
    application = CANONICAL_MODULE.parent
    for module in application.glob("production_*.py"):
        text = module.read_text(encoding="utf-8")
        assert re.search(r"SourceRole\.PRIMARY[^\n]*ExtractionProfile\.FULL", text) is None, module
        assert re.search(r"ExtractionProfile\.FULL[^\n]*SourceRole\.PRIMARY", text) is None, module


def test_canonical_service_contract_helpers_exist() -> None:
    assert callable(production_extraction.production_extraction_metadata)
    assert production_extraction.source_text_contract_version().startswith(
        production_extraction.SOURCE_TEXT_CONTRACT_VERSION
    )


def test_checkpoint_identity_fits_the_durable_columns() -> None:
    for profile in ExtractionProfile:
        identity = production_extraction.source_checkpoint_identity(
            content_sha256="a" * 64,
            profile=profile,
            prompt_version=production_extraction.source_prompt_version(profile),
        )
        # Every dimension is a String(64) column of uq_source_extractions_identity.
        assert all(len(value) <= 64 for value in identity.values()), identity
