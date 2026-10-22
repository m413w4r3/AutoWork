from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import httpx
import pytest
from httpx import ASGITransport

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import ScriptedTurnTransport
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    InvestigationExecutionMode,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.workflow_emulator.investigation_support import (
    build_investigation_app,
    composition_factory,
    investigation_settings,
    seed_verified_article,
)

pytestmark = pytest.mark.integration


class _OperatorClientIdentityProvider:
    async def current(self) -> Identity:
        return Identity(
            actor_id="human:operator-context-integration",
            scopes=frozenset({IdentityScope.HUMAN_OPERATOR, IdentityScope.OPERATOR_CLIENT}),
        )


@pytest.mark.asyncio
async def test_operator_context_summary_and_pages_preserve_postgres_cas(
    uow_factory, tmp_path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "operator-context-summary"), uow_factory)
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=uuid4().hex[:10],
    )
    assert seeded.seed_observation is not None
    settings = investigation_settings(providers=False)
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=ScriptedTurnTransport([]),
    )
    app, _registry, _dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    app.state.identity_provider = _OperatorClientIdentityProvider()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await client.post(
            f"/api/subjects/{seeded.subject.id}/investigations",
            json={
                "production_run_id": str(seeded.run.id),
                "synthesis_artifact_id": str(seeded.synthesis.id),
                "synthesis_sha256": seeded.synthesis.input_hash,
                "execution_mode": InvestigationExecutionMode.OPERATOR_LED.value,
            },
        )
        assert started.status_code == 200, started.text
        investigation_id = seeded.investigation.id
        async with uow_factory() as uow:
            state = await uow.investigation_states.get(investigation_id)
        assert state is not None

        params = {
            "view": "summary",
            "expected_version": state.version,
            "expected_sha256": state.sha256,
        }
        summary = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params=params,
        )
        assert summary.status_code == 200, summary.text
        summary_payload = summary.json()
        unchanged = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params={
                **params,
                "since_version": state.version,
                "since_fingerprint": summary_payload["fingerprint"],
            },
        )
        missing_fingerprint = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params={**params, "since_version": state.version},
        )

        observation_bytes = b"operator context delta invalidation observation"
        observation_blob_id = await artifact_store.put_bytes(
            observation_bytes,
            bucket="operator-context-summary-observations",
            mime_type="application/octet-stream",
        )
        added_observation = EvidenceObservation(
            investigation_id=investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=observation_blob_id,
            sha256=hashlib.sha256(observation_bytes).hexdigest(),
            source="operator-context-delta-test",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="test_observation",
            object_id="added-after-summary",
        )
        async with uow_factory() as uow:
            await uow.evidence_observations.append_if_absent(added_observation)
            await uow.commit()
        after_observation = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params={
                **params,
                "since_version": state.version,
                "since_fingerprint": summary_payload["fingerprint"],
            },
        )
        evidence_page = await client.get(
            f"/api/investigations/{investigation_id}/operator/context/section/evidence_index",
            params={
                "offset": 0,
                "limit": 20,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        oversized_page = await client.get(
            f"/api/investigations/{investigation_id}/operator/context/section/evidence_index",
            params={
                "offset": 0,
                "limit": 21,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )

    assert summary.status_code == 200, summary.text
    summary_payload = summary.json()
    assert summary_payload["schema"] == "investigation-operator-context-summary.v1"
    assert summary_payload["working_state_version"] == state.version
    assert summary_payload["working_state_sha256"] == state.sha256
    assert summary_payload["source"]["synthesis_sha256"] == seeded.synthesis.input_hash
    assert "context" not in summary_payload
    assert len(summary_payload["pointers"]["section_cursors"]) == 29
    assert len(summary_payload["pointers"]["endpoints"]) == 3
    assert summary_payload["pointers"]["section_limit"] == 20
    assert unchanged.status_code == 200, unchanged.text
    unchanged_delta = unchanged.json()["delta_since_version"]
    assert unchanged_delta["strategy"] == "section_hashes"
    assert unchanged_delta["pointers_to_check"] == []
    assert missing_fingerprint.status_code == 200, missing_fingerprint.text
    missing_delta = missing_fingerprint.json()["delta_since_version"]
    assert missing_delta["strategy"] == "all_sections_conservative"
    assert len(missing_delta["pointers_to_check"]) == 32
    assert after_observation.status_code == 200, after_observation.text
    observation_delta = after_observation.json()["delta_since_version"]
    assert observation_delta["strategy"] == "all_sections_conservative"
    assert len(observation_delta["pointers_to_check"]) == 32
    assert evidence_page.status_code == 200, evidence_page.text
    assert evidence_page.json()["schema"] == "investigation-operator-context-section.v1"
    assert evidence_page.json()["total"] == 2
    assert str(seeded.seed_observation.id) in {
        item["evidence_id"] for item in evidence_page.json()["items"]
    }
    assert oversized_page.status_code == 422
