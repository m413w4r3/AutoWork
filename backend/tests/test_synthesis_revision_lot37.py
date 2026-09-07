"""LOT 37: deterministic Q4 revision context and prompt contract."""

from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    ReferenceReport,
    TechnicalExtraction,
)
from cti_app.application.production_prompts import ProductionPromptTemplates
from cti_app.application.production_synthesis_revision import (
    build_synthesis_revision_context,
    narrative_repair_keys,
    synthesis_content_hash,
    synthesis_semantic_source_ids,
)
from cti_app.application.production_workflow import (
    ProductionWorkflowOrchestrator,
    _synthesis_input_hash,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    SubjectProductionRun,
    SubjectProductionStage,
    SynthesisMode,
)


def test_revision_delta_is_sorted_and_content_based() -> None:
    previous_id = uuid4()
    context = build_synthesis_revision_context(
        previous_artifact_id=previous_id,
        previous_input_hash="a" * 64,
        previous_text="Ancien draft [S1].",
        previous_semantic_hash="b" * 64,
        current_semantic_hash="c" * 64,
        previous_source_ids=("S2", "S1"),
        current_source_ids=("S3", "S1"),
        previous_repair_keys=("2" * 64,),
        current_repair_keys=("3" * 64,),
    )

    assert context.previous_artifact_id == previous_id
    assert context.added_source_ids == ("S3",)
    assert context.removed_source_ids == ("S2",)
    assert context.added_repair_keys == ("3" * 64,)
    assert context.removed_repair_keys == ("2" * 64,)


def test_publication_only_ioc_and_rules_never_enter_narrative_repair_delta() -> None:
    publication_key = "1" * 64
    narrative_key = "2" * 64
    rule_key = "3" * 64
    extraction = TechnicalExtraction(
        items=(
            ExtractionItem(
                local_id=f"RPA-{publication_key[:16]}",
                category="network_artifacts",
                value="198.51.100.10",
                context="",
                artifact_type=None,
                attack_id=None,
                reference_ids=(),
                source_ids=("S1",),
                supported=True,
                indicator_status=IndicatorStatus.CONFIRMED_IOC,
                provenance=IndicatorProvenance.ANALYST,
                display_policy=DisplayPolicy.IOC_SECTION,
            ),
            ExtractionItem(
                local_id=f"RPA-{narrative_key[:16]}",
                category="tools",
                value="tool.exe",
                context="execution",
                artifact_type=None,
                attack_id=None,
                reference_ids=(),
                source_ids=("S1",),
                supported=True,
                display_policy=DisplayPolicy.BODY_ONLY,
            ),
        )
    )

    keys = narrative_repair_keys(
        extraction,
        {"repair_projection": {"included_repair_keys": [publication_key, narrative_key, rule_key]}},
    )

    assert keys == (narrative_key,)


def test_semantic_source_ids_ignore_unreferenced_publication_only_source() -> None:
    pack = {
        "reference_report": {
            "sources": [{"id": "S1"}, {"id": "S2"}],
            "events": [{"source_ids": ["S1"]}],
        },
        "technical_extraction": {
            "items": [{"source_ids": ["S1"]}],
        },
    }

    assert synthesis_semantic_source_ids(pack) == ("S1",)


def test_revision_prompt_keeps_previous_draft_non_authoritative() -> None:
    context = build_synthesis_revision_context(
        previous_artifact_id=uuid4(),
        previous_input_hash="a" * 64,
        previous_text="Ancien fait devenu unsupported [S2].",
        previous_semantic_hash="b" * 64,
        current_semantic_hash="c" * 64,
        previous_source_ids=("S2",),
        current_source_ids=("S1",),
    )
    prompt = ProductionPromptTemplates.get_synthesis_prompt(
        "Sujet",
        '{"reference_report": {"sources": [{"id": "S1"}]}}',
        revision_context=context,
    )

    assert "PREVIOUS_DRAFT_NON_AUTHORITATIVE" in prompt
    assert "Produce a complete synthesis, never a patch" in prompt
    assert "Current evidence is the sole authority" in prompt
    assert "Remove every old fact" in prompt
    assert "IOC/rules publication-only material must not be forced into the prose" in prompt
    assert "Ancien fait devenu unsupported [S2]." in prompt


def test_revision_hash_uses_previous_content_not_artifact_identity() -> None:
    common = {
        "subject_id": uuid4(),
        "references_hash": "a" * 64,
        "reference_report_hash": "b" * 64,
        "extraction_hash": "c" * 64,
        "technical_extraction_hash": "c" * 64,
        "synthesis_evidence_pack_hash": "d" * 64,
        "previous_synthesis_content_hash": synthesis_content_hash("same draft"),
        "current_synthesis_semantic_hash": "e" * 64,
    }

    assert _synthesis_input_hash(**common) == _synthesis_input_hash(**common)
    assert _synthesis_input_hash(
        **{**common, "previous_synthesis_content_hash": synthesis_content_hash("other draft")}
    ) != _synthesis_input_hash(**common)


class _Diagnostics:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def record(self, **values: Any) -> None:
        self.events.append(values)


def _orchestrator(store: object) -> ProductionWorkflowOrchestrator:
    """A bare orchestrator: only the draft store and diagnostics are exercised."""
    orchestrator = ProductionWorkflowOrchestrator(
        cast(Any, SimpleNamespace()),
        artifact_store=cast(Any, store),
    )
    orchestrator._diagnostics = cast(Any, _Diagnostics())
    return orchestrator


def _events(orchestrator: ProductionWorkflowOrchestrator) -> list[dict[str, Any]]:
    return cast(_Diagnostics, orchestrator._diagnostics).events


class _DraftStore:
    def __init__(self, texts: dict[UUID, str]) -> None:
        self._texts = texts

    async def read_text(self, blob_id: UUID) -> str:
        return self._texts[blob_id]


class _ArtifactsUow:
    def __init__(self, artifacts: list[ProductionArtifact]) -> None:
        self._artifacts = artifacts
        self.production_artifacts = SimpleNamespace(list_for_run=self._list_for_run)

    async def _list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self._artifacts if item.production_run_id == run_id]


def _synthesis_artifact(
    *,
    run: SubjectProductionRun,
    blob_id: UUID,
    semantic_hash: str,
    source_ids: tuple[str, ...],
    status: ProductionArtifactStatus = ProductionArtifactStatus.VERIFIED,
    stale_cause: str | None = None,
) -> ProductionArtifact:
    metadata: dict[str, Any] = {
        "semantic_source_ids": list(source_ids),
        "semantic_repair_keys": [],
        "semantic_projection_hash": semantic_hash,
    }
    if stale_cause is not None:
        metadata["stale_cause"] = stale_cause
    return ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="a" * 64,
        status=status,
        rendered_blob_id=blob_id,
        metadata=metadata,
    )


def _run() -> SubjectProductionRun:
    return SubjectProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        current_stage=SubjectProductionStage.SYNTHESIS,
    )


_EMPTY_REPORT = ReferenceReport(sources=(), events=())
_PACK: dict[str, Any] = {
    "reference_report": {"events": [{"source_ids": ["S1", "S2"]}]},
    "technical_extraction": {"items": []},
}


@pytest.mark.asyncio
async def test_no_previous_synthesis_yields_a_fresh_q4_with_no_revision_context() -> None:
    run = _run()
    orchestrator = _orchestrator(_DraftStore({}))
    candidate = await orchestrator._find_revision_candidate(
        uow=_ArtifactsUow([]),
        run=run,
        report=_EMPTY_REPORT,
        extraction_artifact=ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash="b" * 64,
        ),
        extraction_payload=TechnicalExtraction(items=()),
        synthesis_pack=_PACK,
        source_tiers_by_url={},
        current_semantic_hash="c" * 64,
    )
    assert candidate is None

    orchestrator._record_synthesis_mode(
        run=run,
        mode=SynthesisMode.FRESH,
        previous_artifact_id=None,
        previous_word_count=0,
        context=None,
    )
    event = _events(orchestrator)[-1]
    assert event["mode"] == "fresh"
    assert event["previous_synthesis_artifact_id"] is None
    assert event["previous_synthesis_sha256"] is None
    assert event["semantic_projection_changed"] is False


@pytest.mark.asyncio
async def test_readable_previous_draft_becomes_a_non_authoritative_revision_context() -> None:
    run = _run()
    blob_id = uuid4()
    draft = "Un ancien brouillon citant [S1]."
    orchestrator = _orchestrator(_DraftStore({blob_id: draft}))
    previous = _synthesis_artifact(
        run=run,
        blob_id=blob_id,
        semantic_hash="d" * 64,
        source_ids=("S1",),
    )
    extraction_artifact = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="b" * 64,
    )

    found = await orchestrator._find_revision_candidate(
        uow=_ArtifactsUow([previous, extraction_artifact]),
        run=run,
        report=_EMPTY_REPORT,
        extraction_artifact=extraction_artifact,
        extraction_payload=TechnicalExtraction(items=()),
        synthesis_pack=_PACK,
        source_tiers_by_url={},
        current_semantic_hash="e" * 64,
    )

    assert found is not None
    artifact, context = found
    assert artifact.id == previous.id
    assert context.previous_text == draft
    # S2 entered the narrative projection; S1 was already there.
    assert context.added_source_ids == ("S2",)
    assert context.removed_source_ids == ()
    assert context.previous_semantic_hash == "d" * 64
    assert context.current_semantic_hash == "e" * 64

    # The prompt sent for that context is the complete synthesis prompt plus the
    # draft as explicitly non-authoritative material.
    prompt = ProductionPromptTemplates.get_synthesis_prompt(
        subject_title="Sujet",
        synthesis_evidence_pack="{}",
        revision_context=context,
    )
    assert prompt.startswith(
        ProductionPromptTemplates.get_synthesis_prompt(
            subject_title="Sujet", synthesis_evidence_pack="{}"
        )
    )
    assert "PREVIOUS_DRAFT_NON_AUTHORITATIVE" in prompt
    assert draft in prompt

    orchestrator._record_synthesis_mode(
        run=run,
        mode=SynthesisMode.REVISE_PREVIOUS,
        previous_artifact_id=context.previous_artifact_id,
        previous_word_count=len(draft.split()),
        context=context,
    )
    event = _events(orchestrator)[-1]
    assert event["mode"] == "revise_previous"
    assert event["previous_synthesis_artifact_id"] == str(previous.id)
    assert event["previous_synthesis_sha256"] == synthesis_content_hash(draft)
    assert event["semantic_projection_changed"] is True
    assert event["semantic_delta_added_sources"] == ["S2"]


@pytest.mark.asyncio
async def test_unreadable_or_unrelated_stale_drafts_are_never_revised() -> None:
    run = _run()
    missing_blob = uuid4()
    unrelated = _synthesis_artifact(
        run=run,
        blob_id=uuid4(),
        semantic_hash="d" * 64,
        source_ids=("S1",),
        status=ProductionArtifactStatus.STALE,
        stale_cause="model_output_rejected",
    )
    unreadable = _synthesis_artifact(
        run=run,
        blob_id=missing_blob,
        semantic_hash="d" * 64,
        source_ids=("S1",),
    )
    orchestrator = _orchestrator(_DraftStore({}))

    candidate = await orchestrator._find_revision_candidate(
        uow=_ArtifactsUow([unrelated, unreadable]),
        run=run,
        report=_EMPTY_REPORT,
        extraction_artifact=ProductionArtifact(
            production_run_id=run.id,
            subject_id=run.subject_id,
            stage=ProductionArtifactStage.EXTRACTION,
            version=1,
            input_hash="b" * 64,
        ),
        extraction_payload=TechnicalExtraction(items=()),
        synthesis_pack=_PACK,
        source_tiers_by_url={},
        current_semantic_hash="e" * 64,
    )
    # A draft whose bytes cannot be read is not a draft, and a row made stale
    # for an unrelated reason is not a trustworthy one either.
    assert candidate is None
