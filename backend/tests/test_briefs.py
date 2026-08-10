from __future__ import annotations

import calendar
from datetime import UTC, date, datetime
from io import BytesIO
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from cti_app.application.briefs import (
    BriefDraftOutput,
    BriefError,
    BriefService,
)
from cti_app.application.model_gateway import ModelExecution
from cti_app.domain.blobs import BlobRecord
from cti_app.domain.briefs import BriefDraft
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    Claim,
    ClaimKind,
    CollectionState,
    Indicator,
    IndicatorKind,
    SourceCollection,
    SourceSpan,
)
from cti_app.domain.discovery import SourceRelationshipStatus, SourceRole
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    CandidateReference,
    EditorialGroup,
    EditorialScore,
    EditorialType,
    GroupingConfidence,
    GroupingOutcome,
    HumanDecision,
    HumanDecisionType,
)
from cti_app.domain.entities import SourceDocument, Subject
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.collection_support import InMemoryCollectionUnitOfWorkFactory


class DraftModel:
    def __init__(self, output: BriefDraftOutput) -> None:
        self.output = output
        self.requests: list[object] = []

    async def draft(self, request: object, output_schema: object | None = None) -> ModelExecution:
        del output_schema
        self.requests.append(request)
        run = ModelRun(
            provider=ModelProvider.QWEN,
            model_role=ModelRole.DRAFTING,
            requested_model="Qwen3-32B",
            prompt_template_id="brief-drafting",
            prompt_template_version="1.0",
            authorized_input_hash="a" * 64,
            evidence_pack_hash="b" * 64,
            parameters={},
        )
        return ModelExecution(run=run, structured_output=self.output)


async def _context(
    tmp_path: Path,
) -> tuple[
    InMemoryCollectionUnitOfWorkFactory, FilesystemBlobStore, Subject, Claim, Indicator, UUID
]:
    factory = InMemoryCollectionUnitOfWorkFactory()
    edition = Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, calendar.monthrange(2026, 7)[1]),
        tlp=TLP.AMBER,
        languages=("fr",),
        target_major_articles=1,
        target_briefs=1,
        source_profile="default",
    )
    subject = Subject(external_id="iran-brief", slug="iran-brief", tlp=TLP.AMBER)
    group = EditorialGroup(
        edition_id=edition.id,
        title="Campagne MuddyWater",
        candidate_references=(CandidateReference(uuid4(), uuid4()),),
        outcome=GroupingOutcome.NEW_SUBJECT,
        score=EditorialScore(2, 2, 2, 2, 2, 2, {"impact": "test"}),
        source_relationship_status=SourceRelationshipStatus.VERIFIED,
        needs_source_verification=False,
        needs_source_expansion=False,
        grouping_confidence=GroupingConfidence.HIGH,
        grouping_justification="Sélection humaine",
    )
    group.select(EditorialType.BRIEF, subject.id)
    store = FilesystemBlobStore(tmp_path / "blobs")
    descriptor = await store.put(
        source=BytesIO(b"MuddyWater cible l'Iran avec evil.example."),
        logical_bucket="source-raw",
        mime_type="text/html",
    )
    blob = BlobRecord(descriptor=descriptor)
    document = SourceDocument(
        subject_id=subject.id,
        blob_id=blob.id,
        original_name="report.html",
        origin="https://research.example/report",
        acquired_at=datetime.now(UTC),
        license_restriction=None,
        tlp=TLP.AMBER,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    collection = SourceCollection(
        subject_id=subject.id,
        edition_id=edition.id,
        group_id=group.id,
        batch_id=uuid4(),
        source_candidate_id=uuid4(),
        requested_url=document.origin,
        proposed_role=SourceRole.PRIMARY,
        state=CollectionState.COMPLETED,
        relationship_status=SourceRelationshipStatus.VERIFIED,
        relationship_evidence="human:dev-analyst",
        source_document_id=document.id,
    )
    claim = Claim(
        subject_id=subject.id,
        edition_id=edition.id,
        group_id=group.id,
        source_document_id=document.id,
        derived_artifact_id=uuid4(),
        kind=ClaimKind.FACT,
        value="MuddyWater cible l'Iran",
        span=SourceSpan(0, 26),
        extraction_method="deterministic-test",
        extraction_payload={},
    )
    indicator = Indicator(
        subject_id=subject.id,
        edition_id=edition.id,
        group_id=group.id,
        source_document_id=document.id,
        derived_artifact_id=claim.derived_artifact_id,
        kind=IndicatorKind.DOMAIN,
        original_value="evil.example",
        normalized_value="evil.example",
        span=SourceSpan(32, 44),
    )
    decisions = [
        HumanDecision(
            edition_id=edition.id,
            decision_type=HumanDecisionType.SELECT,
            group_ids=(group.id,),
            actor_id="dev-analyst",
            correlation_id="test",
            payload={"subject_id": str(subject.id), "editorial_type": "brief"},
        ),
        HumanDecision(
            edition_id=edition.id,
            decision_type=HumanDecisionType.CLAIM_VALIDATE,
            group_ids=(group.id,),
            actor_id="dev-analyst",
            correlation_id="test",
            payload={"claim_id": str(claim.id), "corrected_value": None},
        ),
        HumanDecision(
            edition_id=edition.id,
            decision_type=HumanDecisionType.INDICATOR_VALIDATE,
            group_ids=(group.id,),
            actor_id="dev-analyst",
            correlation_id="test",
            payload={"indicator_id": str(indicator.id), "corrected_value": None},
        ),
    ]
    factory.editions[edition.id] = edition
    factory.subjects[subject.id] = subject
    factory.groups[group.id] = group
    factory.blobs[blob.id] = blob
    factory.documents[document.id] = document
    factory.collections[collection.id] = collection
    factory.claims[claim.id] = claim
    factory.indicators[indicator.id] = indicator
    factory.decisions.extend(decisions)
    return factory, store, subject, claim, indicator, document.id


def _output(claim_id: UUID, indicator_id: UUID, source_id: UUID, *, text: str) -> BriefDraftOutput:
    return BriefDraftOutput.model_validate(
        {
            "title": "MuddyWater cible l'Iran",
            "blocks": [
                {
                    "sentences": [
                        {
                            "text": text,
                            "factual": True,
                            "claim_ids": [str(claim_id)],
                            "indicator_ids": [str(indicator_id)],
                        }
                    ]
                }
            ],
            "limits": ["Attribution non évaluée."],
            "source_ids": [str(source_id)],
        }
    )


async def test_freeze_is_idempotent_and_new_evidence_invalidates_old_draft(
    tmp_path: Path,
) -> None:
    factory, store, subject, claim, indicator, source_id = await _context(tmp_path)
    service = BriefService(
        factory,
        store,
        DraftModel(_output(claim.id, indicator.id, source_id, text="MuddyWater cible l'Iran.")),
    )
    first = await service.freeze(subject.id, actor_id="dev-analyst")
    assert await service.freeze(subject.id, actor_id="dev-analyst") == first
    draft = await service.generate(subject.id, actor_id="dev-analyst")

    factory.decisions.append(
        HumanDecision(
            edition_id=first.edition_id,
            decision_type=HumanDecisionType.CLAIM_CORRECT,
            group_ids=(first.group_id,),
            actor_id="dev-analyst",
            correlation_id="correction",
            payload={"claim_id": str(claim.id), "corrected_value": "MuddyWater vise l'Iran"},
        )
    )
    second = await service.freeze(subject.id, actor_id="dev-analyst")

    assert second.version == 2
    assert second.content_hash != first.content_hash
    assert (await service.qa(draft)).checks["current_evidence_pack"] is False


async def test_generation_rejects_an_ioc_added_by_the_model(tmp_path: Path) -> None:
    factory, store, subject, claim, indicator, source_id = await _context(tmp_path)
    service = BriefService(
        factory,
        store,
        DraftModel(
            _output(
                claim.id,
                indicator.id,
                source_id,
                text="MuddyWater utilise evil.example et 203.0.113.10.",
            )
        ),
    )

    with pytest.raises(BriefError, match="IOC non validé"):
        await service.generate(subject.id, actor_id="dev-analyst")
    assert factory.brief_drafts == {}


async def test_factual_sentence_requires_a_claim_reference(tmp_path: Path) -> None:
    factory, store, subject, claim, indicator, source_id = await _context(tmp_path)
    output = _output(claim.id, indicator.id, source_id, text="MuddyWater cible l'Iran.")
    output.blocks[0].sentences[0].claim_ids = []
    service = BriefService(factory, store, DraftModel(output))

    with pytest.raises(ValueError, match="factual sentence"):
        await service.generate(subject.id, actor_id="dev-analyst")


async def test_approval_and_markdown_export_use_current_pack(tmp_path: Path) -> None:
    factory, store, subject, claim, indicator, source_id = await _context(tmp_path)
    service = BriefService(
        factory,
        store,
        DraftModel(_output(claim.id, indicator.id, source_id, text="MuddyWater cible l'Iran.")),
    )
    draft: BriefDraft = await service.generate(subject.id, actor_id="dev-analyst")
    decision = await service.approve(subject.id, actor_id="dev-analyst", correlation_id="approval")
    markdown = await service.markdown(subject.id)

    assert decision.payload["draft_id"] == str(draft.id)
    assert markdown.startswith("# MuddyWater cible l'Iran")
    assert "https://research.example/report" in markdown
    assert "SHA-256" in markdown
