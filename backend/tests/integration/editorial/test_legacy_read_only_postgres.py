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
            assert body["read_only_reason"] in {
                "format_not_editable",
                "publication_kind_ambiguous",
            }
            assert body["canonical_content"] == legacy_documents[artifact.version - 1]

        snapshot_response = await client.get(f"/api/subjects/{seeded.subject.id}/editor/snapshot")
        assert snapshot_response.status_code == 200, snapshot_response.text
        snapshot = snapshot_response.json()
        assert snapshot["publication_version"] == 5
        assert snapshot["capabilities"]["read_only_reason"] in {
            "format_not_editable",
            "publication_kind_ambiguous",
        }
        assert all(
            not value
            for key, value in snapshot["capabilities"].items()
            if key != "read_only_reason"
        )
        assert snapshot["blocks"]
        assert all(block["allowed_operations"] == [] for block in snapshot["blocks"])

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
