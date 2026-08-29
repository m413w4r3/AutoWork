"""Deterministic brief rendering and the QA gate that guards READY."""

from __future__ import annotations

from datetime import date
from typing import Any
from uuid import uuid4

from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    ParsedEvent,
    ParsedSource,
    ReferenceReport,
    TechnicalExtraction,
)
from cti_app.application.production_rendering import (
    build_reference_numbering,
    collect_indicators,
    render_brief,
)
from cti_app.application.production_stages import ProductionQAService
from cti_app.application.production_verification import (
    accepted_for_publication,
    project_review_status,
)
from cti_app.domain.collection import ReviewStatus
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication import ArtifactType

RESEARCH_DATE = date(2026, 8, 1)


def _source(local_id: str, url: str, title: str) -> ParsedSource:
    return ParsedSource(
        local_id=local_id,
        title=title,
        url=url,
        canonical_url=url,
        publisher="Example Labs",
        published_at=date(2026, 7, 1),
        role=SourceRole.PRIMARY,
    )


def _report() -> ReferenceReport:
    return ReferenceReport(
        sources=(
            _source("S1", "https://a.example/one", "Rapport un"),
            _source("S2", "https://b.example/two", "Rapport deux"),
        ),
        events=(
            ParsedEvent(
                local_id="R1",
                event_date=date(2026, 7, 1),
                source_ids=("S1",),
                text="Première observation.",
            ),
            ParsedEvent(
                local_id="R2",
                event_date=date(2026, 7, 5),
                source_ids=("S2",),
                text="Extension de la campagne.",
            ),
        ),
    )


def _item(
    category: str,
    value: str,
    artifact_type: str | None = None,
    *,
    confirmed: bool = False,
) -> ExtractionItem:
    return ExtractionItem(
        local_id=f"I-{value}",
        category=category,
        value=value,
        context="ctx",
        artifact_type=ArtifactType(artifact_type) if artifact_type is not None else None,
        attack_id=None,
        reference_ids=("R1",),
        source_ids=("S1",),
        supported=True,
        indicator_status=(
            IndicatorStatus.CONFIRMED_IOC if confirmed else IndicatorStatus.CONTEXTUAL
        ),
        display_policy=DisplayPolicy.IOC_SECTION if confirmed else DisplayPolicy.BODY_ONLY,
    )


def _extraction() -> TechnicalExtraction:
    return TechnicalExtraction(
        items=(
            _item("network_artifacts", "malicious.example.com", "domain", confirmed=True),
            _item("network_artifacts", "Malicious.Example.com", "domain", confirmed=True),
            _item("cves", "CVE-2026-1234"),
        )
    )


SYNTHESIS = "Le groupe agit depuis 2020 [S2]. Une extension est observée [S1]."


def test_numbers_follow_first_use_in_the_synthesis() -> None:
    """AutoWork owns the numbering, and the reader sees it in reading order."""
    numbering = build_reference_numbering(_report(), SYNTHESIS)

    assert numbering == {"S2": 1, "S1": 2}


def test_unknown_marker_never_becomes_a_footnote() -> None:
    numbering = build_reference_numbering(_report(), "Analyse [S9] douteuse.")

    assert "S9" not in numbering


def test_indicators_are_deduplicated_by_kind_and_value() -> None:
    indicators = collect_indicators(_extraction())

    assert [item.value for item in indicators] == [
        "malicious.example.com",
    ]


def test_brief_contains_the_real_content_and_no_placeholder() -> None:
    numbering = build_reference_numbering(_report(), SYNTHESIS)

    brief = render_brief(
        subject_title="TAG-182 et MarkiRAT",
        report=_report(),
        extraction=_extraction(),
        synthesis_text=SYNTHESIS,
        numbering=numbering,
    )

    assert "# TAG-182 et MarkiRAT" in brief
    assert "Première observation." in brief
    assert "Le groupe agit depuis 2020 [1]." in brief
    assert "malicious.example.com" in brief
    assert "https://a.example/one" in brief
    assert "mots)" not in brief
    assert "identifiées" not in brief
    assert "[S1]" not in brief and "[S2]" not in brief


def test_every_footnote_used_is_declared() -> None:
    numbering = build_reference_numbering(_report(), SYNTHESIS)
    brief = render_brief(
        subject_title="T",
        report=_report(),
        extraction=_extraction(),
        synthesis_text=SYNTHESIS,
        numbering=numbering,
    )

    declared = {line.split("]")[0][1:] for line in brief.splitlines() if line.startswith("[")}

    assert declared == {"1", "2"}


# --- QA --------------------------------------------------------------------


def _artifact(stage: ProductionArtifactStage) -> ProductionArtifact:
    return ProductionArtifact(
        production_run_id=uuid4(),
        subject_id=uuid4(),
        stage=stage,
        version=1,
        input_hash="a" * 64,
    )


async def _qa(**overrides: Any) -> dict[str, Any]:
    numbering = build_reference_numbering(_report(), SYNTHESIS)
    payload: dict[str, Any] = {
        "run_id": uuid4(),
        "references_artifact": _artifact(ProductionArtifactStage.REFERENCES),
        "extraction_artifact": _artifact(ProductionArtifactStage.EXTRACTION),
        "synthesis_artifact": _artifact(ProductionArtifactStage.SYNTHESIS),
        "brief_artifact": _artifact(ProductionArtifactStage.BRIEF),
        "report": _report(),
        "extraction": _extraction(),
        "synthesis_text": SYNTHESIS,
        "brief_markdown": render_brief(
            subject_title="T",
            report=_report(),
            extraction=_extraction(),
            synthesis_text=SYNTHESIS,
            numbering=numbering,
        ),
        "archived_urls": {"https://a.example/one", "https://b.example/two"},
        "research_date": RESEARCH_DATE,
    }
    payload.update(overrides)
    return await ProductionQAService(lambda: None).run_qa(**payload)  # type: ignore[arg-type]


async def test_a_complete_brief_passes_qa() -> None:
    result = await _qa()

    assert result["passed"] is True, result["errors"]


async def test_qa_fails_when_a_source_was_never_archived() -> None:
    result = await _qa(archived_urls=set())

    assert result["passed"] is False
    assert "at_least_one_archived_source" in result["checks"]
    assert result["checks"]["at_least_one_archived_source"] is False


async def test_qa_fails_on_an_event_dated_after_the_research() -> None:
    report = ReferenceReport(
        sources=_report().sources,
        events=(
            ParsedEvent(
                local_id="R1",
                event_date=date(2027, 1, 1),
                source_ids=("S1",),
                text="Impossible.",
            ),
        ),
    )

    result = await _qa(report=report)

    assert result["checks"]["no_future_date"] is False


async def test_qa_fails_when_the_synthesis_cites_an_unknown_source() -> None:
    result = await _qa(synthesis_text="Analyse [S9].")

    assert result["checks"]["no_unknown_marker_in_synthesis"] is False


async def test_qa_fails_when_the_synthesis_cites_a_url_outside_the_corpus() -> None:
    result = await _qa(synthesis_text="Voir https://elsewhere.example/x [S1].")

    assert result["checks"]["no_url_outside_corpus"] is False


async def test_qa_fails_when_an_item_cites_an_unknown_reference() -> None:
    extraction = TechnicalExtraction(
        items=(
            ExtractionItem(
                local_id="I1",
                category="actors",
                value="TAG-182",
                context="",
                artifact_type=None,
                attack_id=None,
                reference_ids=("R9",),
                source_ids=(),
                supported=True,
            ),
        )
    )

    result = await _qa(extraction=extraction)

    assert result["checks"]["no_unknown_reference_in_items"] is False


async def test_qa_fails_on_a_stale_artifact() -> None:
    stale = _artifact(ProductionArtifactStage.REFERENCES)
    stale.status = ProductionArtifactStatus.STALE

    result = await _qa(references_artifact=stale)

    assert result["checks"]["no_stale_references"] is False


async def test_qa_fails_on_an_orphan_footnote() -> None:
    result = await _qa(brief_markdown="# T\n\nTexte avec [7] sans déclaration.\n")

    assert result["checks"]["no_orphan_footnote"] is False


# --- machine_verified ------------------------------------------------------


def test_machine_verification_lifts_an_item_out_of_extracted() -> None:
    assert project_review_status(None, machine_verified=True) is ReviewStatus.MACHINE_VERIFIED
    assert project_review_status(None, machine_verified=False) is ReviewStatus.EXTRACTED


def test_a_human_decision_always_wins_over_machine_verification() -> None:
    assert (
        project_review_status(ReviewStatus.REJECTED, machine_verified=True) is ReviewStatus.REJECTED
    )


def test_publication_accepts_verified_evidence_only() -> None:
    assert accepted_for_publication(ReviewStatus.MACHINE_VERIFIED)
    assert accepted_for_publication(ReviewStatus.VALIDATED)
    assert accepted_for_publication(ReviewStatus.CORRECTED)
    assert not accepted_for_publication(ReviewStatus.EXTRACTED)
    assert not accepted_for_publication(ReviewStatus.REJECTED)
