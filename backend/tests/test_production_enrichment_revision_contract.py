from __future__ import annotations

import copy
import hashlib
from dataclasses import asdict, replace
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest

import cti_app.application.production_enrichment_revision as revision_module
from cti_app.application.editorial_enrichment_revision import (
    targeted_request_identity,
    targeted_request_identity_payload,
)
from cti_app.application.editorial_revision_effects import (
    EditorialRevisionEffects,
    EditorialRevisionEffectsResult,
    EditorialRunGuard,
)
from cti_app.application.editorial_workspace.service import EditorialWorkspaceService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_enrichment_revision import (
    ProductionEditorialEnrichmentRevisionService,
)
from cti_app.application.production_prompts import (
    EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION,
    EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION,
    EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION,
)
from cti_app.domain.production import ProductionArtifactStage
from cti_app.domain.production_editorial_enrichment import (
    EDITORIAL_ENRICHMENT_V8_CURRENT_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_V9_POLICY_VERSION,
    EDITORIAL_ENRICHMENT_V10_POLICY_VERSION,
    EditorialEnrichmentElementKind,
    EnrichmentPlacementKind,
    EnrichmentPlacementV1,
    editorial_enrichment_from_json,
    editorial_enrichment_to_json,
)
from cti_app.domain.production_editorial_revision import (
    TargetedEditorialRevisionOperation,
)
from tests.test_production_editorial_enrichment_application import (
    _common_enrichment_request,
    _install_stale_enrichment_restore_history,
    _RecordingGateway,
    _revision_harness,
    _succeeded_text,
    _table_revision_wire,
)


class _RecordingRevisionEffects:
    def __init__(self, harness: SimpleNamespace, trace: list[tuple[str, object]]) -> None:
        self._harness = harness
        self._trace = trace

    async def acquire_run_guard(self, uow: object, production_run_id: UUID) -> EditorialRunGuard:
        self._trace.append(("acquire_run_guard", uow))
        assert production_run_id == self._harness.world.run.id
        return EditorialRunGuard(production_run_id)

    async def lock_run_row(self, uow: object, guard: EditorialRunGuard) -> object:
        self._trace.append(("lock_run_row", uow))
        assert guard.production_run_id == self._harness.world.run.id
        return self._harness.world.run

    async def current_snapshot_in_uow(self, uow: object, subject_id: UUID) -> object:
        self._trace.append(("current_snapshot_in_uow", uow))
        workspace = EditorialWorkspaceService(
            uow_factory=self._harness.service._enrichment._uow_factory,
            artifact_store=self._harness.world.store,
        )
        return await workspace.snapshot_record_in_uow(uow, subject_id)

    async def apply_after_write(self, uow: object, **values: Any) -> EditorialRevisionEffectsResult:
        self._trace.append(("apply_after_write", uow))
        assert values["stage"] is ProductionArtifactStage.EDITORIAL_ENRICHMENT
        assert values["artifact"].production_run_id == self._harness.world.run.id
        assert (
            hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(values["canonical_content"])
            ).hexdigest()
            == values["canonical_sha256"]
        )
        return EditorialRevisionEffectsResult(snapshot=values["base_snapshot"])


def _record_write_path(harness: SimpleNamespace) -> list[tuple[str, object]]:
    trace: list[tuple[str, object]] = []
    uow_factory = harness.service._enrichment._uow_factory

    def tracked_uow_factory() -> Any:
        uow = uow_factory()
        commit = uow.commit

        async def tracked_commit() -> None:
            trace.append(("commit", uow))
            await commit()

        uow.commit = tracked_commit
        return uow

    harness.service._enrichment._uow_factory = tracked_uow_factory
    persistence = harness.service._persistence
    store_result = persistence.store_editorial_enrichment_result

    async def tracked_store_result(**kwargs: Any) -> Any:
        trace.append(("persist_candidate", kwargs["uow"]))
        return await store_result(**kwargs)

    persistence.store_editorial_enrichment_result = tracked_store_result
    harness.service._revision_effects = _RecordingRevisionEffects(harness, trace)
    return trace


def _write_events(trace: list[tuple[str, object]]) -> list[tuple[str, object]]:
    return [
        (name, uow)
        for name, uow in trace
        if name
        in {
            "acquire_run_guard",
            "lock_run_row",
            "current_snapshot_in_uow",
            "persist_candidate",
            "apply_after_write",
            "commit",
        }
    ]


def _install_real_revision_effects(harness: SimpleNamespace) -> None:
    original_factory = harness.service._enrichment._uow_factory

    def effects_uow_factory() -> Any:
        uow = original_factory()

        async def no_op(*_args: object) -> None:
            return None

        async def no_proposals(_run_id: UUID) -> tuple[Any, ...]:
            return ()

        async def get_for_update(run_id: UUID) -> object | None:
            return harness.world.run if run_id == harness.world.run.id else None

        uow.editorial_proposals = SimpleNamespace(
            lock_adoption_for_run=no_op,
            list_proposed_for_run_for_update=no_proposals,
        )
        uow.production_runs.get_for_update = get_for_update
        return uow

    harness.service._enrichment._uow_factory = effects_uow_factory
    harness.service._persistence._uow_factory = effects_uow_factory
    workspace_service = EditorialWorkspaceService(
        uow_factory=effects_uow_factory,
        artifact_store=harness.world.store,
    )
    harness.service._revision_effects = EditorialRevisionEffects(
        workspace_service=workspace_service
    )


def _without_created_at(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_created_at(item) for key, item in value.items() if key != "created_at"
        }
    if isinstance(value, list):
        return [_without_created_at(item) for item in value]
    return value


async def _persistent_revision_state(harness: SimpleNamespace) -> tuple[Any, Any]:
    snapshot_repository = harness.service._enrichment._uow_factory().editorial_workspace_snapshots
    snapshots = []
    for record in snapshot_repository.records:
        state = asdict(record)
        state.pop("snapshot_id")
        state["parent_snapshot_id"] = record.parent_snapshot_id is not None
        snapshots.append(state)

    artifacts = []
    for artifact in harness.artifacts.artifacts:
        canonical_sha256 = None
        if artifact.canonical_blob_id is not None:
            canonical_sha256 = hashlib.sha256(
                await harness.world.store.read_bytes(artifact.canonical_blob_id)
            ).hexdigest()
        artifacts.append(
            (
                (None if artifact.stage is ProductionArtifactStage.PUBLICATION else artifact.id),
                artifact.stage,
                artifact.version,
                artifact.input_hash,
                artifact.status,
                canonical_sha256,
                _without_created_at(artifact.metadata),
            )
        )
    return snapshots, artifacts


@pytest.mark.asyncio
async def test_legacy_enrichment_revise_uses_shared_effects_in_one_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    assert isinstance(harness.service, ProductionEditorialEnrichmentRevisionService)
    trace = _record_write_path(harness)
    request = _common_enrichment_request(
        harness,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        operation=TargetedEditorialRevisionOperation.REWRITE_TABLE,
        element_key="tools_table",
    )

    result = await harness.service.revise_targeted(request)

    assert result.refusal is None
    assert result.artifact is not None
    assert len(harness.world.gateway.calls) == 1
    events = _write_events(trace)
    write_uow = next(uow for name, uow in events if name == "persist_candidate")
    events = [(name, uow) for name, uow in events if uow is write_uow]
    assert [name for name, _uow in events] == [
        "acquire_run_guard",
        "lock_run_row",
        "current_snapshot_in_uow",
        "persist_candidate",
        "apply_after_write",
        "commit",
    ]
    assert all(uow is write_uow for _name, uow in events)
    assert result.revision is not None
    identity_payload = targeted_request_identity_payload(request)
    assert result.revision["request_identity"] == targeted_request_identity(request)
    assert (
        result.revision["request_identity"]
        == hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(identity_payload)
        ).hexdigest()
    )
    assert identity_payload["prompt_version"] == EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION
    assert identity_payload["contract_version"] == EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION
    assert identity_payload["parser_version"] == EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION
    assert identity_payload["validator_version"] == "editorial-enrichment-validator-v1"
    model_request = harness.world.gateway.calls[0][0]
    assert model_request.prompt_template_version == EDITORIAL_ENRICHMENT_REVISION_PROMPT_VERSION
    assert model_request.parameters["contract_version"] == (
        EDITORIAL_ENRICHMENT_REVISION_CONTRACT_VERSION
    )
    assert model_request.metadata["revision_request"] == identity_payload
    assert result.revision["parser_version"] == EDITORIAL_ENRICHMENT_WIRE_PARSER_VERSION
    assert result.revision["validator_version"] == "editorial-enrichment-validator-v2-source-waiver"
    assert (
        harness.world.gateway.calls[0][0].metadata["editorial_revision_request_identity"]
        == (result.revision["request_identity"])
    )


@pytest.mark.asyncio
async def test_legacy_enrichment_replay_returns_same_artifact(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    request = _common_enrichment_request(
        harness,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        operation=TargetedEditorialRevisionOperation.REWRITE_TABLE,
        element_key="tools_table",
    )
    original_base_bytes = await harness.world.store.read_bytes(harness.base.canonical_blob_id)

    first = await harness.service.revise_targeted(request)
    assert first.artifact is not None and first.revision is not None
    assert first.artifact.canonical_blob_id is not None
    first_bytes = await harness.world.store.read_bytes(first.artifact.canonical_blob_id)
    artifact_count = len(harness.artifacts.artifacts)

    replay = await harness.service.revise_targeted(request)

    assert replay.artifact is not None and replay.revision is not None
    assert replay.artifact.canonical_blob_id is not None
    assert (
        replay.artifact.id,
        replay.artifact.canonical_blob_id,
        replay.artifact.model_run_id,
    ) == (first.artifact.id, first.artifact.canonical_blob_id, first.artifact.model_run_id)
    replay_bytes = await harness.world.store.read_bytes(replay.artifact.canonical_blob_id)
    assert replay_bytes == first_bytes
    assert hashlib.sha256(replay_bytes).digest() == hashlib.sha256(first_bytes).digest()
    assert replay.revision["request_identity"] == first.revision["request_identity"]
    assert len(harness.artifacts.artifacts) == artifact_count
    assert len(harness.world.gateway.calls) == 1
    assert (
        await harness.world.store.read_bytes(harness.base.canonical_blob_id) == original_base_bytes
    )


@pytest.mark.asyncio
async def test_enrichment_preview_never_writes(monkeypatch: pytest.MonkeyPatch) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    request = _common_enrichment_request(
        harness,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        operation=TargetedEditorialRevisionOperation.REWRITE_TABLE,
        element_key="tools_table",
    )
    artifacts_before = copy.deepcopy(harness.artifacts.artifacts)
    snapshot_repository = harness.service._enrichment._uow_factory().editorial_workspace_snapshots
    snapshots_before = copy.deepcopy(snapshot_repository.records)
    payloads_before = copy.deepcopy(harness.world.store.payloads)

    preview = await harness.service.preview_targeted(request)

    assert preview.expected_model_calls == 1
    assert harness.artifacts.artifacts == artifacts_before
    assert snapshot_repository.records == snapshots_before
    assert harness.world.store.payloads == payloads_before
    assert harness.world.gateway.calls == []
    assert harness.world.writer.calls == []


@pytest.mark.asyncio
async def test_targeted_enrichment_revise_advances_snapshot_and_registry_without_preview_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, "unexpected")),
    )
    _install_real_revision_effects(harness)
    monkeypatch.setattr(
        revision_module,
        "_ReadOnlyWorkspaceSnapshotUow",
        lambda *_args: pytest.fail("write path used the preview-only snapshot wrapper"),
    )
    request = _common_enrichment_request(
        harness,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        operation=TargetedEditorialRevisionOperation.DELETE_TABLE,
        element_key="tools_table",
    )

    result = await harness.service.revise_targeted(request)

    assert result.artifact is not None
    records = harness.service._enrichment._uow_factory().editorial_workspace_snapshots.records
    assert len(records) >= 2
    base_record = records[0]
    advanced_record = next(
        record
        for record in records[1:]
        if record.parent_snapshot_id == base_record.snapshot_id
        and record.enrichment_artifact_id == result.artifact.id
    )
    assert advanced_record.parent_snapshot_id == base_record.snapshot_id
    assert advanced_record.enrichment_artifact_id == result.artifact.id
    assert advanced_record.enrichment_version == result.artifact.version
    assert advanced_record.registry_hash != base_record.registry_hash


@pytest.mark.asyncio
async def test_stale_enrichment_restore_advances_snapshot_and_registry_without_preview_wrapper(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, "unexpected")),
        include_figure=True,
        include_target_annotations=True,
    )
    await _install_stale_enrichment_restore_history(harness)
    _install_real_revision_effects(harness)
    monkeypatch.setattr(
        revision_module,
        "_ReadOnlyWorkspaceSnapshotUow",
        lambda *_args: pytest.fail("write path used the preview-only snapshot wrapper"),
    )

    result = await harness.service.restore_stale_enrichment_blocks(
        subject_id=harness.world.snapshot.subject_id,
        base_artifact_id=harness.base.id,
        base_version=harness.base.version,
        base_input_hash=harness.base.input_hash,
        base_canonical_sha256=harness.base_sha256,
        actor="dev-analyst",
    )

    records = harness.service._enrichment._uow_factory().editorial_workspace_snapshots.records
    assert len(records) >= 2
    base_record = records[0]
    advanced_record = next(
        record
        for record in records[1:]
        if record.parent_snapshot_id == base_record.snapshot_id
        and record.enrichment_artifact_id == result.artifact.id
    )
    assert advanced_record.parent_snapshot_id == base_record.snapshot_id
    assert advanced_record.enrichment_artifact_id == result.artifact.id
    assert advanced_record.enrichment_version == result.artifact.version
    assert advanced_record.registry_hash != base_record.registry_hash


@pytest.mark.asyncio
async def test_preview_then_targeted_revise_matches_revise_only_persistent_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, "unexpected")),
    )
    _install_real_revision_effects(harness)
    request = _common_enrichment_request(
        harness,
        element_kind=EditorialEnrichmentElementKind.TABLE,
        operation=TargetedEditorialRevisionOperation.DELETE_TABLE,
        element_key="tools_table",
    )
    initial_artifacts = copy.deepcopy(harness.artifacts.artifacts)
    initial_payloads = copy.deepcopy(harness.world.store.payloads)

    revise_only = await harness.service.revise_targeted(request)
    assert revise_only.artifact is not None
    revise_only_state = await _persistent_revision_state(harness)

    harness.artifacts.artifacts = copy.deepcopy(initial_artifacts)
    harness.world.store.payloads = copy.deepcopy(initial_payloads)
    snapshot_repository = harness.service._enrichment._uow_factory().editorial_workspace_snapshots
    snapshot_repository.records.clear()
    harness.assembled.clear()
    harness.rendered.clear()

    preview = await harness.service.preview_targeted(request)
    assert preview.expected_model_calls == 0
    assert snapshot_repository.records == []
    preview_then_revise = await harness.service.revise_targeted(request)
    assert preview_then_revise.artifact is not None

    assert await _persistent_revision_state(harness) == revise_only_state


@pytest.mark.asyncio
async def test_stale_enrichment_restore_uses_shared_effects_in_one_uow(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, "unexpected")),
        include_figure=True,
        include_target_annotations=True,
    )
    await _install_stale_enrichment_restore_history(harness)
    trace = _record_write_path(harness)
    arguments = {
        "subject_id": harness.world.snapshot.subject_id,
        "base_artifact_id": harness.base.id,
        "base_version": harness.base.version,
        "base_input_hash": harness.base.input_hash,
        "base_canonical_sha256": harness.base_sha256,
        "actor": "dev-analyst",
    }

    result = await harness.service.restore_stale_enrichment_blocks(**arguments)

    events = _write_events(trace)
    write_uow = next(uow for name, uow in events if name == "persist_candidate")
    events = [(name, uow) for name, uow in events if uow is write_uow]
    assert [name for name, _uow in events] == [
        "acquire_run_guard",
        "lock_run_row",
        "current_snapshot_in_uow",
        "persist_candidate",
        "apply_after_write",
        "commit",
    ]
    assert all(uow is write_uow for _name, uow in events)
    assert result.artifact.version == harness.base.version + 1
    assert result.revision["actor"] == "dev-analyst"
    assert result.revision["model_call_count"] == 0
    assert harness.world.gateway.calls == []


@pytest.mark.asyncio
async def test_v8_and_v9_readers_preserve_placement_contracts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    harness = await _revision_harness(
        monkeypatch,
        _RecordingGateway(lambda request: _succeeded_text(request, _table_revision_wire())),
    )
    current = editorial_enrichment_from_json(harness.base_payload)
    anchor_id = next(
        item.block_id
        for item in harness.world.workspace.snapshots.records[-1].block_registry
        if item.kind.value in {"lead_paragraph", "paragraph"}
    )
    legacy_placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_SECTION,
        section_index=0,
    )
    v8 = replace(
        current,
        schema_version=8,
        enrichment_policy_version=EDITORIAL_ENRICHMENT_V8_CURRENT_POLICY_VERSION,
        tables=tuple(replace(item, placement=legacy_placement) for item in current.tables),
        diagrams=tuple(replace(item, placement=legacy_placement) for item in current.diagrams),
        charts=tuple(replace(item, placement=legacy_placement) for item in current.charts),
        source_figures=tuple(
            replace(item, placement=legacy_placement) for item in current.source_figures
        ),
    )
    v9_placement = EnrichmentPlacementV1(
        EnrichmentPlacementKind.AFTER_PARAGRAPH,
        anchor_block_id=anchor_id,
    )
    v9 = replace(
        current,
        schema_version=9,
        enrichment_policy_version=EDITORIAL_ENRICHMENT_V9_POLICY_VERSION,
        tables=tuple(replace(item, placement=v9_placement) for item in current.tables),
    )
    v10 = replace(
        current,
        schema_version=10,
        enrichment_policy_version=EDITORIAL_ENRICHMENT_V10_POLICY_VERSION,
        tables=tuple(replace(item, placement=v9_placement) for item in current.tables),
    )

    for expected in (v8, v9, v10):
        payload = editorial_enrichment_to_json(expected)
        decoded = editorial_enrichment_from_json(payload)
        assert decoded == expected
        assert editorial_enrichment_to_json(decoded) == payload
    assert editorial_enrichment_to_json(v8)["tables"][0]["placement"] == {
        "kind": "after_section",
        "section_index": 0,
    }
    assert editorial_enrichment_to_json(v9)["tables"][0]["placement"] == {
        "kind": "after_paragraph",
        "section_index": None,
        "anchor_block_id": str(anchor_id),
    }
    assert editorial_enrichment_to_json(v10)["tables"][0]["placement"] == {
        "kind": "after_paragraph",
        "section_index": None,
        "anchor_block_id": str(anchor_id),
    }
