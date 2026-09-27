"""AW-011 extraction policy: tier-driven profiles and the architecture cutover.

The tier of the frozen ``ProductionReferenceCorpusV1`` is the only authority
for FULL vs IOC_RULES. These tests lock that policy and the architecture guard
that keeps the canonical extraction module free of the retired REFERENCES
wire-format dependencies.
"""

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
    extraction_profile_for_tier,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ExtractionProfile,
)
from cti_app.domain.production_references import (
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
    state: CollectionState = CollectionState.ARCHIVED,
) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=tier,
        kind=ProductionReferenceKind.PUBLICATION,
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


TIER_POLICY = (
    (ProductionReferenceTier.CORE, SourceRole.PRIMARY, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.INDEPENDENT, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.RELAY, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.AGGREGATOR, ExtractionProfile.FULL),
    (ProductionReferenceTier.CORE, SourceRole.UNKNOWN, ExtractionProfile.FULL),
    (ProductionReferenceTier.SUPPORTING, SourceRole.PRIMARY, ExtractionProfile.IOC_RULES),
    (ProductionReferenceTier.SUPPORTING, SourceRole.INDEPENDENT, ExtractionProfile.IOC_RULES),
    (ProductionReferenceTier.TECHNICAL, SourceRole.PRIMARY, ExtractionProfile.IOC_RULES),
    (ProductionReferenceTier.TECHNICAL, SourceRole.UNKNOWN, ExtractionProfile.IOC_RULES),
)


@pytest.mark.parametrize(("tier", "role", "expected"), TIER_POLICY)
def test_profile_is_decided_by_tier_never_by_role(
    tier: ProductionReferenceTier,
    role: SourceRole,
    expected: ExtractionProfile,
) -> None:
    assert extraction_profile_for_tier(tier) is expected

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
        ),
    )
    plan = build_extraction_plan(corpus)

    profiles = {source.canonical_url: source.profile for source in plan.sources}

    assert profiles[f"https://example.test/{tier.value}-{role.value}"] is expected


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

    assert plan.profile_policy_version == "production-reference-tier-v1"


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


def test_canonical_module_never_decides_the_profile_from_source_role() -> None:
    source = _canonical_source()

    # No role-keyed FULL decision may exist: the tier is the only authority.
    assert re.search(r"SourceRole\.PRIMARY\s*(?:is|==)\s*", source) is None
    assert re.search(r"role\s*(?:is|==)\s*SourceRole\.PRIMARY", source) is None
    assert "extraction_profile_for_tier" in source


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
