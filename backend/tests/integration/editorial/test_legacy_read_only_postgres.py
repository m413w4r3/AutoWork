from __future__ import annotations

import hashlib
from typing import cast
from uuid import UUID

import httpx
import pytest

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.api import main as api_main
from cti_app.api.main import create_app
from cti_app.application.blob_storage import BlobStore
from cti_app.application.blobs import BlobCatalogService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.config import Settings
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from tests.integration.editorial.test_proposal_adoption_postgres import _seed
from tests.test_editorial_legacy_read_only import _legacy_documents

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_legacy_read_only_postgres_keeps_artifact_bytes_and_versions(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(_env_file=None, app_env="development", api_bind_host="127.0.0.1"),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            editorial_proposal_accept_enabled=True,
            production_editorial_resource_search_enabled=False,
        ),
    )
    seeded = await _seed(uow_factory, proposal_count=0, seed_discovery_lineage=True)
    blob_store = cast(BlobStore, seeded.blob_store)
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))
    legacy_documents = _legacy_documents()
    publication_artifacts: list[ProductionArtifact] = []
    seeded_shas: dict[UUID, str] = {}

    for version, document in enumerate(legacy_documents, start=1):
        blob_id, canonical_sha256 = await artifact_store.put_canonical_json(
            document, bucket="production-artifacts-canonical"
        )
        artifact = ProductionArtifact(
            production_run_id=seeded.run.id,
            subject_id=seeded.subject.id,
            stage=ProductionArtifactStage.PUBLICATION,
            version=version,
            status=ProductionArtifactStatus.VERIFIED,
            input_hash=hashlib.sha256(f"legacy-publication-v{version}".encode()).hexdigest(),
            canonical_blob_id=blob_id,
        )
        publication_artifacts.append(artifact)
        seeded_shas[artifact.id] = canonical_sha256

    # Keep a V8 artifact in history while the subject's current publication is
    # also V8, matching a pre-V9 production run that remains editable in M22.
    current_v8_blob_id, current_v8_sha256 = await artifact_store.put_canonical_json(
        legacy_documents[-1], bucket="production-artifacts-canonical"
    )
    current_v8 = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=len(publication_artifacts) + 1,
        status=ProductionArtifactStatus.VERIFIED,
        input_hash=hashlib.sha256(b"current-v8-publication").hexdigest(),
        canonical_blob_id=current_v8_blob_id,
    )
    publication_artifacts.append(current_v8)
    seeded_shas[current_v8.id] = current_v8_sha256

    async with uow_factory() as uow:
        for artifact in publication_artifacts:
            await uow.production_artifacts.append(artifact)
        await uow.commit()

    app = create_app()
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = artifact_store
    app.state.identity_provider = LocalIdentityProvider("dev-analyst")

    before_versions = [(item.id, item.version) for item in publication_artifacts]
    before_bytes = {
        item.id: await artifact_store.read_bytes(item.canonical_blob_id)
        for item in publication_artifacts
        if item.canonical_blob_id is not None
    }
    before_shas = {
        item_id: hashlib.sha256(value).hexdigest() for item_id, value in before_bytes.items()
    }
    assert before_shas == seeded_shas

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        for artifact in publication_artifacts:
            response = await client.get(
                f"/api/subjects/{seeded.subject.id}/production/artifacts/publication/{artifact.id}"
            )
            assert response.status_code == 200, response.text
            body = response.json()
            assert body["version"] == artifact.version
            assert body["canonical_sha256"] == before_shas[artifact.id]
            if artifact.id == current_v8.id:
                assert body["read_only_reason"] is None
            else:
                assert body["read_only_reason"] in {
                    "historical_version",
                    "publication_kind_ambiguous",
                    "publication_kind_unknown",
                }
                assert "capabilities" not in body
                assert "allowed_operations" not in body
            expected_document = (
                legacy_documents[-1]
                if artifact.id == current_v8.id
                else legacy_documents[artifact.version - 1]
            )
            assert body["canonical_content"] == expected_document

        snapshot_response = await client.get(f"/api/subjects/{seeded.subject.id}/editor/snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        assert snapshot["publication_version"] == current_v8.version
        assert snapshot["capabilities"]["read_only_reason"] is None
        assert snapshot["blocks"]
        assert any(
            "rewrite_paragraph" in block["allowed_operations"] for block in snapshot["blocks"]
        )

    async with uow_factory() as uow:
        current_publications = [
            item
            for item in await uow.production_artifacts.list_for_run(seeded.run.id)
            if item.stage is ProductionArtifactStage.PUBLICATION
        ]
    after_versions = sorted((item.id, item.version) for item in current_publications)
    assert after_versions == sorted(before_versions)
    after_bytes = {
        item.id: await artifact_store.read_bytes(item.canonical_blob_id)
        for item in current_publications
        if item.canonical_blob_id is not None
    }
    assert {
        item_id: hashlib.sha256(value).hexdigest() for item_id, value in after_bytes.items()
    } == before_shas
    assert after_bytes == before_bytes


@pytest.mark.asyncio
async def test_forged_write_on_read_only_context_writes_no_proposal_or_revision(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.domain.production_synthesis import production_synthesis_to_json

    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(_env_file=None, app_env="development", api_bind_host="127.0.0.1"),
    )
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            editorial_proposal_accept_enabled=True,
            production_editorial_resource_search_enabled=False,
        ),
    )
    monkeypatch.setattr(editorial_workspace_api, "_proposal_impact_consumer_wired", lambda: True)
    seeded = await _seed(
        uow_factory,
        proposal_count=0,
        seed_discovery_lineage=True,
        persist_real_revision_inputs=True,
    )
    blob_store = cast(BlobStore, seeded.blob_store)
    artifact_store = ProductionArtifactStore(BlobCatalogService(blob_store, uow_factory))

    # The current publication resolves the kind, while the pinned synthesis format is
    # unsupported: the snapshot is read-only, so any write must be refused server-side.
    publication_blob_id, _publication_sha = await artifact_store.put_canonical_json(
        _legacy_documents()[-1], bucket="production-artifacts-canonical"
    )
    publication = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=1,
        status=ProductionArtifactStatus.VERIFIED,
        input_hash=hashlib.sha256(b"read-only-current-publication").hexdigest(),
        canonical_blob_id=publication_blob_id,
    )
    unsupported_payload = production_synthesis_to_json(seeded.synthesis)
    unsupported_payload["schema_version"] = "1"
    synthesis_blob_id, _synthesis_sha = await artifact_store.put_canonical_json(
        unsupported_payload, bucket="production-artifacts-canonical"
    )
    unsupported_synthesis = ProductionArtifact(
        production_run_id=seeded.run.id,
        subject_id=seeded.subject.id,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=seeded.base_artifact.version + 1,
        status=ProductionArtifactStatus.VERIFIED,
        input_hash=seeded.base_artifact.input_hash,
        canonical_blob_id=synthesis_blob_id,
    )
    async with uow_factory() as uow:
        await uow.production_artifacts.append(publication)
        await uow.production_artifacts.append(unsupported_synthesis)
        await uow.commit()

    app = create_app()
    app.state.uow_factory = uow_factory
    app.state.blob_store = blob_store
    app.state.production_artifact_store = artifact_store
    app.state.identity_provider = LocalIdentityProvider("dev-analyst")

    async with uow_factory() as uow:
        proposals_before = await uow.editorial_proposals.list_for_subject(
            seeded.subject.id, occurrence_id=None, before=None, limit=100
        )
        artifacts_before = sorted(
            (item.id, item.version)
            for item in await uow.production_artifacts.list_for_run(seeded.run.id)
        )

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        snapshot_response = await client.get(f"/api/subjects/{seeded.subject.id}/editor/snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        assert snapshot["capabilities"]["read_only_reason"] == "format_not_editable"
        title = next(block for block in snapshot["blocks"] if block["kind"] == "title")
        response = await client.post(
            f"/api/subjects/{seeded.subject.id}/editor/blocks/{title['block_id']}/manual-edit",
            headers={"Idempotency-Key": "forged-read-only-write-0001"},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "operation": "rewrite_title",
                "text": "Synthetic forged replacement title",
            },
        )
        assert response.status_code == 409, response.text
        assert response.json()["detail"] == {
            "code": "editorial_read_only",
            "message": "Cette version est en lecture seule : elle ne peut pas être modifiée.",
        }

    async with uow_factory() as uow:
        proposals_after = await uow.editorial_proposals.list_for_subject(
            seeded.subject.id, occurrence_id=None, before=None, limit=100
        )
        artifacts_after = sorted(
            (item.id, item.version)
            for item in await uow.production_artifacts.list_for_run(seeded.run.id)
        )
    assert [item.id for item in proposals_after] == [item.id for item in proposals_before]
    assert artifacts_after == artifacts_before
