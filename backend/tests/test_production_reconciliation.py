from __future__ import annotations

import hashlib
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import pytest

from cti_app.application.jobs import DuplicateJobError
from cti_app.application.production_reconciliation import (
    ProductionReconciliationError,
    ProductionReconciliationService,
    _verified_external_turn_id,
)
from cti_app.domain.editions import EditionStatus
from cti_app.domain.jobs import Job
from cti_app.domain.model_runs import (
    ModelProvider,
    ModelRole,
    ModelRun,
    ModelRunStatus,
    ModelSubmissionState,
)
from cti_app.domain.production import (
    EditionProductionBatch,
    EditionProductionBatchItem,
    ProductionBatchPhase,
    ProductionBatchStatus,
    ProductionSubmissionReconciliation,
    SubjectProductionRun,
    SubjectProductionStage,
    SubjectProductionStatus,
    model_run_awaits_reconciliation,
)


class _Runs:
    def __init__(self, run: SubjectProductionRun, model: ModelRun) -> None:
        self.runs = {run.id: run}
        self.models = {model.id: model}

    async def get(self, run_id: UUID):
        return self.runs.get(run_id)

    async def get_for_update(self, run_id: UUID):
        return self.runs.get(run_id)

    async def save(self, run: SubjectProductionRun) -> None:
        self.runs[run.id] = run


class _Models:
    def __init__(self, models: dict[UUID, ModelRun]) -> None:
        self.models = models

    async def get(self, run_id: UUID):
        return self.models.get(run_id)


class _EditionRepo:
    def __init__(self, edition: SimpleNamespace) -> None:
        self.edition = edition

    async def get(self, edition_id: UUID):
        return self.edition if edition_id == self.edition.id else None

    async def get_for_update(self, edition_id: UUID):
        return await self.get(edition_id)


class _BatchRepo:
    def __init__(self, batch: EditionProductionBatch) -> None:
        self.batch = batch
        self.saves = 0

    async def get(self, batch_id: UUID):
        return self.batch if batch_id == self.batch.id else None

    async def get_for_update(self, batch_id: UUID):
        return await self.get(batch_id)

    async def get_active_for_edition(self, edition_id: UUID):
        if self.batch.edition_id != edition_id:
            return None
        return (
            self.batch
            if self.batch.status in {ProductionBatchStatus.QUEUED, ProductionBatchStatus.RUNNING}
            else None
        )

    async def save(self, batch: EditionProductionBatch) -> None:
        self.batch = batch
        self.saves += 1


class _Items:
    def __init__(self, items: list[EditionProductionBatchItem]) -> None:
        self.items = items

    async def get_by_run(self, run_id: UUID):
        return next((item for item in self.items if item.production_run_id == run_id), None)

    async def list_for_batch(self, batch_id: UUID):
        return [item for item in self.items if item.batch_id == batch_id]


class _Manifests:
    def __init__(self) -> None:
        self.frozen = False

    async def get_latest_for_edition(self, edition_id: UUID):
        return object() if self.frozen else None


class _Uow:
    def __init__(
        self,
        run: SubjectProductionRun,
        model: ModelRun,
        *,
        edition_status: EditionStatus = EditionStatus.PRODUCTION,
        batch_status: ProductionBatchStatus = ProductionBatchStatus.RUNNING,
    ) -> None:
        edition = SimpleNamespace(id=run.edition_id, status=edition_status)
        batch = EditionProductionBatch(
            id=uuid4(),
            edition_id=run.edition_id,
            status=batch_status,
            phase=ProductionBatchPhase.REVIEW,
        )
        item = EditionProductionBatchItem(
            batch_id=batch.id,
            subject_id=run.subject_id,
            production_run_id=run.id,
            position=1,
        )
        self.subject_production_runs = _Runs(run, model)
        self.model_runs = _Models({model.id: model})
        self.editions = _EditionRepo(edition)
        self.edition_production_batches = _BatchRepo(batch)
        self.edition_production_batch_items = _Items([item])
        self.publication_manifests = _Manifests()

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *args: object) -> None:
        return None

    async def commit(self) -> None:
        return None


class _Gateway:
    def __init__(self, model: ModelRun) -> None:
        self.model = model
        self.adapter_calls = 0
        self.outputs: dict[str, bytes] = {}
        self.adoptions: list[dict[str, object]] = []

    async def adopt_recovery_output(self, run_id: UUID, content: bytes, **kwargs: object):
        assert run_id == self.model.id
        self.adoptions.append(dict(kwargs))
        self.model.adopt_recovery(
            output_reference=f"output:{run_id}",
            output_sha256=hashlib.sha256(content).hexdigest(),
            output_chars=len(content.decode()),
            provenance=str(kwargs["provenance"]),
            actor_id=str(kwargs["actor_id"]),
        )
        self.outputs[f"output:{run_id}"] = content
        return self.model

    async def read_output(self, reference: str, *, max_bytes: int) -> bytes:
        return self.outputs[reference][:max_bytes]


class _Bridge:
    def __init__(self, response_id: str, text: str) -> None:
        self.response_id = response_id
        self.text = text
        self.previews = 0
        self.releases = 0

    async def preview_visible_recovery(self, bridge_run_id: str):
        self.previews += 1
        return {
            "bridge_run_id": bridge_run_id,
            # The verified external assistant turn id: deliberately unrelated to
            # the bridge run id and to ModelRun.response_id.
            "turn_id": "dom-turn-77",
            "text": self.text,
            "metadata": {"turn_id": "dom-turn-77", "target_id": "target-1"},
        }

    async def release_visible_recovery(self, bridge_run_id: str):
        assert bridge_run_id == self.response_id
        self.releases += 1
        return {"released": True}


class _Jobs:
    def __init__(self) -> None:
        self.jobs: dict[str, Job] = {}
        self.submissions = 0

    async def submit(self, **kwargs: object) -> Job:
        key = str(kwargs["idempotency_key"])
        if key in self.jobs:
            raise DuplicateJobError(self.jobs[key].id)
        job = Job(
            kind=str(kwargs["kind"]),
            aggregate_type="subject",
            aggregate_id=cast(UUID, kwargs["aggregate_id"]),
            idempotency_key=key,
            correlation_id="test",
            input_parameters=cast(dict[str, object], kwargs["input_parameters"]),
            max_attempts=int(cast(int, kwargs["max_attempts"])),
        )
        self.jobs[key] = job
        self.submissions += 1
        return job

    async def get(self, job_id: UUID) -> Job:
        return next(job for job in self.jobs.values() if job.id == job_id)


class _Dispatcher:
    def __init__(self) -> None:
        self.dispatched: list[UUID] = []

    async def dispatch(self, job_id: UUID, *, delay_ms: int = 0) -> None:
        del delay_ms
        self.dispatched.append(job_id)


ReconciliationFixture = tuple[ProductionReconciliationService, _Uow, _Gateway, _Bridge, _Jobs]


def _build_fixture(
    *,
    edition_status: EditionStatus = EditionStatus.PRODUCTION,
    batch_status: ProductionBatchStatus = ProductionBatchStatus.RUNNING,
) -> ReconciliationFixture:
    edition_id, subject_id = uuid4(), uuid4()
    model_id = uuid4()
    model = ModelRun(
        id=model_id,
        provider=ModelProvider.OPENAI,
        model_role=ModelRole.RESEARCH,
        requested_model="chatgpt-web",
        prompt_template_id="production",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
        status=ModelRunStatus.NEEDS_REVIEW,
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        submission_attempt=1,
        response_id="bridge-1",
        error_code="model_submission_reconciliation_required",
    )
    run = SubjectProductionRun(
        id=uuid4(),
        subject_id=subject_id,
        edition_id=edition_id,
        status=SubjectProductionStatus.NEEDS_REVIEW,
        current_stage=SubjectProductionStage.EXTRACTION,
        pipeline_generation=7,
        error_code="model_submission_reconciliation_required",
        error_message="reconcile",
    )
    # The persisted identity must point back to the exact production run.
    run.reconciliation = ProductionSubmissionReconciliation(
        production_run_id=run.id,
        model_run_id=model_id,
        stage=SubjectProductionStage.EXTRACTION,
        bridge_response_id="bridge-1",
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        phase="reconciliation",
    )
    uow = _Uow(run, model, edition_status=edition_status, batch_status=batch_status)
    gateway = _Gateway(model)
    bridge = _Bridge("bridge-1", "# recovered\n\nanswer")
    jobs = _Jobs()
    service = ProductionReconciliationService(
        lambda: uow,
        gateway,
        jobs,
        _Dispatcher(),
        bridge,  # type: ignore[arg-type]
    )
    return service, uow, gateway, bridge, jobs


@pytest.fixture
def fixture() -> ReconciliationFixture:
    return _build_fixture()


@pytest.fixture
def review_fixture() -> ReconciliationFixture:
    """The state actually reached after a production that finished with issues.

    The batch is terminal, its phase is review, and the edition already moved
    on to review.  Nothing here is hypothetical: this is what an operator sees
    when a single article stopped on an ambiguous ChatGPT submission.
    """
    return _build_fixture(
        edition_status=EditionStatus.REVIEW,
        batch_status=ProductionBatchStatus.COMPLETED_WITH_ISSUES,
    )


@pytest.mark.asyncio
async def test_preview_and_hash_mismatch_do_not_mutate(fixture: ReconciliationFixture) -> None:
    service, uow, gateway, bridge, jobs = fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    preview = await service.preview_visible(run.id)
    assert preview.sha256 == hashlib.sha256(preview.text.encode()).hexdigest()
    assert gateway.adapter_calls == 0
    assert jobs.submissions == 0
    with pytest.raises(ProductionReconciliationError) as error:
        await service.adopt_visible(preview.production_run_id, "0" * 64, actor_id="analyst")
    assert error.value.code == "production_reconciliation_hash_mismatch"
    assert gateway.model.status is ModelRunStatus.NEEDS_REVIEW
    assert bridge.releases == 0


@pytest.mark.asyncio
async def test_adoption_resumes_same_generation_and_repeats_idempotently(
    fixture: ReconciliationFixture,
) -> None:
    service, uow, gateway, bridge, jobs = fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    expected = hashlib.sha256(b"# recovered\n\nanswer").hexdigest()
    result = await service.adopt_visible(run.id, expected, actor_id="analyst")
    assert gateway.model.status is ModelRunStatus.SUCCEEDED
    assert run.status is SubjectProductionStatus.RUNNING
    assert run.pipeline_generation == 7
    assert result["provenance"] == "visible_recovery"
    assert jobs.submissions == 1
    assert bridge.releases == 1
    await service.adopt_visible(run.id, expected, actor_id="analyst")
    assert jobs.submissions == 1
    assert bridge.releases == 2
    assert len(jobs.jobs) == 1


@pytest.mark.asyncio
async def test_manual_adoption_has_no_visible_preview_or_provider_call(
    fixture: ReconciliationFixture,
) -> None:
    service, uow, gateway, bridge, jobs = fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    text = "manual markdown"
    digest = hashlib.sha256(text.encode()).hexdigest()
    await service.preview_manual(run.id, text)
    await service.adopt_manual(run.id, text, digest, actor_id="analyst")
    assert gateway.adapter_calls == 0
    assert bridge.previews == 0
    assert jobs.submissions == 1
    assert gateway.model.error_details is not None
    assert gateway.model.error_details["recovery"]["provenance"] == "manual_import"


@pytest.mark.asyncio
async def test_abandon_releases_exact_visible_target_without_adopting_it(
    fixture: ReconciliationFixture,
) -> None:
    service, uow, gateway, bridge, jobs = fixture
    run = next(iter(uow.subject_production_runs.runs.values()))

    result = await service.abandon_visible(run.id)

    assert result["action"] == "production_reconciliation_abandoned"
    assert bridge.releases == 1
    assert gateway.model.status is ModelRunStatus.NEEDS_REVIEW
    assert run.status is SubjectProductionStatus.NEEDS_REVIEW
    assert jobs.submissions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["cancelled", "selection", "frozen", "sibling"])
async def test_resume_safety_fences_are_typed_conflicts(
    fixture: ReconciliationFixture, blocked: str
) -> None:
    service, uow, gateway, bridge, jobs = fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    if blocked == "cancelled":
        uow.edition_production_batches.batch.status = ProductionBatchStatus.CANCELLED
    elif blocked == "selection":
        uow.editions.edition.status = EditionStatus.SELECTION
    elif blocked == "frozen":
        uow.publication_manifests.frozen = True
    else:
        sibling = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=run.edition_id,
            status=SubjectProductionStatus.RUNNING,
            current_stage=SubjectProductionStage.SOURCES,
        )
        uow.subject_production_runs.runs[sibling.id] = sibling
        item = EditionProductionBatchItem(
            batch_id=uow.edition_production_batches.batch.id,
            subject_id=sibling.subject_id,
            production_run_id=sibling.id,
            position=2,
        )
        uow.edition_production_batch_items.items.append(item)
    with pytest.raises(ProductionReconciliationError) as error:
        await service.adopt_manual(
            run.id,
            "manual",
            hashlib.sha256(b"manual").hexdigest(),
            actor_id="analyst",
        )
    assert error.value.code in {
        "production_reconciliation_batch_cancelled",
        "production_reconciliation_edition_selection",
        "production_reconciliation_publication_frozen",
        "production_reconciliation_active_sibling",
    }
    assert gateway.model.status is ModelRunStatus.NEEDS_REVIEW
    assert jobs.submissions == 0
    assert bridge.releases == 0


@pytest.mark.asyncio
async def test_adoption_reopens_the_finished_batch_for_a_review_recovery(
    review_fixture: ReconciliationFixture,
) -> None:
    """The batch is the dispatch fence, so the resume must reopen it."""
    service, uow, _, _, jobs = review_fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    expected = hashlib.sha256(b"# recovered\n\nanswer").hexdigest()

    result = await service.adopt_visible(run.id, expected, actor_id="analyst")

    batch = uow.edition_production_batches.batch
    assert batch.status is ProductionBatchStatus.RUNNING
    assert batch.phase is ProductionBatchPhase.REVIEW
    assert batch.finished_at is None
    assert run.status is SubjectProductionStatus.RUNNING
    # The exact archived answer is resumed: same generation, no new prompt.
    assert run.pipeline_generation == 7
    assert result["pipeline_generation"] == 7
    assert jobs.submissions == 1


@pytest.mark.asyncio
async def test_repeated_adoption_reopens_once_and_submits_one_job(
    review_fixture: ReconciliationFixture,
) -> None:
    service, uow, _, _, jobs = review_fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    expected = hashlib.sha256(b"# recovered\n\nanswer").hexdigest()

    await service.adopt_visible(run.id, expected, actor_id="analyst")
    saves_after_first = uow.edition_production_batches.saves
    await service.adopt_visible(run.id, expected, actor_id="analyst")

    assert jobs.submissions == 1
    assert len(jobs.jobs) == 1
    # The second call finds a dispatchable batch and changes nothing.
    assert uow.edition_production_batches.saves == saves_after_first


@pytest.mark.asyncio
async def test_cancelled_batch_is_never_reopened_by_a_review_recovery(
    review_fixture: ReconciliationFixture,
) -> None:
    service, uow, gateway, _, jobs = review_fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    uow.edition_production_batches.batch.status = ProductionBatchStatus.CANCELLED

    with pytest.raises(ProductionReconciliationError) as error:
        await service.adopt_visible(
            run.id, hashlib.sha256(b"# recovered\n\nanswer").hexdigest(), actor_id="analyst"
        )

    assert error.value.code == "production_reconciliation_batch_cancelled"
    assert uow.edition_production_batches.batch.status is ProductionBatchStatus.CANCELLED
    assert gateway.model.status is ModelRunStatus.NEEDS_REVIEW
    assert jobs.submissions == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked", ["frozen", "sibling", "assembling"])
async def test_terminal_batch_recovery_keeps_every_publication_fence(
    review_fixture: ReconciliationFixture, blocked: str
) -> None:
    service, uow, gateway, _, jobs = review_fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    if blocked == "frozen":
        uow.publication_manifests.frozen = True
    elif blocked == "assembling":
        uow.editions.edition.status = EditionStatus.ASSEMBLING
    else:
        sibling = SubjectProductionRun(
            subject_id=uuid4(),
            edition_id=run.edition_id,
            status=SubjectProductionStatus.RUNNING,
            current_stage=SubjectProductionStage.SOURCES,
        )
        uow.subject_production_runs.runs[sibling.id] = sibling
        uow.edition_production_batch_items.items.append(
            EditionProductionBatchItem(
                batch_id=uow.edition_production_batches.batch.id,
                subject_id=sibling.subject_id,
                production_run_id=sibling.id,
                position=2,
            )
        )

    with pytest.raises(ProductionReconciliationError) as error:
        await service.adopt_manual(
            run.id, "manual", hashlib.sha256(b"manual").hexdigest(), actor_id="analyst"
        )

    assert error.value.code in {
        "production_reconciliation_publication_frozen",
        "production_reconciliation_active_sibling",
    }
    assert uow.edition_production_batches.batch.status is (
        ProductionBatchStatus.COMPLETED_WITH_ISSUES
    )
    assert gateway.model.status is ModelRunStatus.NEEDS_REVIEW
    assert jobs.submissions == 0


@pytest.mark.asyncio
async def test_visible_adoption_forwards_the_verified_external_turn_id(
    fixture: ReconciliationFixture,
) -> None:
    """The DOM turn id captured by the bridge is what reaches the gateway."""
    service, uow, gateway, _bridge, _jobs = fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    preview = await service.preview_visible(run.id)

    assert preview.external_turn_id == "dom-turn-77"
    # It is never exposed as operator-facing preview data.
    assert "external_turn_id" not in preview.as_dict()

    await service.adopt_visible(run.id, preview.sha256, actor_id="analyst")

    assert gateway.adoptions == [
        {
            "provenance": "visible_recovery",
            "actor_id": "analyst",
            "external_turn_id": "dom-turn-77",
        }
    ]
    assert gateway.adoptions[0]["external_turn_id"] != gateway.model.response_id
    assert gateway.adoptions[0]["external_turn_id"] != preview.bridge_response_id


@pytest.mark.asyncio
async def test_review_adoption_uses_the_same_identity_contract(
    review_fixture: ReconciliationFixture,
) -> None:
    """Review and Production adopt through one contract; identity must match."""
    service, uow, gateway, _bridge, _jobs = review_fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    preview = await service.preview_visible(run.id)
    await service.adopt_visible(run.id, preview.sha256, actor_id="reviewer")

    assert gateway.adoptions[0]["external_turn_id"] == "dom-turn-77"


@pytest.mark.asyncio
async def test_manual_adoption_never_invents_an_external_turn_id(
    fixture: ReconciliationFixture,
) -> None:
    service, uow, gateway, bridge, _jobs = fixture
    run = next(iter(uow.subject_production_runs.runs.values()))
    markdown = "# import manuel\n\ncontenu"
    preview = await service.preview_manual(run.id, markdown)

    assert preview.external_turn_id is None

    await service.adopt_manual(run.id, markdown, preview.sha256, actor_id="analyst")

    assert gateway.adoptions == [{"provenance": "manual_import", "actor_id": "analyst"}]
    assert bridge.previews == 0


@pytest.mark.parametrize(
    "value",
    [None, "", "   ", 42, "x" * 513],
)
def test_unverified_turn_ids_degrade_to_none(value: object) -> None:
    """No look-alike substitute is ever produced for an unusable capture id."""
    assert _verified_external_turn_id(value) is None


def test_verified_turn_id_is_kept_verbatim() -> None:
    assert _verified_external_turn_id(" dom-turn-77 ") == "dom-turn-77"


@pytest.mark.asyncio
async def test_bridge_reason_model_run_is_reconcilable_and_resumes_same_generation() -> None:
    """The exact production incident, end to end on the application side.

    The ModelRun carries the real bridge reason (`active_signal_stalled`), not
    the state machine's reconciliation code; it must still be previewable and
    adoptable, and adoption must resume the same pipeline generation.
    """
    service, uow, gateway, bridge, jobs = _build_fixture()
    run = next(iter(uow.subject_production_runs.runs.values()))
    run.current_stage = SubjectProductionStage.REFERENCES
    run.reconciliation = ProductionSubmissionReconciliation(
        production_run_id=run.id,
        model_run_id=gateway.model.id,
        stage=SubjectProductionStage.REFERENCES,
        bridge_response_id="bridge-1",
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        phase="reconciliation",
    )
    gateway.model.error_code = "active_signal_stalled"

    preview = await service.preview_visible(run.id)
    assert preview.stage == "references"
    assert preview.external_turn_id == "dom-turn-77"

    adopted = await service.adopt_visible(run.id, preview.sha256, actor_id="analyst")

    assert gateway.model.status is ModelRunStatus.SUCCEEDED
    assert run.status is SubjectProductionStatus.RUNNING
    assert run.pipeline_generation == 7
    assert adopted["pipeline_generation"] == 7
    assert adopted["provenance"] == "visible_recovery"
    assert gateway.adoptions[0]["external_turn_id"] == "dom-turn-77"
    assert jobs.submissions == 1
    # One explicit operator preview, then the adoption's own hash-confirmed read.
    assert bridge.previews == 2


@pytest.mark.asyncio
async def test_external_turn_identity_unavailable_is_reconcilable_never_retried() -> None:
    """Un texte final sans identité externe stable reste adoptable par un humain.

    Le bridge a bien sérialisé la réponse, mais aucun `data-message-id` durable
    n'a été posé : le run ne peut pas promettre un CONTINUE routable. Ce motif
    appartient donc au jeu des réconciliations post-soumission explicites — pas
    au retry ordinaire, qui resoumettrait le prompt.
    """
    assert model_run_awaits_reconciliation("external_turn_identity_unavailable")

    service, uow, gateway, _bridge, jobs = _build_fixture()
    run = next(iter(uow.subject_production_runs.runs.values()))
    run.current_stage = SubjectProductionStage.REFERENCES
    run.reconciliation = ProductionSubmissionReconciliation(
        production_run_id=run.id,
        model_run_id=gateway.model.id,
        stage=SubjectProductionStage.REFERENCES,
        bridge_response_id="bridge-1",
        submission_state=ModelSubmissionState.SUBMITTED_OR_UNKNOWN,
        phase="reconciliation",
    )
    gateway.model.error_code = "external_turn_identity_unavailable"

    preview = await service.preview_visible(run.id)
    adopted = await service.adopt_visible(run.id, preview.sha256, actor_id="analyst")

    assert gateway.model.status is ModelRunStatus.SUCCEEDED
    assert adopted["provenance"] == "visible_recovery"
    # Aucune resoumission : l'adoption reprend la même génération de pipeline.
    assert jobs.submissions == 1
