from __future__ import annotations

import json
from types import SimpleNamespace
from typing import cast
from uuid import UUID

import pytest

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.production_parsers import ReferenceReport, TechnicalExtraction
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionError,
    ProductionRepairProjectionResult,
    ProductionRepairStaleError,
)
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionDerivedOutput,
    ProductionRepairImpact,
    ProductionRepairImpactKind,
    SubjectProductionStatus,
)


class _Unset:
    """Sentinel: ``None`` is a meaningful checkpoint outcome (failure)."""


_UNSET = _Unset()

EDITION_ID = UUID("aaaaaaaa-aaaa-4aaa-8aaa-aaaaaaaaaaaa")
SUBJECT_ID = UUID("bbbbbbbb-bbbb-4bbb-8bbb-bbbbbbbbbbbb")
RUN_ID = UUID("cccccccc-cccc-4ccc-8ccc-cccccccccccc")


def _artifact(stage: ProductionArtifactStage, version: int = 1) -> ProductionArtifact:
    return ProductionArtifact(
        production_run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        stage=stage,
        version=version,
        input_hash=(f"{version:x}" * 64)[:64],
    )


def _impact(kind: ProductionRepairImpactKind) -> ProductionRepairImpact:
    outputs = {
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY: {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.RULE_BUNDLE,
            ProductionDerivedOutput.CHECKPOINT,
        },
        ProductionRepairImpactKind.PUBLICATION_ONLY: {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.PUBLICATION,
            ProductionDerivedOutput.CHECKPOINT,
        },
        ProductionRepairImpactKind.NARRATIVE: {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.SYNTHESIS,
            ProductionDerivedOutput.PUBLICATION,
            ProductionDerivedOutput.CHECKPOINT,
        },
        ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE: set(),
    }[kind]
    return ProductionRepairImpact(
        kind=kind,
        affected_outputs=frozenset(outputs),
        model_call_required=kind is ProductionRepairImpactKind.NARRATIVE,
        reason=kind.value,
    )


class _Artifacts:
    """In-memory rows with an undoable journal, so a rollback is observable."""

    def __init__(self) -> None:
        self.items = [
            _artifact(ProductionArtifactStage.REFERENCES),
            _artifact(ProductionArtifactStage.EXTRACTION),
            _artifact(ProductionArtifactStage.SYNTHESIS),
            _artifact(ProductionArtifactStage.PUBLICATION),
        ]
        self.stale_calls: list[tuple[UUID, tuple[str, ...]]] = []
        self._journal: list[tuple[str, object]] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        values = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(values, key=lambda item: item.version) if values else None

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)
        self._journal.append(("append", artifact))

    async def mark_stages_stale(self, run_id: UUID, stages: set[str]) -> list[str]:
        order = ("references", "extraction", "synthesis", "publication")
        selected = tuple(stage for stage in order if stage in stages)
        self.stale_calls.append((run_id, selected))
        restored: list[tuple[ProductionArtifact, ProductionArtifactStatus]] = []
        for item in self.items:
            if item.production_run_id == run_id and item.stage.value in selected:
                restored.append((item, item.status))
                item.status = ProductionArtifactStatus.STALE
        self._journal.append(("stale", restored))
        return list(selected)

    def commit(self) -> None:
        self._journal.clear()

    def rollback(self) -> None:
        for operation, payload in reversed(self._journal):
            if operation == "append":
                self.items.remove(cast(ProductionArtifact, payload))
            else:
                for item, status in cast(
                    "list[tuple[ProductionArtifact, ProductionArtifactStatus]]", payload
                ):
                    item.status = status
        self._journal.clear()


class _Uow:
    def __init__(self, edition_status: EditionStatus = EditionStatus.REVIEW) -> None:
        self.artifacts = _Artifacts()
        self.run = SimpleNamespace(
            id=RUN_ID,
            edition_id=EDITION_ID,
            subject_id=SUBJECT_ID,
            pipeline_generation=4,
            status=SubjectProductionStatus.READY,
            requires_reconciliation=False,
            research_date=None,
        )
        self.subject_production_runs = SimpleNamespace(
            get=lambda _run_id: self._run(),
            get_current_for_subject=lambda _subject_id: self._run(),
            get_for_update=lambda _run_id: self._run(),
        )
        self.production_artifacts = self.artifacts
        self.editions = SimpleNamespace(
            get_for_update=lambda _edition_id: self._edition(edition_status),
        )
        self.publication_manifests = SimpleNamespace(
            get_latest_for_edition=lambda _edition_id: self._none(),
        )
        self.production_input_snapshots = SimpleNamespace(
            get_by_run=lambda _run_id: self._snapshot(),
        )
        self.source_collections = SimpleNamespace(
            list_for_subject=lambda _subject_id: self._collections(),
        )
        self.commits = 0
        self.rolled_back = False

    async def _run(self) -> object:
        return self.run

    async def _edition(self, status: EditionStatus) -> object:
        return SimpleNamespace(status=status)

    async def _none(self) -> None:
        return None

    async def _snapshot(self) -> object:
        return SimpleNamespace(subject_title="Subject")

    async def _collections(self) -> list[object]:
        return []

    async def __aenter__(self) -> _Uow:
        self._open_commits = self.commits
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self._open_commits == self.commits:
            self.rolled_back = True
            self.artifacts.rollback()
        return None

    async def commit(self) -> None:
        self.commits += 1
        self.artifacts.commit()


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self) -> _Uow:
        return self.uow


class _Projection:
    def __init__(self, result: ProductionRepairProjectionResult) -> None:
        self.result = result
        self.calls = 0

    async def project_effective_extraction_in_uow(
        self,
        uow: _Uow,
        *,
        run: object,
        actor_id: str,
        expected_pipeline_generation: int | None = None,
    ) -> ProductionRepairProjectionResult:
        assert actor_id == "analyst"
        assert expected_pipeline_generation == getattr(run, "pipeline_generation", None)
        self.calls += 1
        if self.result.changed:
            await uow.production_artifacts.append(self.result.artifact)
        return self.result


class _Assembly:
    def __init__(self, error: Exception | None = None) -> None:
        self.calls = 0
        self.error = error

    async def _load_inputs(
        self, _references: object, _extraction: object, _synthesis: object
    ) -> tuple[ReferenceReport, TechnicalExtraction, str]:
        return (
            ReferenceReport(sources=(), events=()),
            TechnicalExtraction(items=(), rules=()),
            "text",
        )

    async def assemble_publication_in_uow(
        self,
        uow: _Uow,
        run_id: UUID,
        subject_id: UUID,
        _title: str,
        _references: ProductionArtifact,
        _extraction: ProductionArtifact,
        _synthesis: ProductionArtifact,
    ) -> ProductionArtifact:
        self.calls += 1
        if self.error is not None:
            raise self.error
        artifact = ProductionArtifact(
            production_run_id=run_id,
            subject_id=subject_id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=2,
            input_hash="e" * 64,
        )
        await uow.production_artifacts.append(artifact)
        return artifact


class _QA:
    def __init__(self, passed: bool = True) -> None:
        self.calls = 0
        self.passed = passed

    async def run_qa(self, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return {
            "passed": self.passed,
            "checks": {},
            "errors": [] if self.passed else ["publication_qa_failed"],
            "warnings": [],
        }


class _Checkpoint:
    def __init__(self, result: object | _Unset = _UNSET) -> None:
        self.calls: list[UUID] = []
        self.result = (
            SimpleNamespace(rule_sidecar_error=None) if isinstance(result, _Unset) else result
        )

    async def checkpoint(self, run_id: UUID) -> object:
        self.calls.append(run_id)
        return self.result


def _service(
    kind: ProductionRepairImpactKind,
    *,
    edition_status: EditionStatus = EditionStatus.REVIEW,
    diagnostics: DiagnosticsLog | None = None,
    assembly_error: Exception | None = None,
    qa_passed: bool = True,
    checkpoint_result: object | _Unset = _UNSET,
) -> tuple[ProductionRepairMaterializationService, _Uow, _Projection, _Assembly, _QA, _Checkpoint]:
    uow = _Uow(edition_status)
    projection = _Projection(
        ProductionRepairProjectionResult(
            artifact=_artifact(ProductionArtifactStage.EXTRACTION, version=2),
            changed=kind is not ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            impact=_impact(kind),
        )
    )
    assembly = _Assembly(assembly_error)
    qa = _QA(qa_passed)
    checkpoint = _Checkpoint(checkpoint_result)
    service = ProductionRepairMaterializationService(
        _Factory(uow),
        projection_service=projection,  # type: ignore[arg-type]
        publication_assembly_service=assembly,  # type: ignore[arg-type]
        qa_service=qa,  # type: ignore[arg-type]
        checkpoint_service=checkpoint,
        artifact_store=object(),  # make QA path execute
        diagnostics=diagnostics,
    )
    return service, uow, projection, assembly, qa, checkpoint


@pytest.mark.asyncio
async def test_rules_materialization_does_not_stale_downstream() -> None:
    service, uow, _projection, assembly, qa, checkpoint = _service(
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY
    )

    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert result.action == "rules_materialized"
    assert uow.artifacts.stale_calls == []
    assert assembly.calls == 0
    assert qa.calls == 1
    assert checkpoint.calls == [RUN_ID]


@pytest.mark.asyncio
async def test_publication_materialization_stales_only_publication() -> None:
    service, uow, _projection, assembly, _qa, checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY
    )

    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert result.action == "publication_reassembled"
    assert uow.artifacts.stale_calls == [(RUN_ID, ("publication",))]
    assert assembly.calls == 1
    assert checkpoint.calls == [RUN_ID]
    assert result.publication_artifact is not None


@pytest.mark.asyncio
async def test_narrative_materialization_stales_two_outputs_without_dispatch() -> None:
    service, uow, _projection, assembly, qa, checkpoint = _service(
        ProductionRepairImpactKind.NARRATIVE
    )

    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert result.action == "retry_required"
    assert result.retry_stage == "synthesis"
    assert uow.artifacts.stale_calls == [(RUN_ID, ("synthesis", "publication"))]
    assert assembly.calls == 0
    assert qa.calls == 0
    assert checkpoint.calls == []


@pytest.mark.asyncio
async def test_no_deliverable_change_does_not_stale_or_checkpoint() -> None:
    service, uow, _projection, assembly, qa, checkpoint = _service(
        ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    )

    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert result.action == "none"
    assert uow.artifacts.stale_calls == []
    assert assembly.calls == qa.calls == 0
    assert checkpoint.calls == []


@pytest.mark.asyncio
async def test_materialization_refuses_a_frozen_edition() -> None:
    service, _uow, projection, _assembly, _qa, _checkpoint = _service(
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        edition_status=EditionStatus.PUBLISHED,
    )

    with pytest.raises(ProductionRepairProjectionError, match="edition_frozen_for_publication"):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert projection.calls == 0


@pytest.mark.asyncio
async def test_materialization_diagnostics_are_structured_and_value_free(tmp_path) -> None:
    diagnostics = DiagnosticsLog.from_env(tmp_path / "diagnostics")
    service, _uow, _projection, _assembly, _qa, _checkpoint = _service(
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        diagnostics=diagnostics,
    )

    await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    events = [
        json.loads(line)
        for line in (tmp_path / "diagnostics" / "events.jsonl").read_text().splitlines()
    ]
    names = {event["event"] for event in events}
    assert {
        "production.repair.plan",
        "production.repair.projection_completed",
        "production.repair.rule_bundle_materialized",
    } <= names
    for event in events:
        if event["event"].startswith("production.repair."):
            assert event["run_id"] == str(RUN_ID)
            assert event["subject_id"] == str(SUBJECT_ID)
            assert event["impact_kind"] == ProductionRepairImpactKind.RULE_BUNDLE_ONLY.value
            assert event["affected_outputs"]
            assert isinstance(event["model_call_required"], bool)
            assert isinstance(event["reused_synthesis"], bool)
            assert isinstance(event["duration_ms"], int)


@pytest.mark.asyncio
async def test_publication_materialization_is_a_single_transaction() -> None:
    """The projection, the stale, the assembly and the QA share one commit."""
    service, uow, projection, assembly, qa, _checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY
    )

    await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert projection.calls == assembly.calls == qa.calls == 1
    assert uow.commits == 1
    assert uow.rolled_back is False


@pytest.mark.asyncio
async def test_publication_assembly_failure_leaves_the_article_untouched() -> None:
    """An Assembly error never leaves a decided IOC out of the document."""
    service, uow, _projection, assembly, qa, checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY,
        assembly_error=RuntimeError("pandoc exploded"),
    )

    with pytest.raises(RuntimeError, match="pandoc exploded"):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert uow.commits == 0
    assert uow.rolled_back is True
    assert assembly.calls == 1
    assert qa.calls == 0
    assert checkpoint.calls == []
    extraction = await uow.artifacts.get_current(RUN_ID, "extraction")
    publication = await uow.artifacts.get_current(RUN_ID, "publication")
    assert extraction is not None and extraction.version == 1
    assert publication is not None and publication.version == 1
    assert publication.status is ProductionArtifactStatus.VERIFIED
    assert len(uow.artifacts.items) == 4


@pytest.mark.asyncio
async def test_publication_qa_failure_leaves_the_article_untouched() -> None:
    """A QA failure rolls the repaired Extraction back with the document."""
    service, uow, _projection, assembly, qa, checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY,
        qa_passed=False,
    )

    with pytest.raises(ProductionRepairProjectionError, match="production_repair_qa_failed"):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert uow.commits == 0
    assert uow.rolled_back is True
    assert assembly.calls == qa.calls == 1
    assert checkpoint.calls == []
    extraction = await uow.artifacts.get_current(RUN_ID, "extraction")
    publication = await uow.artifacts.get_current(RUN_ID, "publication")
    assert extraction is not None and extraction.version == 1
    assert publication is not None and publication.version == 1
    assert publication.status is ProductionArtifactStatus.VERIFIED


@pytest.mark.asyncio
async def test_publication_retry_after_a_failure_succeeds() -> None:
    """The repair debt survives the failure and the retry materializes it."""
    service, uow, _projection, assembly, _qa, _checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY,
        assembly_error=RuntimeError("transient"),
    )
    with pytest.raises(RuntimeError):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assembly.error = None
    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert result.action == "publication_reassembled"
    assert uow.commits == 1
    publication = await uow.artifacts.get_current(RUN_ID, "publication")
    assert publication is not None and publication.version == 2


@pytest.mark.asyncio
async def test_narrative_failure_before_stale_commits_no_projection() -> None:
    """A narrative repair never commits the Extraction without the stale."""
    service, uow, _projection, _assembly, _qa, _checkpoint = _service(
        ProductionRepairImpactKind.NARRATIVE
    )

    async def _explode(_run_id: UUID, _stages: set[str]) -> list[str]:
        raise RuntimeError("stale port down")

    uow.artifacts.mark_stages_stale = _explode  # type: ignore[method-assign]

    with pytest.raises(RuntimeError, match="stale port down"):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert uow.commits == 0
    assert uow.rolled_back is True
    extraction = await uow.artifacts.get_current(RUN_ID, "extraction")
    synthesis = await uow.artifacts.get_current(RUN_ID, "synthesis")
    assert extraction is not None and extraction.version == 1
    assert synthesis is not None and synthesis.status is ProductionArtifactStatus.VERIFIED


@pytest.mark.asyncio
async def test_rule_bundle_checkpoint_failure_is_reported_as_pending() -> None:
    """A failed sidecar projection never claims the rules were materialized."""
    service, uow, _projection, _assembly, _qa, checkpoint = _service(
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        checkpoint_result=None,
    )

    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert result.action == "rules_projection_pending"
    assert checkpoint.calls == [RUN_ID]
    # The canonical Extraction keeps the decision: nothing is lost, and no Q4
    # is replayed to recover the sidecars.
    assert uow.commits == 1
    extraction = await uow.artifacts.get_current(RUN_ID, "extraction")
    assert extraction is not None and extraction.version == 2
    assert uow.artifacts.stale_calls == []


@pytest.mark.asyncio
async def test_rule_bundle_sidecar_error_is_reported_as_pending() -> None:
    service, _uow, _projection, _assembly, _qa, _checkpoint = _service(
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        checkpoint_result=SimpleNamespace(rule_sidecar_error="permission denied"),
    )

    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")

    assert result.action == "rules_projection_pending"


@pytest.mark.asyncio
async def test_rule_bundle_projection_is_idempotently_replayable() -> None:
    """The pending projection can be replayed without touching production."""
    service, uow, _projection, _assembly, _qa, checkpoint = _service(
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY,
        checkpoint_result=None,
    )
    await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    commits = uow.commits

    checkpoint.result = SimpleNamespace(rule_sidecar_error=None)
    assert await service.materialize_rule_bundle_from_current_extraction(RUN_ID) is True
    assert await service.materialize_rule_bundle_from_current_extraction(RUN_ID) is True

    assert uow.commits == commits
    assert checkpoint.calls == [RUN_ID, RUN_ID, RUN_ID]


@pytest.mark.asyncio
async def test_a_stale_generation_refuses_before_any_projection() -> None:
    """A concurrent retry (generation N+1) stops the plan built from N."""
    service, uow, projection, assembly, qa, checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY
    )

    with pytest.raises(ProductionRepairStaleError):
        await service.apply(
            edition_id=EDITION_ID,
            subject_id=SUBJECT_ID,
            actor_id="analyst",
            observed_run_id=RUN_ID,
            observed_pipeline_generation=uow.run.pipeline_generation - 1,
        )

    assert projection.calls == assembly.calls == qa.calls == 0
    assert checkpoint.calls == []
    assert uow.commits == 0
    assert len(uow.artifacts.items) == 4
