"""Rebuild the disposable release workspace from canonical records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.edition_publication import PublicationManifestV1

MAX_RELEASE_DOCX_BYTES = 32 * 1024 * 1024


class EditionReleaseMaterializationError(ValueError):
    """The canonical release is unavailable or internally inconsistent."""


class ReleaseWorkspaceMaterializer(Protocol):
    async def materialize_release(
        self,
        *,
        period: Any,
        country_code: str,
        edition_id: UUID,
        manifest: Mapping[str, Any],
        edition: Mapping[str, Any],
        markdown: str,
        docx: bytes,
    ) -> Path: ...


class EditionReleaseRematerializationService:
    """Project one existing canonical release into the local workspace."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        workspace_materializer: ReleaseWorkspaceMaterializer | None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._workspace_materializer = workspace_materializer

    async def materialize(self, edition_id: UUID, *, manifest_id: UUID | None = None) -> Path:
        if self._workspace_materializer is None:
            raise EditionReleaseMaterializationError("workspace_materializer_unavailable")

        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionReleaseMaterializationError("edition_not_found")
            manifest = (
                await uow.publication_manifests.get(manifest_id)
                if manifest_id is not None
                else await uow.publication_manifests.get_latest_for_edition(edition_id)
            )
            if manifest is None or manifest.edition_id != edition_id:
                raise EditionReleaseMaterializationError("manifest_not_found")
            release = await uow.edition_releases.get_by_manifest(manifest.id)
            if release is None:
                raise EditionReleaseMaterializationError("edition_release_not_found")
            manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest.id)
            if manifest_blob_id is None:
                raise EditionReleaseMaterializationError("manifest_blob_missing")

            period = edition.period_start
            country_code = edition.country_code

        manifest_payload = await self._artifact_store.read_json(manifest_blob_id)
        try:
            blob_manifest = PublicationManifestV1.from_json(manifest_payload)
        except (KeyError, TypeError, ValueError) as exc:
            raise EditionReleaseMaterializationError("manifest_blob_invalid") from exc
        if blob_manifest != manifest:
            raise EditionReleaseMaterializationError("manifest_blob_mismatch")

        edition_payload = await self._artifact_store.read_json(release.edition_document_blob_id)
        markdown = await self._artifact_store.read_text(release.markdown_blob_id)
        docx = await self._artifact_store.read_bytes(
            release.docx_blob_id, max_bytes=MAX_RELEASE_DOCX_BYTES
        )
        self._verify_payload_hash(
            edition_payload,
            release.edition_document_sha256,
            json_payload=True,
            error_code="edition_document_blob_mismatch",
        )
        self._verify_payload_hash(
            markdown.encode("utf-8"),
            release.markdown_sha256,
            error_code="edition_markdown_blob_mismatch",
        )
        self._verify_payload_hash(
            docx,
            release.docx_sha256,
            error_code="edition_docx_blob_mismatch",
        )

        return await self._workspace_materializer.materialize_release(
            period=period,
            country_code=country_code,
            edition_id=edition_id,
            manifest=manifest_payload,
            edition=edition_payload,
            markdown=markdown,
            docx=docx,
        )

    @staticmethod
    def _verify_payload_hash(
        payload: Mapping[str, Any] | bytes,
        expected: str,
        *,
        error_code: str,
        json_payload: bool = False,
    ) -> None:
        if json_payload:
            if isinstance(payload, bytes):
                raise TypeError("JSON payload must be a mapping")
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        else:
            if not isinstance(payload, bytes):
                raise TypeError("Binary payload must be bytes")
            encoded = payload
        if hashlib.sha256(encoded).hexdigest() != expected:
            raise EditionReleaseMaterializationError(error_code)


__all__ = [
    "MAX_RELEASE_DOCX_BYTES",
    "EditionReleaseMaterializationError",
    "EditionReleaseRematerializationService",
]
