from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.production_parsers import ReferenceReport, TechnicalExtraction
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionError,
    ProductionRepairProjectionResult,
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
    def __init__(self) -> None:
        self.items = [
            _artifact(ProductionArtifactStage.REFERENCES),
            _artifact(ProductionArtifactStage.EXTRACTION),
            _artifact(ProductionArtifactStage.SYNTHESIS),
            _artifact(ProductionArtifactStage.PUBLICATION),
        ]
        self.stale_calls: list[tuple[UUID, tuple[str, ...]]] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        values = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(values, key=lambda item: item.version) if values else None

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def mark_stages_stale(self, run_id: UUID, stages: set[str]) -> list[str]:
        order = ("references", "extraction", "synthesis", "publication")
        selected = tuple(stage for stage in order if stage in stages)
        self.stale_calls.append((run_id, selected))
        for item in self.items:
            if item.production_run_id == run_id and item.stage.value in selected:
                item.status = ProductionArtifactStatus.STALE
        return list(selected)


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
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1


class _Factory:
    def __init__(self, uow: _Uow) -> None:
        self.uow = uow

    def __call__(self) -> _Uow:
        return self.uow


class _Projection:
    def __init__(self, result: ProductionRepairProjectionResult) -> None:
        self.result = result
        self.calls = 0

    async def project_effective_extraction(
        self, _run_id: UUID, *, actor_id: str
    ) -> ProductionRepairProjectionResult:
        assert actor_id == "analyst"
        self.calls += 1
        return self.result


class _Assembly:
    def __init__(self) -> None:
        self.calls = 0

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
    def __init__(self) -> None:
        self.calls = 0

    async def run_qa(self, **_kwargs: object) -> dict[str, object]:
        self.calls += 1
        return {"passed": True, "checks": {}, "errors": [], "warnings": []}


class _Checkpoint:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def checkpoint(self, run_id: UUID) -> None:
        self.calls.append(run_id)


def _service(
    kind: ProductionRepairImpactKind,
    *,
    edition_status: EditionStatus = EditionStatus.REVIEW,
    diagnostics: DiagnosticsLog | None = None,
) -> tuple[ProductionRepairMaterializationService, _Uow, _Projection, _Assembly, _QA, _Checkpoint]:
    uow = _Uow(edition_status)
    projection = _Projection(
        ProductionRepairProjectionResult(
            artifact=_artifact(ProductionArtifactStage.EXTRACTION, version=2),
            changed=kind is not ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            impact=_impact(kind),
        )
    )
    assembly = _Assembly()
    qa = _QA()
    checkpoint = _Checkpoint()
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

    result = await service.apply(
        edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst"
    )

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

    result = await service.apply(
        edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst"
    )

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

    result = await service.apply(
        edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst"
    )

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

    result = await service.apply(
        edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst"
    )

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
