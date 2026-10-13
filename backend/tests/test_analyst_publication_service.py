from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from cti_app.application.analyst_opinion_service import OpinionError, OpinionErrorCode
from cti_app.application.analyst_publication_service import AnalystPublicationService
from cti_app.application.publication_augmentation import PublicationAugmentationArtifactService
from cti_app.domain.analyst_opinion import AnalystOpinionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from tests.test_analyst_opinion_service import make_opinion_harness


def _publication_service(harness):
    augmentation = PublicationAugmentationArtifactService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
    )
    return AnalystPublicationService(
        uow_factory=harness.uow_factory,  # type: ignore[arg-type]
        artifact_store=harness.store,  # type: ignore[arg-type]
        augmentation_service=augmentation,
    )


@pytest.mark.asyncio
async def test_publication_is_idempotent_and_preserves_the_previous_v5_blob() -> None:
    harness = make_opinion_harness()
    draft = await harness.service.compose_from_findings(harness.investigation.id)
    approved = await harness.service.approve(
        draft.id,
        draft.version,
        draft.content_sha256,
        "editor:one",
        "Citations and reservations checked",
        idempotency_key="approval-1",
    )
    service = _publication_service(harness)

    artifact = await service.publish_augmented_article(
        approved.id,
        approved.version,
        "editor:one",
        approved.base_publication_sha256,
    )
    replay = await service.publish_augmented_article(
        approved.id,
        approved.version,
        "editor:one",
        approved.base_publication_sha256,
    )

    assert replay == artifact
    assert artifact.stage is ProductionArtifactStage.PUBLICATION
    assert artifact.status is ProductionArtifactStatus.VERIFIED
    assert artifact.version == harness.base_artifact.version + 1
    assert artifact.metadata["previous_publication_artifact_id"] == str(harness.base_artifact.id)
    assert harness.store.blobs[harness.base_artifact.canonical_blob_id] == harness.base_bytes
    assert len(harness.artifacts.appended) == 1
    assert harness.uow.composition_writes == 0
    assert harness.uow.manifest_writes == 0


@pytest.mark.asyncio
async def test_publication_refuses_unapproved_stale_base_and_missing_citations() -> None:
    harness = make_opinion_harness()
    draft = await harness.service.compose_from_findings(harness.investigation.id)
    service = _publication_service(harness)

    with pytest.raises(OpinionError) as unapproved:
        await service.publish_augmented_article(
            draft.id,
            draft.version,
            "editor:one",
            draft.base_publication_sha256,
        )
    assert unapproved.value.code is OpinionErrorCode.OPINION_NOT_APPROVED

    approved = await harness.service.approve(
        draft.id,
        draft.version,
        draft.content_sha256,
        "editor:one",
        "Reviewed",
    )
    with pytest.raises(OpinionError) as changed_sha:
        await service.publish_augmented_article(
            approved.id,
            approved.version,
            "editor:one",
            "0" * 64,
        )
    assert changed_sha.value.code is OpinionErrorCode.STALE_BASE

    replacement = ProductionArtifact(
        id=uuid4(),
        production_run_id=harness.base_artifact.production_run_id,
        subject_id=harness.base_artifact.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=harness.base_artifact.version + 1,
        input_hash=hashlib.sha256(b"replacement base").hexdigest(),
        canonical_blob_id=uuid4(),
        status=ProductionArtifactStatus.VERIFIED,
    )
    harness.artifacts.items.append(replacement)
    with pytest.raises(OpinionError) as stale_base:
        await service.publish_augmented_article(
            approved.id,
            approved.version,
            "editor:one",
            approved.base_publication_sha256,
        )
    assert stale_base.value.code is OpinionErrorCode.STALE_BASE

    harness.artifacts.items.remove(replacement)
    harness.observations.items = ()
    with pytest.raises(OpinionError) as missing_evidence:
        await service.publish_augmented_article(
            approved.id,
            approved.version,
            "editor:one",
            approved.base_publication_sha256,
        )
    assert missing_evidence.value.code is OpinionErrorCode.CITATIONS_INVALID


@pytest.mark.asyncio
async def test_request_changes_is_an_append_only_note_decision() -> None:
    harness = make_opinion_harness()
    draft = await harness.service.compose_from_findings(harness.investigation.id)

    requested = await harness.service.request_changes(
        draft.id,
        draft.version,
        draft.content_sha256,
        "editor:reviewer",
        "Clarify that execution was not observed",
        idempotency_key="request-changes-1",
    )

    assert requested.status is AnalystOpinionStatus.REJECTED
    assert requested.version == draft.version + 1
    assert len(harness.decisions.items) == 1
    assert harness.decisions.items[0].decision_type.value == "note_changes_requested"
    assert harness.decisions.items[0].target_type.value == "note"
