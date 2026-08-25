"""Gateway-integration layer for the Olalampo E2E fixture.

`test_production_e2e_olalampo.py` drives the whole production pipeline
through `_FakeConversations`, a hand-rolled duck-typed stand-in that never
touches a real `ModelGateway` and therefore never actually persists or fails
a `ModelRun`. That is fine for asserting workflow-level behaviour (routing,
verification, rendering), but it cannot answer the question this module
exists to answer: what does the real gateway actually persist for Q2, and
what does it actually allow to be retried?

This module keeps the same fixture text (Q1/Q4 still flow through
`_FakeConversations`, unchanged) but routes Q2 (structured extraction)
through the real `ModelGateway` + `ModelConversationService.extract_structured`,
backed by the existing in-memory `ModelRun` repository
(`InMemoryModelRunUnitOfWorkFactory`) and a scripted `ModelAdapter` standing
in for the provider transport. `ModelConversationService.extract_structured`
never touches its `uow_factory`/`blob_store` constructor arguments (it only
calls `self._gateway.extract(...)`), so those can be left unused rather than
standing up a full conversation-persistence stack for this.

Real gateway retry policy for Q2 (`ModelGateway._execute`, backend/src/
cti_app/application/model_gateway.py), as encoded by the tests below:

  * SUCCEEDED + a schema-valid persisted output -> checkpoint hit. The
    adapter is never called again; the archived raw output is replayed and
    re-validated against the schema (`_persisted_execution`).
  * WAITING_BACKGROUND / RUNNING / NEEDS_REVIEW -> "needs reconciliation
    before resubmission" (`ModelGatewayError`), never silently resubmitted.
  * BLOCKED -> never resubmitted.
  * FAILED -> resubmission requires BOTH `allow_failed_resubmit=True` on the
    caller's request AND `run.submission_state is
    ModelSubmissionState.NOT_SUBMITTED`. The production Q2 extraction loop
    (`ProductionWorkflowOrchestrator._execute_extraction_stage`) never sets
    `allow_failed_resubmit`, and `_execute` marks every fresh run
    `SUBMITTED_OR_UNKNOWN` (`mark_submission_uncertain`) *before* it ever
    calls the adapter — so through that call path a FAILED Q2 chunk is
    *never* auto-retried, whether the underlying failure was "the provider
    replied but the JSON didn't match the schema" or "the transport errored
    after the request was already sent". Both leave the run FAILED with the
    same submission_state, and the extraction stage surfaces `needs_review`
    with `error_code == "q2_chunk_coverage_failed"` on every subsequent
    retry rather than re-posting the prompt.

  The `allow_failed_resubmit` + `NOT_SUBMITTED` escape hatch exists in the
  domain (`ModelRun.restart_after_certain_pre_submission_failure`) for a
  caller with independent, out-of-band proof that a *specific* run's request
  never reached the provider (e.g. a reconciliation tool). Nothing on the Q2
  extraction path can produce that proof today, so it is exercised here
  directly against the gateway rather than pretending the chunk loop can
  reach it.
"""

from __future__ import annotations

import hashlib
from typing import Any, cast
from uuid import UUID, uuid4

from cti_app.application.model_conversations import ModelConversationService
from cti_app.application.model_gateway import (
    AdapterResult,
    AdapterResultStatus,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRouter,
    ModelRoutingHint,
    StructuredModelUnavailableError,
    validate_structured_output,
)
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_evidence_pack import EvidenceChunk, ProductionEvidencePack
from cti_app.application.production_parsers import Q2ChunkOutput
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.application.source_evidence_processing import SourceEvidenceProcessingService
from cti_app.domain.collection import CollectionState, SourceOriginKind
from cti_app.domain.model_runs import (
    ModelProvider,
    ModelRole,
    ModelRunStatus,
    ModelSubmissionState,
    ModelUsage,
)
from cti_app.domain.production import (
    ProductionProfile,
    SubjectProductionRun,
    SubjectProductionStage,
)
from cti_app.integrations.models import InMemoryModelOutputStore
from tests.model_support import InMemoryModelRunUnitOfWorkFactory
from tests.test_production_e2e_olalampo import (
    OLALAMPO_Q1,
    OLALAMPO_Q2_S1,
    OLALAMPO_Q2_S2,
    OLALAMPO_Q4_DRAFT,
    OLALAMPO_Q4_REPAIR,
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
    _FakeConversations,
    _Uow,
)


class _ScriptedStructuredAdapter:
    """Stands in for `QwenAdapter`'s transport for Q2 (structured-extraction)
    calls, matching its real shape: schema validation happens *inside*
    `invoke()` and can raise before the method ever returns. That is what
    makes the gateway record the run as FAILED with `submission_state ==
    SUBMITTED_OR_UNKNOWN` — the request was already marked uncertain before
    this call — exactly as it would for a real provider response that turned
    out not to be valid JSON.
    """

    provider = ModelProvider.QWEN
    requested_model = "stub-qwen-structured-v1"
    is_external = False

    def __init__(self, script: list[tuple[str, str | Exception]]) -> None:
        self._script = list(script)
        self.calls: list[Any] = []

    async def invoke(
        self, request: Any, *, role: ModelRole, output_schema: type[Any] | None = None
    ) -> AdapterResult:
        self.calls.append(request)
        for marker, scripted_behavior in self._script:
            if marker in request.text:
                behavior = scripted_behavior
                break
        else:
            raise AssertionError(
                f"No scripted behavior matches request text: {request.text[:120]!r}"
            )
        if isinstance(behavior, Exception):
            raise behavior
        assert output_schema is not None
        structured = validate_structured_output(behavior, output_schema)
        return AdapterResult(
            status=AdapterResultStatus.COMPLETED,
            provider=self.provider,
            requested_model=self.requested_model,
            actual_model_version=self.requested_model,
            usage=ModelUsage(input_tokens=1, output_tokens=1, total_tokens=2),
            output_text=None,
            structured_output=structured,
        )

    async def resume(
        self, response_id: str, *, role: ModelRole, output_schema: type[Any] | None = None
    ) -> AdapterResult:
        raise ModelGatewayError("Stub adapter does not support background resume")


class _GatewayBackedConversations:
    """Q1/Q4 still flow through `_FakeConversations` unchanged; Q2
    (`extract_structured`) flows through a real `ModelGateway` +
    `ModelConversationService`, backed by the existing in-memory ModelRun
    repository and a scripted adapter — so Q2's checkpoint/retry behaviour is
    the gateway's real policy, not an approximation of it.
    """

    def __init__(
        self, turn_answers: list[str], structured_adapter: _ScriptedStructuredAdapter
    ) -> None:
        self._turns = _FakeConversations(turn_answers)
        self.run_uow_factory = InMemoryModelRunUnitOfWorkFactory()
        router = ModelRouter(
            openai_research=structured_adapter,
            openai_structured=structured_adapter,
            qwen=structured_adapter,
            fake=structured_adapter,
        )
        self.gateway = ModelGateway(router, self.run_uow_factory, InMemoryModelOutputStore())
        self._extraction_service = ModelConversationService(
            uow_factory=None,  # type: ignore[arg-type]  # unused by extract_structured
            gateway=self.gateway,
            blob_store=None,  # type: ignore[arg-type]  # unused by extract_structured
        )

    async def create(self, **kwargs: Any) -> Any:
        return await self._turns.create(**kwargs)

    async def add_turn(self, *args: Any, **kwargs: Any) -> Any:
        return await self._turns.add_turn(*args, **kwargs)

    async def turns(self, *args: Any, **kwargs: Any) -> Any:
        return await self._turns.turns(*args, **kwargs)

    async def extract_structured(self, **kwargs: Any) -> Any:
        return await self._extraction_service.extract_structured(**kwargs)

    @property
    def turn_requests(self) -> list[dict[str, Any]]:
        return self._turns.turn_requests


def _fixed_two_chunks() -> tuple[EvidenceChunk, EvidenceChunk]:
    return (
        EvidenceChunk(
            source_document_id=S1_DOC_ID,
            parent_source_ids=(),
            source_ids=("S1",),
            title=S1_TITLE,
            origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
            chunk_id="olalampo-gw-chunk-s1",
            text=S1_TEXT,
            sha256=hashlib.sha256(S1_TEXT.encode()).hexdigest(),
        ),
        EvidenceChunk(
            source_document_id=S2_DOC_ID,
            parent_source_ids=(),
            source_ids=("S2",),
            title=S2_TITLE,
            origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
            chunk_id="olalampo-gw-chunk-s2",
            text=S2_TEXT,
            sha256=hashlib.sha256(S2_TEXT.encode()).hexdigest(),
        ),
    )


def _build_gateway_run(
    structured_adapter: _ScriptedStructuredAdapter,
) -> tuple[ProductionWorkflowOrchestrator, _Uow, _GatewayBackedConversations]:
    run = SubjectProductionRun(
        subject_id=UUID(int=200), edition_id=UUID(int=201), profile=ProductionProfile.BRIEF_AUTO
    )
    run.start_running()
    uow = _Uow(run=run)
    conversations = _GatewayBackedConversations([OLALAMPO_Q1], structured_adapter)
    orchestrator = ProductionWorkflowOrchestrator(
        lambda: uow,  # type: ignore[arg-type]
        model_service=conversations,  # type: ignore[arg-type]
        collection_service=_CollectionService(uow),  # type: ignore[arg-type]
        artifact_store=ProductionArtifactStore(_Blobs()),  # type: ignore[arg-type]
        source_evidence_processor=cast(SourceEvidenceProcessingService, _ArchiveProcessor({})),
    )
    return orchestrator, uow, conversations


def _build_olalampo_gateway(
    structured_adapter: _ScriptedStructuredAdapter,
) -> tuple[
    ProductionWorkflowOrchestrator, _Uow, _GatewayBackedConversations, ProductionArtifactStore
]:
    """Full three-document Olalampo fixture (including the never-cited WHOIS
    page), wired to the real gateway for Q2 exactly like `_build_gateway_run`
    but through the actual archive/evidence-pack pipeline instead of a
    monkeypatched two-chunk pack."""
    run = SubjectProductionRun(
        subject_id=UUID(int=300), edition_id=UUID(int=301), profile=ProductionProfile.BRIEF_AUTO
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
                    "id": uuid4(),
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
    conversations = _GatewayBackedConversations(
        [OLALAMPO_Q1, OLALAMPO_Q4_DRAFT, OLALAMPO_Q4_REPAIR], structured_adapter
    )
    store = ProductionArtifactStore(_Blobs())  # type: ignore[arg-type]
    archive_processor = _ArchiveProcessor(texts_by_blob)
    orchestrator = ProductionWorkflowOrchestrator(
        lambda: uow,  # type: ignore[arg-type]
        model_service=conversations,  # type: ignore[arg-type]
        collection_service=_CollectionService(uow),  # type: ignore[arg-type]
        artifact_store=store,
        source_evidence_processor=cast(SourceEvidenceProcessingService, archive_processor),
    )
    return orchestrator, uow, conversations, store


# =============================================================================
# Scenario 1 -- two Q2 chunks succeed; retrying the stage makes zero new
# provider submissions and reads both outputs back from the gateway's
# checkpoint.
# =============================================================================


async def test_scenario_1_stage_retry_after_full_success_hits_checkpoints_only() -> None:
    adapter = _ScriptedStructuredAdapter([(S1_TEXT, OLALAMPO_Q2_S1), (S2_TEXT, OLALAMPO_Q2_S2)])
    orchestrator, uow, conversations = _build_gateway_run(adapter)
    chunks = _fixed_two_chunks()

    async def fake_pack(*args: Any, **kwargs: Any) -> ProductionEvidencePack:
        return ProductionEvidencePack(
            "ready",
            "olalampo-gw-pack-1",
            chunks,
            {},
            original_derived_texts={str(S1_DOC_ID): S1_TEXT, str(S2_DOC_ID): S2_TEXT},
        )

    orchestrator._build_production_evidence_pack = fake_pack  # type: ignore[method-assign]

    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.REFERENCES)
    uow.run.current_stage = SubjectProductionStage.EXTRACTION

    first = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)
    assert first["status"] == "success", first
    assert len(adapter.calls) == 2

    # Force real re-entry into the per-chunk loop on retry instead of the
    # artifact-level "cached" shortcut (which would skip Q2 entirely and
    # prove nothing about the gateway's own checkpoint behaviour).
    uow.production_artifacts.items = [
        artifact
        for artifact in uow.production_artifacts.items
        if artifact.stage.value != "extraction"
    ]

    second = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)
    assert second["status"] == "success", second
    assert second["completed_chunk_ids"] == ["olalampo-gw-chunk-s1", "olalampo-gw-chunk-s2"]
    # Zero new provider submissions: both chunks were checkpoint hits.
    assert len(adapter.calls) == 2
    for provenance in second["chunk_provenance"].values():
        assert provenance["checkpoint"] == "hit"
        assert provenance["status"] == "succeeded"
    del conversations  # unused beyond construction wiring


# =============================================================================
# Scenario 2 -- a ModelRun that is FAILED with independently-proven
# NOT_SUBMITTED status is the one and only FAILED state the real gateway
# allows to retry, and only when the caller explicitly opts in. This cannot
# be reached through the Q2 extraction loop (see module docstring), so it is
# exercised directly against `ModelGateway`.
# =============================================================================


async def test_scenario_2_only_proven_not_submitted_failures_are_retryable() -> None:
    adapter = _ScriptedStructuredAdapter([("retry-me", OLALAMPO_Q2_S1)])
    run_uow_factory = InMemoryModelRunUnitOfWorkFactory()
    router = ModelRouter(
        openai_research=adapter, openai_structured=adapter, qwen=adapter, fake=adapter
    )
    gateway = ModelGateway(router, run_uow_factory, InMemoryModelOutputStore())

    request = ModelRequest(
        text="retry-me chunk text",
        prompt_template_id="production-q2-extraction",
        prompt_template_version="1",
        evidence_pack_hash=hashlib.sha256(b"scenario-2").hexdigest(),
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.BULK_EXTRACTION,
        run_id=uuid4(),
        allow_failed_resubmit=True,
    )
    run = gateway.build_run(request, ModelRole.STRUCTURED_EXTRACTION)
    # Certain pre-submission failure: e.g. the process crashed while building
    # the outbound payload, before `mark_submission_uncertain` ever ran, so
    # `submission_state` is still its default NOT_SUBMITTED. This is proof
    # the request never reached the provider.
    run.fail("local_build_error", "the request never left the process")
    assert run.submission_state is ModelSubmissionState.NOT_SUBMITTED
    run_uow_factory.state[run.id] = run

    execution = await gateway.extract(request, Q2ChunkOutput)

    assert execution.run.status is ModelRunStatus.SUCCEEDED
    assert execution.run.id == run.id  # same logical ModelRun, restarted in place
    assert len(adapter.calls) == 1

    # Without proof of NOT_SUBMITTED, the same escape hatch refuses.
    adapter_2 = _ScriptedStructuredAdapter([("retry-me-2", OLALAMPO_Q2_S1)])
    run_uow_factory_2 = InMemoryModelRunUnitOfWorkFactory()
    router_2 = ModelRouter(
        openai_research=adapter_2, openai_structured=adapter_2, qwen=adapter_2, fake=adapter_2
    )
    gateway_2 = ModelGateway(router_2, run_uow_factory_2, InMemoryModelOutputStore())
    request_2 = ModelRequest(
        text="retry-me-2 chunk text",
        prompt_template_id="production-q2-extraction",
        prompt_template_version="1",
        evidence_pack_hash=hashlib.sha256(b"scenario-2b").hexdigest(),
        external_llm_allowed=True,
        routing_hint=ModelRoutingHint.BULK_EXTRACTION,
        run_id=uuid4(),
        allow_failed_resubmit=True,
    )
    run_2 = gateway_2.build_run(request_2, ModelRole.STRUCTURED_EXTRACTION)
    run_2.mark_submission_uncertain()
    run_2.fail("bridge_timeout", "the transport errored after the request was sent")
    assert run_2.submission_state is ModelSubmissionState.SUBMITTED_OR_UNKNOWN
    run_uow_factory_2.state[run_2.id] = run_2
    try:
        await gateway_2.extract(request_2, Q2ChunkOutput)
    except ModelGatewayError:
        pass
    else:
        raise AssertionError("A SUBMITTED_OR_UNKNOWN failure must not be resubmitted")
    assert adapter_2.calls == []


# =============================================================================
# Scenario 3 -- chunk 1 succeeds; chunk 2's provider response is received but
# fails schema validation. The ModelRun persists FAILED (submission proven),
# and the second `execute_stage` call does not blindly resubmit it.
# =============================================================================


async def test_scenario_3_invalid_structured_output_is_not_blindly_resubmitted() -> None:
    adapter = _ScriptedStructuredAdapter(
        [(S1_TEXT, OLALAMPO_Q2_S1), (S2_TEXT, "this is not valid json at all")]
    )
    orchestrator, uow, _conversations = _build_gateway_run(adapter)
    chunks = _fixed_two_chunks()

    async def fake_pack(*args: Any, **kwargs: Any) -> ProductionEvidencePack:
        return ProductionEvidencePack(
            "ready",
            "olalampo-gw-pack-3",
            chunks,
            {},
            original_derived_texts={str(S1_DOC_ID): S1_TEXT, str(S2_DOC_ID): S2_TEXT},
        )

    orchestrator._build_production_evidence_pack = fake_pack  # type: ignore[method-assign]

    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.REFERENCES)
    uow.run.current_stage = SubjectProductionStage.EXTRACTION

    first = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)
    assert first["status"] == "needs_review"
    assert first["error_code"] == "q2_chunk_coverage_failed"
    assert first["completed_chunk_ids"] == ["olalampo-gw-chunk-s1"]
    assert first["failed_chunk_ids"] == ["olalampo-gw-chunk-s2"]
    assert len(adapter.calls) == 2  # both chunks were genuinely attempted once

    failed_run_id = UUID(first["chunk_provenance"]["olalampo-gw-chunk-s2"]["model_run_id"])
    persisted = await conversations_gateway_run(orchestrator, failed_run_id)
    assert persisted is not None
    assert persisted.status is ModelRunStatus.FAILED
    assert persisted.submission_state is ModelSubmissionState.SUBMITTED_OR_UNKNOWN

    second = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)
    assert second["status"] == "needs_review"
    assert second["error_code"] == "q2_chunk_coverage_failed"
    assert second["completed_chunk_ids"] == ["olalampo-gw-chunk-s1"]
    assert second["failed_chunk_ids"] == ["olalampo-gw-chunk-s2"]
    # No blind resubmission: still exactly the two original calls.
    assert len(adapter.calls) == 2


# =============================================================================
# Scenario 4 -- a transient transport/server error arrives after the request
# was already sent. Exactly one logical submission is made; the prompt is
# never repeated, and the resulting state is visible in chunk_provenance.
# =============================================================================


async def test_scenario_4_post_submission_transport_error_is_single_submission() -> None:
    adapter = _ScriptedStructuredAdapter(
        [
            (S1_TEXT, OLALAMPO_Q2_S1),
            (S2_TEXT, StructuredModelUnavailableError("bridge/server error after submission")),
        ]
    )
    orchestrator, uow, _conversations = _build_gateway_run(adapter)
    chunks = _fixed_two_chunks()

    async def fake_pack(*args: Any, **kwargs: Any) -> ProductionEvidencePack:
        return ProductionEvidencePack(
            "ready",
            "olalampo-gw-pack-4",
            chunks,
            {},
            original_derived_texts={str(S1_DOC_ID): S1_TEXT, str(S2_DOC_ID): S2_TEXT},
        )

    orchestrator._build_production_evidence_pack = fake_pack  # type: ignore[method-assign]

    uow.run.current_stage = SubjectProductionStage.REFERENCES
    await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.REFERENCES)
    uow.run.current_stage = SubjectProductionStage.EXTRACTION

    first = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)
    assert first["status"] == "needs_review"
    assert first["error_code"] == "q2_chunk_coverage_failed"
    provenance = first["chunk_provenance"]["olalampo-gw-chunk-s2"]
    assert provenance["status"] == "failed"
    assert len(adapter.calls) == 2  # one logical submission per chunk

    second = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.EXTRACTION)
    assert second["status"] == "needs_review"
    # No immediate repeat of the prompt for the transiently-failed chunk.
    assert len(adapter.calls) == 2


async def conversations_gateway_run(
    orchestrator: ProductionWorkflowOrchestrator, run_id: UUID
) -> Any:
    """Reach into the gateway-backed conversations wired onto `orchestrator`
    to read back a persisted ModelRun -- there is no other public accessor
    for it from the workflow layer."""
    model_service = cast(_GatewayBackedConversations, orchestrator._model_service)
    return await model_service.gateway.get_run(run_id)


# =============================================================================
# Scenarios 5/6/7 -- through the real gateway, over the full four-stage
# pipeline including the never-cited WHOIS page: the Q4 grounding gap is
# still real (not silently "fixed" here), the web_search matrix holds, and
# the WHOIS fixture still never reaches the Q2 corpus.
# =============================================================================


async def test_scenarios_5_6_7_full_pipeline_through_real_gateway() -> None:
    adapter = _ScriptedStructuredAdapter([(S1_TEXT, OLALAMPO_Q2_S1), (S2_TEXT, OLALAMPO_Q2_S2)])
    orchestrator, uow, conversations, store = _build_olalampo_gateway(adapter)

    uow.run.current_stage = SubjectProductionStage.REFERENCES
    references_result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.REFERENCES
    )
    assert references_result["status"] == "success", references_result

    uow.run.current_stage = SubjectProductionStage.EXTRACTION
    extraction_result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.EXTRACTION
    )
    assert extraction_result["status"] == "success", extraction_result

    uow.run.current_stage = SubjectProductionStage.SYNTHESIS
    synthesis_result = await orchestrator.execute_stage(
        uow.run.id, SubjectProductionStage.SYNTHESIS
    )
    assert synthesis_result["status"] == "success", synthesis_result

    uow.run.current_stage = SubjectProductionStage.ASSEMBLY
    assembly_result = await orchestrator.execute_stage(uow.run.id, SubjectProductionStage.ASSEMBLY)
    assert assembly_result["status"] == "success", assembly_result

    # --- Property 7: the WHOIS page never enters the Q2 corpus. -----------
    assert not any(S3_TEXT in call.text for call in adapter.calls)
    assert not any("WHOIS Database Download" in call.text for call in adapter.calls)
    evidence_pack_source_document_ids = extraction_result["evidence_pack_source_document_ids"]
    assert str(S3_DOC_ID) not in evidence_pack_source_document_ids
    assert str(S1_DOC_ID) in evidence_pack_source_document_ids
    assert str(S2_DOC_ID) in evidence_pack_source_document_ids

    # --- Property 6: Q4 draft web_search=True, Q4 repair=False, Q2=False. -
    assert len(adapter.calls) == 2
    assert all(call.web_search is False for call in adapter.calls)
    turn_requests = conversations.turn_requests
    assert len(turn_requests) == 3  # Q1, Q4 draft, Q4 repair
    q1, q4_draft, q4_repair = turn_requests
    assert q1["web_search"] is True
    assert q4_draft["web_search"] is True
    assert q4_repair["web_search"] is False

    # --- Property 5 (documented gap, not "fixed"): a paragraph carrying a
    # valid [S#] marker is accepted regardless of whether its content is
    # actually grounded in that source. -------------------------------
    brief_artifact = next(
        a for a in reversed(uow.production_artifacts.items) if a.stage.value == "brief"
    )
    assert brief_artifact.rendered_blob_id is not None
    brief_markdown = await store.read_text(brief_artifact.rendered_blob_id)
    assert "Kranovia" in brief_markdown
    assert assembly_result["qa"]["passed"] is True
