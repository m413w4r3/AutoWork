"""SOURCES -> REFERENCES through the real gateway, for P23.6.

This reproduces the exact production trace from the P23.6 ticket:

    ProductionWorkflowOrchestrator._execute_references_stage
    -> _ask_with_format_repair
    -> ModelConversationService.add_turn
    -> ModelGateway.execute
    -> ModelGateway._execute

`test_production_e2e_olalampo_gateway_integration.py` already routes Q2
through a real `ModelGateway`, but Q1 there still goes through
`_FakeConversations` -- exactly the gap that let the P23.5 regression
through undetected. This module runs SOURCES then REFERENCES with a real
`ModelConversationService` + real `ModelGateway` for Q1, and a scripted fake
OpenAI/bridge adapter. `_FakeConversations` is not used for Q1 in this
module.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import ModelGateway, ModelRouter
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.production import (
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.integrations.models import BlobModelOutputStore, FakeModelAdapter
from tests.conversation_support import InMemoryConversationUnitOfWorkFactory
from tests.test_model_conversations_gateway_integration import _ScriptedBridgeAdapter
from tests.test_production_e2e_olalampo import (
    OLALAMPO_Q1,
    S1_DOC_ID,
    S1_TEXT,
    S1_TITLE,
    S1_URL,
    S2_DOC_ID,
    S2_TEXT,
    S2_TITLE,
    S2_URL,
    S3_DOC_ID,
    S3_TEXT,
    S3_TITLE,
    S3_URL,
)
from tests.test_production_workflow_stages import (
    _ArchiveProcessor,
    _Blobs,
    _CollectionService,
    _Uow,
)


class _CollectionServiceWithSources(_CollectionService):
    """The SOURCES stage additionally calls `list_sources`, which none of
    the existing `_CollectionService` test doubles implement (nothing before
    this module drove SOURCES for real; REFERENCES-only fixtures seed
    `source_collections` directly and start at REFERENCES)."""

    async def list_sources(self, subject_id: UUID) -> list[Any]:
        del subject_id
        return list(self._uow.source_collections.items)


class _JobContext:
    def __init__(self, job_id: UUID) -> None:
        self.job_id = job_id


def _build(
    tmp_path: Path, answer: str
) -> tuple[ProductionWorkflowOrchestrator, _Uow, _ScriptedBridgeAdapter]:
    run = SubjectProductionRun(
        subject_id=UUID(int=400), edition_id=UUID(int=401), profile=ProductionProfile.BRIEF_AUTO
    )
    run.start_running()
    uow = _Uow(run=run)

    documents = (
        (S1_DOC_ID, S1_URL, S1_TITLE),
        (S2_DOC_ID, S2_URL, S2_TITLE),
        (S3_DOC_ID, S3_URL, S3_TITLE),
    )
    blob_ids: dict[UUID, UUID] = {}
    for document_id, url, title in documents:
        artifact_id, blob_id = uuid4(), uuid4()
        blob_ids[document_id] = blob_id
        uow.source_documents.items.append(
            type("Document", (), {"id": document_id, "final_url": url, "title": title})()
        )
        uow.derived_artifacts.items[artifact_id] = type(
            "Artifact",
            (),
            {"id": artifact_id, "text_blob_id": blob_id, "parser_version": "test-parser-1"},
        )()
        uow.source_collections.items.append(
            type(
                "Collection",
                (),
                {
                    "id": document_id,
                    "canonical_url": url,
                    "state": CollectionState.ARCHIVED,
                    "title": title,
                    "publisher": "Example Labs",
                    "published_at": None,
                    "proposed_role": None,
                    "do_not_submit": False,
                    "external_llm_allowed": True,
                    "source_document_id": document_id,
                    "derived_artifact_id": artifact_id,
                    "origin_kind": SourceOriginKind.REFERENCE_RESEARCH,
                    "parent_source_collection_id": None,
                },
            )()
        )

    texts_by_blob = {
        blob_ids[S1_DOC_ID]: S1_TEXT,
        blob_ids[S2_DOC_ID]: S2_TEXT,
        blob_ids[S3_DOC_ID]: S3_TEXT,
    }

    adapter = _ScriptedBridgeAdapter(answer)
    router = ModelRouter(
        openai_research=adapter,
        openai_structured=adapter,
        qwen=FakeModelAdapter(),
        fake=FakeModelAdapter(),
    )
    conversation_uow = InMemoryConversationUnitOfWorkFactory(
        known_subject_ids={run.subject_id}, known_edition_ids={run.edition_id}
    )
    blob_store = FilesystemBlobStore(tmp_path)
    output_store = BlobModelOutputStore(
        BlobCatalogService(blob_store, conversation_uow)  # type: ignore[arg-type]
    )
    gateway = ModelGateway(router, conversation_uow, output_store)  # type: ignore[arg-type]
    model_service = ModelConversationService(
        conversation_uow,  # type: ignore[arg-type]
        gateway,
        blob_store,
    )

    store = ProductionArtifactStore(_Blobs())  # type: ignore[arg-type]
    archive_processor = _ArchiveProcessor(texts_by_blob)
    orchestrator = ProductionWorkflowOrchestrator(
        lambda: uow,  # type: ignore[arg-type]
        model_service=model_service,
        collection_service=_CollectionServiceWithSources(uow),  # type: ignore[arg-type]
        artifact_store=store,
        source_evidence_processor=archive_processor,  # type: ignore[arg-type]
    )
    return orchestrator, uow, adapter


async def test_sources_then_references_reaches_the_real_gateway_exactly_once(
    tmp_path: Path,
) -> None:
    """Regression for P23.6: this is the exact production trace -- SOURCES
    success immediately followed by a REFERENCES turn against a freshly
    persisted (RUNNING/NOT_SUBMITTED) ModelRun. Before the fix, the second
    stage never reached the adapter: `ModelGateway._execute` raised
    'Model run needs reconciliation before resubmission' on the very first
    call, because it treated the pre-persisted RUNNING run as a dangerous
    replay."""
    orchestrator, uow, adapter = _build(tmp_path, OLALAMPO_Q1)

    sources_result = await orchestrator.execute_stage(
        uow.run.id,
        SubjectProductionStage.SOURCES,
        context=_JobContext(uow.run.id),  # type: ignore[arg-type]
        correlation_id="p23-6-repro",
    )
    assert sources_result["status"] == "success", sources_result

    uow.run.current_stage = SubjectProductionStage.REFERENCES
    references_result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="p23-6-repro"
    )

    assert references_result["status"] == "success", references_result
    assert len(adapter.calls) == 1
    assert adapter.calls[0].web_search is True
