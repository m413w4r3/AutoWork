from __future__ import annotations

import json
from dataclasses import replace
from types import SimpleNamespace
from uuid import UUID

import pytest

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_references import production_reference_corpus_to_json
from cti_app.application.production_repairs import (
    ProductionRepairMaterializationService,
    ProductionRepairProjectionError,
    ProductionRepairProjectionResult,
    ProductionRepairStaleError,
)
from cti_app.application.production_synthesis import canonical_extraction_hash
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionDerivedOutput,
    ProductionRepairImpact,
    ProductionRepairImpactKind,
    ProductionRun,
)
from cti_app.domain.production_editorial_enrichment import editorial_enrichment_to_json
from cti_app.domain.production_extraction import production_extraction_to_json
from cti_app.domain.production_pipeline import production_artifact_stages
from cti_app.domain.production_synthesis import production_synthesis_to_json
from cti_app.domain.publication_document import serialize_publication_document
from tests.editorial_enrichment_support import build_empty_editorial_enrichment
from tests.test_publication_builder_v4 import _canonical_inputs

EDITION_ID = UUID(int=4)
SUBJECT_ID = UUID(int=1)
RUN_ID = UUID(int=3)


class _CanonicalStore:
    def __init__(self, payloads: dict[UUID, dict[str, object]]) -> None:
        self.payloads = payloads
        self.next_id = 100
        self.failure: Exception | None = None

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self.payloads[blob_id]

    async def read_bytes(self, blob_id: UUID) -> bytes:
        return ProductionArtifactStore.canonical_json_bytes(self.payloads[blob_id])

    async def store_stage_payloads(
        self, *, canonical: dict[str, object], rendered: str | None = None
    ) -> tuple[UUID | None, UUID | None, UUID | None]:
        if self.failure is not None:
            raise self.failure
        blob_id = UUID(int=self.next_id)
        self.next_id += 1
        self.payloads[blob_id] = canonical
        return None, blob_id, None


def _impact(kind: ProductionRepairImpactKind) -> ProductionRepairImpact:
    outputs = {
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY: {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.EDITORIAL_ENRICHMENT,
            ProductionDerivedOutput.RULE_BUNDLE,
            ProductionDerivedOutput.CHECKPOINT,
        },
        ProductionRepairImpactKind.PUBLICATION_ONLY: {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.EDITORIAL_ENRICHMENT,
            ProductionDerivedOutput.PUBLICATION,
            ProductionDerivedOutput.CHECKPOINT,
        },
        ProductionRepairImpactKind.NARRATIVE: {
            ProductionDerivedOutput.EXTRACTION,
            ProductionDerivedOutput.SYNTHESIS,
            ProductionDerivedOutput.EDITORIAL_ENRICHMENT,
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
    def __init__(self, initial: list[ProductionArtifact]) -> None:
        self.items = initial
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
        return max(values, key=lambda item: item.version, default=None)

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def find_reusable(self, **_kwargs: object) -> ProductionArtifact | None:
        return None

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)
        self._journal.append(("append", artifact))

    async def mark_stages_stale(self, run_id: UUID, stages: set[str]) -> list[str]:
        selected = tuple(
            stage.value for stage in production_artifact_stages() if stage.value in stages
        )
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
                self.items.remove(payload)  # type: ignore[arg-type]
            else:
                for item, status in payload:  # type: ignore[union-attr]
                    item.status = status
        self._journal.clear()


class _Uow:
    def __init__(
        self, snapshot, artifacts: _Artifacts, *, edition_state=EditionStatus.OPEN
    ) -> None:
        self.artifacts = artifacts
        self.run = ProductionRun(id=RUN_ID, edition_id=EDITION_ID, subject_id=SUBJECT_ID)
        self.run.status = "ready"
        self.run.pipeline_generation = 4
        self.production_runs = SimpleNamespace(
            get=self._run,
            get_for_update=self._run,
            get_current_for_subject=self._run,
        )
        self.production_artifacts = artifacts
        self.editions = SimpleNamespace(
            get_for_update=lambda _edition_id: self._edition(edition_state)
        )
        self.production_input_snapshots = SimpleNamespace(
            get_by_run=lambda _run_id: self._snapshot(snapshot)
        )
        self.commits = 0
        self.rolled_back = False

    async def _run(self, _run_id: UUID) -> ProductionRun:
        return self.run

    async def _edition(self, state: EditionStatus) -> object:
        return SimpleNamespace(state=state)

    async def _snapshot(self, snapshot) -> object:
        return snapshot

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        if self.commits == 0:
            self.rolled_back = True
            self.artifacts.rollback()

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

    async def project_effective_extraction_in_uow(self, uow: _Uow, **_kwargs: object):
        self.calls += 1
        if self.result.changed:
            await uow.production_artifacts.append(self.result.artifact)
        return self.result


class _Checkpoint:
    def __init__(self) -> None:
        self.calls: list[UUID] = []

    async def checkpoint(self, run_id: UUID) -> object:
        self.calls.append(run_id)
        return SimpleNamespace(rule_sidecar_error=None)


def _service(
    kind: ProductionRepairImpactKind,
    *,
    edition_state: EditionStatus = EditionStatus.OPEN,
    diagnostics: DiagnosticsLog | None = None,
) -> tuple[ProductionRepairMaterializationService, _Uow, _Projection, _CanonicalStore, _Checkpoint]:
    snapshot, references, extraction, synthesis = _canonical_inputs()
    enrichment = build_empty_editorial_enrichment(extraction=extraction, synthesis=synthesis)
    payloads = {
        UUID(int=10): production_reference_corpus_to_json(references),
        UUID(int=11): production_extraction_to_json(extraction),
        UUID(int=12): production_synthesis_to_json(synthesis),
        UUID(int=13): editorial_enrichment_to_json(enrichment),
    }
    from cti_app.application.publication_builder import build_publication_document_v4

    store = _CanonicalStore(payloads)
    document = build_publication_document_v4(
        snapshot=snapshot,
        references=references,
        extraction=extraction,
        synthesis=synthesis,
        editorial_enrichment=enrichment,
    )
    store.payloads[UUID(int=14)] = serialize_publication_document(document)
    initial = [
        ProductionArtifact(
            production_run_id=RUN_ID,
            subject_id=SUBJECT_ID,
            stage=stage,
            version=1,
            input_hash=f"{index}" * 64,
            canonical_blob_id=UUID(int=10 + index),
        )
        for index, stage in enumerate(
            (
                ProductionArtifactStage.REFERENCES,
                ProductionArtifactStage.EXTRACTION,
                ProductionArtifactStage.SYNTHESIS,
                ProductionArtifactStage.EDITORIAL_ENRICHMENT,
                ProductionArtifactStage.PUBLICATION,
            ),
            start=0,
        )
    ]
    uow = _Uow(snapshot, _Artifacts(initial), edition_state=edition_state)
    changed_artifact = replace(
        initial[1],
        version=2,
        input_hash=canonical_extraction_hash(extraction),
        canonical_blob_id=UUID(int=11),
    )
    projection = _Projection(
        ProductionRepairProjectionResult(
            artifact=changed_artifact,
            changed=kind is not ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE,
            impact=_impact(kind),
        )
    )
    checkpoint = _Checkpoint()
    service = ProductionRepairMaterializationService(
        _Factory(uow),
        projection_service=projection,
        checkpoint_service=checkpoint,
        artifact_store=store,
        diagnostics=diagnostics,
    )
    return service, uow, projection, store, checkpoint


@pytest.mark.asyncio
async def test_canonical_rule_bundle_repair_reassembles_and_materializes_sidecars() -> None:
    service, uow, projection, _store, checkpoint = _service(
        ProductionRepairImpactKind.RULE_BUNDLE_ONLY
    )
    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert result.action == "rules_materialized"
    assert projection.calls == 1
    assert checkpoint.calls == [RUN_ID]
    assert uow.commits == 1
    assert uow.artifacts.stale_calls == [
        (RUN_ID, ("synthesis",)),
        (RUN_ID, ("editorial_enrichment", "publication")),
    ]


@pytest.mark.asyncio
async def test_canonical_publication_materialization_is_one_transaction() -> None:
    service, uow, projection, _store, _checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY
    )
    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert result.action == "publication_reassembled"
    assert result.publication_artifact is not None
    assert projection.calls == 1
    assert uow.commits == 1
    assert uow.rolled_back is False


@pytest.mark.asyncio
async def test_canonical_assembly_failure_rolls_back_the_repair() -> None:
    service, uow, _projection, store, _checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY
    )
    store.failure = RuntimeError("canonical assembly failed")
    with pytest.raises(RuntimeError, match="canonical assembly failed"):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert uow.commits == 0
    assert uow.rolled_back is True
    assert len(uow.artifacts.items) == 5
    assert all(item.status is not ProductionArtifactStatus.STALE for item in uow.artifacts.items)


@pytest.mark.asyncio
async def test_canonical_qa_failure_rolls_back_the_repair(monkeypatch: pytest.MonkeyPatch) -> None:
    service, uow, _projection, _store, _checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY
    )

    async def failed_qa(*_args: object, **_kwargs: object) -> dict[str, object]:
        return {"passed": False, "checks": {}, "errors": ["forced"], "warnings": []}

    monkeypatch.setattr(
        "cti_app.application.production_repairs.ProductionQAService.run_qa", failed_qa
    )
    with pytest.raises(ProductionRepairProjectionError, match="production_repair_qa_failed"):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert uow.commits == 0
    assert uow.rolled_back is True


@pytest.mark.asyncio
async def test_narrative_repair_stales_outputs_without_materializing() -> None:
    service, uow, _projection, _store, checkpoint = _service(ProductionRepairImpactKind.NARRATIVE)
    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert result.action == "retry_required"
    assert result.retry_stage == "synthesis"
    assert checkpoint.calls == []
    assert uow.artifacts.stale_calls == [
        (RUN_ID, ("synthesis", "editorial_enrichment", "publication"))
    ]


@pytest.mark.asyncio
async def test_no_deliverable_change_does_not_stale_or_checkpoint() -> None:
    service, uow, _projection, _store, checkpoint = _service(
        ProductionRepairImpactKind.NO_DELIVERABLE_CHANGE
    )
    result = await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert result.action == "none"
    assert uow.artifacts.stale_calls == []
    assert checkpoint.calls == []


@pytest.mark.asyncio
async def test_archived_edition_and_stale_generation_are_rejected_before_projection() -> None:
    service, _uow, projection, _store, _checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY, edition_state=EditionStatus.ARCHIVED
    )
    with pytest.raises(ProductionRepairProjectionError, match="edition_archived"):
        await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    assert projection.calls == 0

    service, uow, projection, _store, _checkpoint = _service(
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
    assert projection.calls == 0


@pytest.mark.asyncio
async def test_repair_diagnostics_record_the_canonical_outcome(tmp_path) -> None:
    diagnostics = DiagnosticsLog.from_env(tmp_path / "diagnostics")
    service, _uow, _projection, _store, _checkpoint = _service(
        ProductionRepairImpactKind.PUBLICATION_ONLY, diagnostics=diagnostics
    )
    await service.apply(edition_id=EDITION_ID, subject_id=SUBJECT_ID, actor_id="analyst")
    events = [
        json.loads(line)
        for line in (tmp_path / "diagnostics" / "events.jsonl").read_text().splitlines()
    ]
    applied = next(event for event in events if event["event"] == "production.repair.applied")
    assert applied["action"] == "publication_reassembled"
    assert applied["projection_changed"] is True
