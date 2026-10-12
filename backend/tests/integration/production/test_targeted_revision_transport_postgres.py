"""PostgreSQL-backed transport refusal contracts for targeted revisions."""

from __future__ import annotations

import hashlib
from collections.abc import Callable, Mapping
from types import SimpleNamespace
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import create_async_engine

from cti_app.api.production import router as production_router
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.model_gateway import SafeModelRequest
from cti_app.domain.model_runs import ModelSubmissionState
from cti_app.domain.production import ProductionArtifactStage, ProductionRunStatus
from cti_app.domain.production_editorial_enrichment import EditorialEnrichmentRevisionAction
from cti_app.infrastructure.database.models.model_execution import ModelRunRow
from cti_app.integrations.models import BridgeTransportError

from .support import ProductionScenario, grounded_editorial_proposal
from .test_pipeline_happy_path import _configured_editorial_scenario, _install_canonical_synthesis

pytestmark = [pytest.mark.integration, pytest.mark.asyncio]

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]


async def _model_run_count(database_url: str) -> int:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            return int(await connection.scalar(select(func.count()).select_from(ModelRunRow)) or 0)
    finally:
        await engine.dispose()


def _application(scenario: ProductionScenario) -> FastAPI:
    app = FastAPI()
    app.include_router(production_router)
    app.state.uow_factory = scenario.uow_factory
    app.state.production_artifact_store = scenario.artifact_store
    app.state.model_gateway = scenario.model
    app.state.collection_service = None
    app.state.publication_render_service = SimpleNamespace(render_preview=_unused_render_preview)
    app.state.identity_provider = LocalIdentityProvider("revision-transport-test")
    return app


async def _unused_render_preview(artifact_id: UUID) -> None:
    raise AssertionError(f"transport refusal must not render artifact {artifact_id}")


@pytest.mark.parametrize("revision_kind", ("synthesis", "enrichment"))
async def test_revision_transport_errors_follow_persisted_model_run_state(
    revision_kind: str,
    production_scenario_factory: ScenarioFactory,
    migrated_postgres_url: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scenario = await _configured_editorial_scenario(
        production_scenario_factory, capture_tag=f"revision-transport-{revision_kind}"
    )
    _install_canonical_synthesis(scenario)
    scenario.model.script.editorial_enrichment(grounded_editorial_proposal)
    await scenario.start()
    production_run = await scenario.run_until_terminal()
    assert production_run.status is ProductionRunStatus.READY, (
        production_run.error_code,
        production_run.error_message,
        production_run.error_details,
    )

    async with scenario.uow_factory() as uow:
        synthesis_artifact = await uow.production_artifacts.get_current(
            production_run.id, ProductionArtifactStage.SYNTHESIS.value
        )
        enrichment_artifact = await uow.production_artifacts.get_current(
            production_run.id, ProductionArtifactStage.EDITORIAL_ENRICHMENT.value
        )
    assert synthesis_artifact is not None and synthesis_artifact.canonical_blob_id is not None
    assert enrichment_artifact is not None and enrichment_artifact.canonical_blob_id is not None
    synthesis_bytes = await scenario.artifact_store.read_bytes(synthesis_artifact.canonical_blob_id)
    enrichment_bytes = await scenario.artifact_store.read_bytes(
        enrichment_artifact.canonical_blob_id
    )
    enrichment_payload = await scenario.artifact_store.read_json(
        enrichment_artifact.canonical_blob_id
    )
    table_key = enrichment_payload["tables"][0]["key"]

    scenario.model.use_chatgpt_bridge_identity()
    next_submission_state = ["pre_submission"]
    scripted_output: list[str | None] = [None]
    response_for = scenario.model.script.response_for

    def fail_targeted_request(request: SafeModelRequest) -> str | Exception:
        prompt_template_id = request.prompt_template_id
        expected_prefix = (
            "production-synthesis-targeted-revision"
            if revision_kind == "synthesis"
            else "production-editorial-enrichment"
        )
        if isinstance(prompt_template_id, str) and prompt_template_id.startswith(expected_prefix):
            if scripted_output[0] is not None:
                return scripted_output[0]
            state = next_submission_state[0]
            raise BridgeTransportError(
                "bridge_transport_test_failure",
                "Scripted Bridge transport failure for the integration test.",
                retryable=state == "pre_submission",
                submission_state=state,
            )
        return response_for(request)

    monkeypatch.setattr(scenario.model.script, "response_for", fail_targeted_request)
    app = _application(scenario)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        for submission_state, expected_status, expected_code, expected_model_state in (
            (
                "pre_submission",
                503,
                "targeted_revision_transport_not_submitted",
                ModelSubmissionState.NOT_SUBMITTED,
            ),
            (
                "post_submission",
                409,
                "targeted_revision_reconciliation_required",
                ModelSubmissionState.EXTERNAL_STATE_UNKNOWN,
            ),
        ):
            next_submission_state[0] = submission_state
            instruction_suffix = submission_state
            if revision_kind == "synthesis":
                path = (
                    f"/api/subjects/{synthesis_artifact.subject_id}/production/artifacts/"
                    "synthesis/revisions"
                )
                payload = {
                    "base_artifact_id": str(synthesis_artifact.id),
                    "base_version": synthesis_artifact.version,
                    "base_input_hash": synthesis_artifact.input_hash,
                    "base_canonical_sha256": hashlib.sha256(synthesis_bytes).hexdigest(),
                    "target": {"kind": "title"},
                    "operation": "rewrite_title",
                    "instruction": f"Revise the title ({instruction_suffix}).",
                    "mode": "existing_evidence_only",
                }
            else:
                path = (
                    f"/api/subjects/{enrichment_artifact.subject_id}/production/artifacts/"
                    "editorial_enrichment/revisions"
                )
                payload = {
                    "base_artifact_id": str(enrichment_artifact.id),
                    "base_version": enrichment_artifact.version,
                    "base_input_hash": enrichment_artifact.input_hash,
                    "base_canonical_sha256": hashlib.sha256(enrichment_bytes).hexdigest(),
                    "element_kind": "table",
                    "element_key": table_key,
                    "action": EditorialEnrichmentRevisionAction.IMPROVE_TABLE.value,
                    "instruction": f"Improve the table ({instruction_suffix}).",
                }

            response = await client.post(path, json=payload)
            assert response.status_code == expected_status, response.text
            detail = response.json()["detail"]
            assert detail["code"] == expected_code
            assert detail["retryable"] is (submission_state == "pre_submission")
            assert detail["request_identity"]
            model_run_id = UUID(detail["model_run_id"])
            async with scenario.uow_factory() as uow:
                model_run = await uow.model_runs.get(model_run_id)
            assert model_run is not None
            assert model_run.submission_state is expected_model_state
            assert detail["submission_state"] == expected_model_state.value

            if submission_state == "pre_submission":
                rows_before_safe_retry = await _model_run_count(migrated_postgres_url)
                calls_before_safe_retry = len(scenario.model.provider_calls)
                next_submission_state[0] = "post_submission"
                safe_retry = await client.post(path, json=payload)
                assert safe_retry.status_code == 409, safe_retry.text
                retry_detail = safe_retry.json()["detail"]
                assert retry_detail["code"] == ("targeted_revision_reconciliation_required")
                assert retry_detail["request_identity"] == detail["request_identity"]
                assert retry_detail["model_run_id"] == detail["model_run_id"]
                assert retry_detail["retryable"] is False
                assert len(scenario.model.provider_calls) == calls_before_safe_retry + 1
                assert await _model_run_count(migrated_postgres_url) == rows_before_safe_retry
                async with scenario.uow_factory() as uow:
                    retried_run = await uow.model_runs.get(model_run_id)
                assert (
                    retried_run is not None
                    and retried_run.submission_state is ModelSubmissionState.EXTERNAL_STATE_UNKNOWN
                )

            if submission_state == "post_submission":
                calls_before_replay = len(scenario.model.provider_calls)
                rows_before_replay = await _model_run_count(migrated_postgres_url)
                replay = await client.post(path, json=payload)
                assert replay.status_code == 409
                replay_detail = replay.json()["detail"]
                assert replay_detail["code"] == expected_code
                assert replay_detail["request_identity"] == detail["request_identity"]
                assert replay_detail["model_run_id"] == detail["model_run_id"]
                assert len(scenario.model.provider_calls) == calls_before_replay
                assert await _model_run_count(migrated_postgres_url) == rows_before_replay
                async with scenario.uow_factory() as uow:
                    replayed_run = await uow.model_runs.get(model_run_id)
                assert replayed_run is not None and replayed_run.id == model_run.id

        if revision_kind == "synthesis":
            scripted_output[0] = "unstructured model output"
            invalid_output_payload = {
                "base_artifact_id": str(synthesis_artifact.id),
                "base_version": synthesis_artifact.version,
                "base_input_hash": synthesis_artifact.input_hash,
                "base_canonical_sha256": hashlib.sha256(synthesis_bytes).hexdigest(),
                "target": {"kind": "title"},
                "operation": "rewrite_title",
                "instruction": "Rewrite the title after a malformed response.",
                "mode": "existing_evidence_only",
            }
            invalid_output = await client.post(
                f"/api/subjects/{synthesis_artifact.subject_id}/production/artifacts/"
                "synthesis/revisions",
                json=invalid_output_payload,
            )
            assert invalid_output.status_code == 200, invalid_output.text
            refusal = invalid_output.json()["refusal"]
            assert refusal["recommended_restart_stage"] is None
            assert refusal["refusal_reason"] == "model_output_invalid"
            assert refusal["next_safe_action"]
