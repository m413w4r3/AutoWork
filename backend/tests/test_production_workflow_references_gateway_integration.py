"""SOURCES -> REFERENCES -> EXTRACTION through the real gateway, for P23.6/P23.7.

This reproduces the exact production trace from the P23.6 ticket:

    ProductionWorkflowOrchestrator._execute_references_stage
    -> _ask_with_format_repair
    -> ModelConversationService.add_turn
    -> ModelGateway.execute
    -> ModelGateway._execute

and, since P23.7, the same trace for Q2 (`_execute_extraction_stage`) --
Q2 moved off the OpenAI structured-output contract (the bridge does not
actually guarantee response_format / JSON Schema) onto the same free-text
`add_turn` + Markdown-parser path Q1 already used, one bounded conversation
per `EvidenceChunk`. This module runs SOURCES then REFERENCES then EXTRACTION
with a real `ModelConversationService` + real `ModelGateway`, and a scripted
fake OpenAI/bridge adapter. `_FakeConversations` is not used anywhere here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ConversationResult,
    ModelGateway,
    ModelGatewayError,
    ModelRouter,
    SafeModelRequest,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelUsage
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
    OLALAMPO_Q2_S1,
    OLALAMPO_Q2_S2,
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


class _DispatchingBridgeAdapter:
    """Like `_ScriptedBridgeAdapter`, but answers per-call based on which
    marker string is found in the request text -- needed once more than one
    distinct chunk conversation is in flight (Q2 opens one per chunk)."""

    provider = ModelProvider.OPENAI
    requested_model = "chatgpt-web"
    is_external = True

    def __init__(self, answers: list[tuple[str, str]]) -> None:
        self._answers = answers
        self.calls: list[SafeModelRequest] = []

    async def invoke(
        self, request: SafeModelRequest, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        del output_schema
        self.calls.append(request)
        context = request.conversation
        assert context is not None
        answer = next((a for marker, a in self._answers if marker in request.text), None)
        if answer is None:
            raise AssertionError(f"No scripted answer for: {request.text[:200]!r}")
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=3, output_tokens=5, total_tokens=8),
            output_text=answer,
            conversation=ConversationResult(
                id=str(context.id),
                mode=context.mode,
                external_locator="https://chatgpt.com/opaque/scripted",
                turn_id=f"bridge-turn-{len(self.calls)}",
                verified=True,
            ),
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: Any = None
    ) -> AdapterResult:
        raise ModelGatewayError("Scripted bridge adapter does not support background resume")


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


_AnyAdapter = _ScriptedBridgeAdapter | _DispatchingBridgeAdapter


def _build(
    tmp_path: Path, adapter: _AnyAdapter
) -> tuple[ProductionWorkflowOrchestrator, _Uow, _AnyAdapter]:
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
    orchestrator, uow, adapter = _build(tmp_path, _ScriptedBridgeAdapter(OLALAMPO_Q1))

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


async def test_q2_uses_bounded_openai_conversations_per_chunk_through_real_gateway(
    tmp_path: Path,
) -> None:
    """P23.7, section 11: Q2 uses OpenAI, `web_search=False`, and each
    `EvidenceChunk` gets its own bounded conversation through the real
    `ModelGateway` -- never Q1's, never another chunk's. A same-identity
    re-run of a fully-succeeded EXTRACTION then hits the artifact-level
    checkpoint before the chunk loop even runs again: zero new adapter
    calls."""
    adapter = _DispatchingBridgeAdapter(
        [
            # Body-only fragments, never a source title: Q1's prompt lists
            # already-archived source titles (S2_TITLE == "Appendix:
            # Indicators of Compromise"), so a marker built from that title
            # would false-match Q1's own prompt.
            ("Operation Olalampo used a modern update infrastructure", OLALAMPO_Q2_S1),
            ("The following indicators were catalogued during investigation", OLALAMPO_Q2_S2),
            ("", OLALAMPO_Q1),  # catch-all: anything else is Q1's prompt
        ]
    )
    orchestrator, uow, _ = _build(tmp_path, adapter)

    await orchestrator.execute_stage(
        uow.run.id,
        SubjectProductionStage.SOURCES,
        context=_JobContext(uow.run.id),  # type: ignore[arg-type]
        correlation_id="p23-7-q2",
    )
    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES, correlation_id="p23-7-q2"
    )
    assert len(adapter.calls) == 1  # Q1 only, so far

    uow.run.current_stage = SubjectProductionStage.EXTRACTION
    first = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.EXTRACTION, correlation_id="p23-7-q2"
    )
    assert first["status"] == "success", first

    # Exactly Q1 + one draft turn per chunk (S1, S2) -- no repair needed since
    # both canned answers are already valid Markdown.
    assert len(adapter.calls) == 3
    # All three calls reached this adapter at all, which the router only ever
    # does for provider=openai (Qwen/fake route to `FakeModelAdapter`).
    q1_call, s1_call, s2_call = adapter.calls
    assert q1_call.web_search is True
    assert s1_call.web_search is False
    assert s2_call.web_search is False
    # Three distinct bounded conversations: Q1's, chunk S1's, chunk S2's.
    conversation_ids = {call.conversation.id for call in adapter.calls if call.conversation}
    assert len(conversation_ids) == 3

    uow.run.current_stage = SubjectProductionStage.EXTRACTION
    second = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.EXTRACTION, correlation_id="p23-7-q2"
    )
    assert second["status"] == "cached"
    assert second["artifact_id"] == first["artifact_id"]
    assert len(adapter.calls) == 3  # no new bridge call at all
