"""Tests de caractérisation des invariants Recovery Discovery (R26).

Ces tests verrouillent le comportement actuel de ``DiscoveryService`` autour
de la récupération (visible, manuelle, completion, reprise d'un
``recovery_child_model_run_id``) AVANT extraction de ce sous-domaine dans son
propre module. Ils ne couvrent que ce qui n'est pas déjà verrouillé par
``test_discovery.py`` / ``test_discovery_api.py`` : voir R26 pour la liste
des invariants visés.

Aucun code de production n'est modifié par ce fichier.
"""

from __future__ import annotations

import hashlib
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import pytest

from cti_app.application.discovery.contracts import (
    DiscoverEditionParameters,
    discovery_idempotency_key,
)
from cti_app.application.discovery.jobs import DISCOVERY_JOB_KIND
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.jobs import (
    JobExecutor,
    JobService,
    SynchronousJobDispatcher,
    create_job_registry,
)
from cti_app.application.model_gateway import ModelGateway, ModelGatewayError, ModelRouter
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.jobs import JobStatus
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun, ModelRunStatus
from cti_app.integrations.models import FakeModelAdapter, InMemoryModelOutputStore
from tests.discovery_support import InMemoryDiscoveryUnitOfWorkFactory
from tests.job_support import InMemoryJobUnitOfWorkFactory
from tests.model_support import InMemoryModelRunUnitOfWorkFactory
from tests.test_discovery import (
    FakeBridgeCapabilities,
    TransientResearchAdapter,
    gateway_for_adapter,
    parameters,
    persisted_research_run,
    research_markdown_fixture,
)

_CONVERSATION = {
    "id": "aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
    "external_locator": "https://chatgpt.com/c/aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa",
}


def _needs_review_parent(
    params: DiscoverEditionParameters, *, conversation: dict[str, str] | None = None
) -> ModelRun:
    """A parent ModelRun parked in NEEDS_REVIEW, response_id preserved."""
    run = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    run.require_review(
        "no_final_answer",
        "ChatGPT s'est arrêté sans produire de réponse finale.",
        details={"conversation": conversation} if conversation is not None else {},
    )
    return run


class FakeVisibleRecoveryBridge:
    """Records the bridge_run_id it was asked to recover, returns queued texts."""

    def __init__(self, texts: str | list[str]) -> None:
        self._texts = [texts] if isinstance(texts, str) else list(texts)
        self.calls: list[str] = []

    async def capabilities(self) -> dict[str, object]:
        return {}

    async def archive_conversation(self, conversation_id: UUID) -> None:
        del conversation_id

    async def preview_visible_recovery(self, bridge_run_id: str) -> dict[str, object]:
        self.calls.append(bridge_run_id)
        index = min(len(self.calls) - 1, len(self._texts) - 1)
        return {"text": self._texts[index]}


class _LinkCountingGateway(ModelGateway):
    """A ModelGateway that counts link_recovery_child calls, otherwise identical."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.link_calls = 0

    async def link_recovery_child(self, parent_run_id: UUID, child_run_id: UUID) -> None:
        self.link_calls += 1
        await super().link_recovery_child(parent_run_id, child_run_id)


# --- 1. Existing SUCCEEDED : visible_citations restaurées -----------------


async def test_existing_succeeded_run_restores_visible_citations_without_new_post() -> None:
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    reference = await output_store.store(
        research_markdown_fixture().encode(), mime_type="text/markdown"
    )
    completed_run = persisted_research_run(
        params, status=ModelRunStatus.SUCCEEDED, output_reference=reference
    )
    completed_run.visible_citations = (
        {"url": "https://extra.example/citation", "label": "Extra source"},
    )
    model_uow.state[completed_run.id] = completed_run
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="edition",
        aggregate_id=params.edition_id,
        idempotency_key=discovery_idempotency_key(params),
        correlation_id="visible-citations-restore",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    assert (await jobs.get(job.id)).status is JobStatus.SUCCEEDED
    assert adapter.calls == []
    batches = await discovery.list_batches(params.edition_id)
    assert batches[0].unattached_visible_citations
    assert (
        batches[0].unattached_visible_citations[0]["canonical_url"]
        == "https://extra.example/citation"
    )


# --- 3. NEEDS_REVIEW avec recovery child : reprise exacte ------------------


async def test_needs_review_with_linked_child_resumes_exactly_that_child() -> None:
    """When a recovery child is already linked, discover_edition adopts its
    output onto the parent instead of resubmitting research."""
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    parent = _needs_review_parent(params)
    child_reference = await output_store.store(
        research_markdown_fixture().encode(), mime_type="text/markdown"
    )
    child = persisted_research_run(
        params, status=ModelRunStatus.SUCCEEDED, output_reference=child_reference
    )
    child.id = uuid4()
    parent.error_details = {
        **(parent.error_details or {}),
        "recovery_child_model_run_id": str(child.id),
    }
    model_uow.state[parent.id] = parent
    model_uow.state[child.id] = child
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    job_uow = InMemoryJobUnitOfWorkFactory()
    registry = create_job_registry(gateway, discovery)
    jobs = JobService(job_uow, registry)
    dispatcher = SynchronousJobDispatcher(JobExecutor(job_uow, registry))
    job = await jobs.submit(
        kind=DISCOVERY_JOB_KIND,
        aggregate_type="edition",
        aggregate_id=params.edition_id,
        idempotency_key=discovery_idempotency_key(params),
        correlation_id="linked-child-resume",
        input_parameters=params.model_dump(mode="json"),
        max_attempts=1,
    )

    await dispatcher.dispatch(job.id)

    assert (await jobs.get(job.id)).status is JobStatus.SUCCEEDED
    # No new research POST: the linked child was resumed directly.
    assert adapter.calls == []
    # No second child was created: exactly parent + the one existing child.
    assert set(model_uow.state) == {parent.id, child.id}
    recovered_parent = model_uow.state[parent.id]
    assert recovered_parent.status is ModelRunStatus.SUCCEEDED
    assert recovered_parent.error_details is not None
    assert recovered_parent.error_details["recovery"]["provenance"] == "recovery_continuation"
    assert recovered_parent.error_details["recovery"]["source_model_run_id"] == str(child.id)


# --- 4. Visible recovery ---------------------------------------------------


async def test_visible_recovery_preview_requires_response_id() -> None:
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    # RUNNING -> NEEDS_REVIEW without ever going through wait_for_background,
    # so no response_id was ever attached.
    run = ModelRun(
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake-deterministic-v1",
        prompt_template_id="monthly-cti-discovery",
        prompt_template_version="4.0",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
    )
    run.require_review("no_final_answer", "incomplete", details={})
    model_uow.state[run.id] = run
    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
    )

    with pytest.raises(ModelGatewayError, match="not waiting for recovery"):
        await discovery.preview_visible_recovery(params, run.id)


async def test_visible_recovery_preview_requires_recoverable_status() -> None:
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    reference = await output_store.store(
        research_markdown_fixture().encode(), mime_type="text/markdown"
    )
    succeeded = persisted_research_run(
        params, status=ModelRunStatus.SUCCEEDED, output_reference=reference
    )
    model_uow.state[succeeded.id] = succeeded
    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
    )

    with pytest.raises(ModelGatewayError, match="not waiting for recovery"):
        await discovery.preview_visible_recovery(params, succeeded.id)


async def test_visible_recovery_preview_computes_sha_without_persisting() -> None:
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    parent = _needs_review_parent(params)
    model_uow.state[parent.id] = parent
    text = research_markdown_fixture() + "\n<!-- visible recovery candidate -->\n"
    bridge = FakeVisibleRecoveryBridge(text)
    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        bridge_capabilities_provider=bridge,
    )

    preview = await discovery.preview_visible_recovery(params, parent.id)

    assert preview["report_markdown"] == text
    assert preview["sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert bridge.calls == ["bridge-durable-run"]
    assert model_uow.state[parent.id].status is ModelRunStatus.NEEDS_REVIEW
    assert model_uow.state[parent.id].raw_output_reference is None
    assert not output_store.objects
    assert adapter.calls == []


async def test_visible_recovery_confirm_redoes_preview_and_rejects_stale_sha() -> None:
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    parent = _needs_review_parent(params)
    model_uow.state[parent.id] = parent
    stale_text = research_markdown_fixture() + "\n<!-- stale -->\n"
    final_text = research_markdown_fixture() + "\n<!-- final -->\n"
    bridge = FakeVisibleRecoveryBridge([stale_text, final_text])
    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        bridge_capabilities_provider=bridge,
    )

    stale_preview = await discovery.preview_visible_recovery(params, parent.id)

    # By the time confirm runs, the bridge's visible text has moved on: the
    # confirm redoes the preview internally and must refuse the stale hash.
    with pytest.raises(ValueError, match="no longer matches"):
        await discovery.adopt_visible_recovery(
            params,
            parent.id,
            expected_sha256=stale_preview["sha256"],
            actor_id="analyst:test",
        )
    assert model_uow.state[parent.id].status is ModelRunStatus.NEEDS_REVIEW
    assert not output_store.objects

    # Confirming with the sha of the text actually recovered now succeeds and
    # persists exactly that confirmed content.
    current_preview = await discovery.preview_visible_recovery(params, parent.id)
    await discovery.adopt_visible_recovery(
        params,
        parent.id,
        expected_sha256=current_preview["sha256"],
        actor_id="analyst:test",
    )

    recovered = model_uow.state[parent.id]
    assert recovered.status is ModelRunStatus.SUCCEEDED
    assert recovered.error_details is not None
    assert recovered.error_details["recovery"]["provenance"] == "visible_recovery"
    assert recovered.raw_output_reference is not None
    archived = await output_store.read(recovered.raw_output_reference, max_bytes=10_000_000)
    assert archived.decode() == final_text
    assert adapter.calls == []


# --- 5. Manual recovery -----------------------------------------------------


async def test_manual_recovery_preview_persists_nothing_and_confirm_rejects_wrong_sha() -> None:
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    parent = persisted_research_run(params, status=ModelRunStatus.WAITING_BACKGROUND)
    model_uow.state[parent.id] = parent
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)
    markdown = research_markdown_fixture() + "\n<!-- manual recovery candidate -->\n"

    preview = await discovery.preview_manual_recovery(params, parent.id, markdown)

    assert preview["sha256"] == hashlib.sha256(markdown.encode()).hexdigest()
    assert model_uow.state[parent.id].status is ModelRunStatus.WAITING_BACKGROUND
    assert not output_store.objects
    assert adapter.calls == []

    with pytest.raises(ValueError, match="no longer matches"):
        await discovery.adopt_recovery_report(
            params,
            parent.id,
            markdown,
            expected_sha256="0" * 64,
            provenance="manual_import",
            actor_id="analyst:test",
        )
    assert model_uow.state[parent.id].status is ModelRunStatus.WAITING_BACKGROUND
    assert not output_store.objects


# --- 6. Completion recovery -------------------------------------------------


async def test_completion_recovery_child_id_is_deterministic_from_parent() -> None:
    params = parameters()
    adapter = FakeModelAdapter(research_text=research_markdown_fixture())
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    parent = _needs_review_parent(params, conversation=_CONVERSATION)
    model_uow.state[parent.id] = parent
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)

    child_id = await discovery.start_completion_recovery(params, parent.id)

    assert child_id == uuid5(NAMESPACE_URL, f"{parent.id}:complete-initial-response:v1")
    assert len(adapter.calls) == 1
    submitted = adapter.calls[0]
    assert submitted.conversation is not None
    assert submitted.conversation.mode == "continue"
    assert submitted.conversation.id == UUID(_CONVERSATION["id"])
    assert submitted.conversation.external_locator == _CONVERSATION["external_locator"]


async def test_completion_recovery_requires_conversation_id_and_external_locator() -> None:
    # Distinct axes so the two parents get distinct deterministic ModelRun
    # ids and cannot shadow each other in model_uow.
    params_a = parameters(axis="missing-conversation")
    params_b = parameters(axis="missing-external-locator")
    adapter = TransientResearchAdapter()
    gateway, model_uow, _ = gateway_for_adapter(adapter)

    missing_conversation = _needs_review_parent(params_a)
    model_uow.state[missing_conversation.id] = missing_conversation
    with pytest.raises(ModelGatewayError, match="conversation is unavailable"):
        await DiscoveryService(
            InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway
        ).start_completion_recovery(params_a, missing_conversation.id)

    missing_locator = _needs_review_parent(
        params_b, conversation={"id": _CONVERSATION["id"]}
    )
    model_uow.state[missing_locator.id] = missing_locator
    with pytest.raises(ModelGatewayError, match="conversation is unavailable"):
        await DiscoveryService(
            InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway
        ).start_completion_recovery(params_b, missing_locator.id)

    assert missing_conversation.id != missing_locator.id
    assert adapter.calls == []


async def test_completion_recovery_fails_when_existing_child_is_failed_or_blocked() -> None:
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    parent = _needs_review_parent(params, conversation=_CONVERSATION)
    model_uow.state[parent.id] = parent
    child_id = uuid5(NAMESPACE_URL, f"{parent.id}:complete-initial-response:v1")
    child = ModelRun(
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake-deterministic-v1",
        prompt_template_id="monthly-cti-discovery-recovery",
        prompt_template_version="1.0",
        authorized_input_hash="a" * 64,
        evidence_pack_hash=parent.evidence_pack_hash,
        parameters={},
        id=child_id,
    )
    child.fail("bridge_failed", "La récupération de complétion a échoué.")
    model_uow.state[child.id] = child
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)

    with pytest.raises(ModelGatewayError, match="échoué"):
        await discovery.start_completion_recovery(params, parent.id)

    # A failed/blocked child never triggers a relaunch.
    assert adapter.calls == []


async def test_completion_recovery_links_child_exactly_once_on_creation() -> None:
    params = parameters()
    adapter = FakeModelAdapter(research_text=research_markdown_fixture())
    model_uow = InMemoryModelRunUnitOfWorkFactory()
    gateway = _LinkCountingGateway(
        ModelRouter(
            openai_research=adapter,
            openai_structured=adapter,
            qwen=adapter,
            fake=adapter,
            forced_provider=ModelProvider.FAKE,
        ),
        model_uow,
        InMemoryModelOutputStore(),
    )
    parent = _needs_review_parent(params, conversation=_CONVERSATION)
    model_uow.state[parent.id] = parent
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)

    await discovery.start_completion_recovery(params, parent.id)

    assert gateway.link_calls == 1


async def test_completion_recovery_returns_existing_continuation_source_without_relaunch() -> None:
    """Once a parent already carries recovery_continuation provenance, asking
    for completion recovery again must return the recorded source, not start
    a fresh (or second) child."""
    params = parameters()
    adapter = TransientResearchAdapter()
    gateway, model_uow, output_store = gateway_for_adapter(adapter)
    source_id = uuid4()
    markdown = research_markdown_fixture()
    reference = await output_store.store(markdown.encode(), mime_type="text/markdown")
    parent = _needs_review_parent(params)
    parent.adopt_recovery(
        output_reference=reference,
        output_sha256=hashlib.sha256(markdown.encode()).hexdigest(),
        output_chars=len(markdown),
        provenance="recovery_continuation",
        actor_id="system:recovery",
        source_model_run_id=source_id,
    )
    model_uow.state[parent.id] = parent
    discovery = DiscoveryService(InMemoryDiscoveryUnitOfWorkFactory(), gateway, archive=gateway)

    returned = await discovery.start_completion_recovery(params, parent.id)

    assert returned == source_id
    assert adapter.calls == []


# --- 7. Standalone import ----------------------------------------------------


async def test_standalone_import_ids_are_deterministic_and_reimport_creates_no_second_run() -> (
    None
):
    adapter = TransientResearchAdapter()
    gateway, model_uow, _ = gateway_for_adapter(adapter)
    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
    )
    params = parameters(axis="manual-import")
    markdown = research_markdown_fixture()
    digest = hashlib.sha256(markdown.encode()).hexdigest()
    expected_manual_run_id = uuid5(
        NAMESPACE_URL, f"cti-discovery-manual-import:{params.edition_id}:{digest}"
    )
    expected_request_hash = hashlib.sha256(
        f"manual-import:v1:{params.edition_id}:{digest}".encode()
    ).hexdigest()

    preview = await discovery.preview_standalone_import(params, markdown)
    assert preview["sha256"] == digest
    # Preview never persists.
    assert not model_uow.state

    batch, reused, _job_id = await discovery.import_standalone_report(
        params, markdown, expected_sha256=preview["sha256"], actor_id="dev-analyst"
    )
    assert reused is False
    assert batch.discovery_model_run_id == expected_manual_run_id
    assert batch.request_hash == expected_request_hash
    assert set(model_uow.state) == {expected_manual_run_id}

    reimported, reused_again, job_id_again = await discovery.import_standalone_report(
        params, markdown, expected_sha256=preview["sha256"], actor_id="dev-analyst"
    )
    assert reused_again is True
    assert job_id_again is None
    assert reimported.id == batch.id
    # No second synthetic ModelRun was created for the identical Markdown.
    assert set(model_uow.state) == {expected_manual_run_id}
    assert adapter.calls == []


async def test_standalone_import_calls_after_persisted_batch_callback_on_new() -> None:
    """Standalone import must invoke after_persisted_batch exactly once on new batch creation,
    and not at all on re-import idempotence."""

    adapter = TransientResearchAdapter()
    gateway, _model_uow, _ = gateway_for_adapter(adapter)

    # Mock job object to return from the callback
    class MockReconciliationJob:
        def __init__(self, job_id: UUID) -> None:
            self.id = job_id

    # Track callback invocations
    callback_invocations: list[dict[str, object]] = []
    expected_job_id = uuid4()

    async def fake_after_persisted_batch(
        batch: object, input_mode: DiscoveryInputMode, actor_id: str
    ) -> object:
        """Fake callback that records calls and returns a mock job object."""
        callback_invocations.append(
            {"batch": batch, "input_mode": input_mode, "actor_id": actor_id}
        )
        return MockReconciliationJob(expected_job_id)

    discovery = DiscoveryService(
        InMemoryDiscoveryUnitOfWorkFactory(),
        gateway,
        archive=gateway,
        bridge_capabilities_provider=FakeBridgeCapabilities(),
        after_persisted_batch=fake_after_persisted_batch,
    )
    params = parameters(axis="standalone-callback-test")
    markdown = research_markdown_fixture()
    preview = await discovery.preview_standalone_import(params, markdown)

    # Preview must not trigger the callback.
    assert len(callback_invocations) == 0

    # First import: creates a new batch and triggers callback exactly once
    actor_id = "analyst:test-R27a"
    batch1, reused1, job_id1 = await discovery.import_standalone_report(
        params, markdown, expected_sha256=preview["sha256"], actor_id=actor_id
    )

    assert reused1 is False
    assert len(callback_invocations) == 1
    assert callback_invocations[0]["input_mode"] == DiscoveryInputMode.MANUAL_IMPORT
    assert callback_invocations[0]["actor_id"] == actor_id
    assert callback_invocations[0]["batch"] is batch1
    assert job_id1 == expected_job_id

    # Re-import: idempotent operation, returns existing batch, callback NOT called again
    batch2, reused2, job_id2 = await discovery.import_standalone_report(
        params, markdown, expected_sha256=preview["sha256"], actor_id=actor_id
    )

    assert reused2 is True
    assert job_id2 is None
    assert batch2.id == batch1.id
    # Callback still called exactly once (no new invocation)
    assert len(callback_invocations) == 1
    assert adapter.calls == []
