"""Rebuild the disposable release workspace from canonical records."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
)
from cti_app.application.typst_render_output import (
    TypstRenderOutputIntegrityError,
    read_verified_render_pdf,
)
from cti_app.domain.edition_publication import PublicationManifestV1
from cti_app.domain.typst_render import TypstRenderStatus


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
        pdf_content: bytes,
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

    async def materialize(
        self,
        edition_id: UUID | None = None,
        *,
        manifest_id: UUID | None = None,
        edition_render_id: UUID | None = None,
        edition_release_id: UUID | None = None,
    ) -> Path:
        if self._workspace_materializer is None:
            raise EditionReleaseMaterializationError("workspace_materializer_unavailable")
        if (edition_id is None) == (edition_release_id is None):
            raise ValueError("Specify exactly one of edition_id or edition_release_id")

        async with self._uow_factory() as uow:
            if edition_release_id is not None:
                release = await uow.edition_releases.get(edition_release_id)
                if release is None:
                    raise EditionReleaseMaterializationError("edition_release_not_found")
                edition = await uow.editions.get(release.edition_id)
                manifest = await uow.publication_manifests.get(release.manifest_id)
                if edition is None:
                    raise EditionReleaseMaterializationError("edition_not_found")
                if manifest is None or manifest.edition_id != release.edition_id:
                    raise EditionReleaseMaterializationError("manifest_not_found")
            else:
                assert edition_id is not None
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

            render = (
                await uow.edition_renders.get(edition_render_id)
                if edition_render_id is not None
                else await uow.edition_renders.get_latest_succeeded_for_release(release.id)
            )
            if (
                render is None
                or render.edition_release_id != release.id
                or render.status is not TypstRenderStatus.SUCCEEDED
            ):
                raise EditionReleaseMaterializationError("edition_render_not_available")
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

        edition_bytes = await self._artifact_store.read_bytes(
            release.edition_document_blob_id,
            max_bytes=MAX_ARTIFACT_BYTES,
        )
        self._verify_payload_hash(
            edition_bytes,
            release.edition_document_sha256,
            error_code="edition_document_blob_mismatch",
        )
        try:
            edition_payload = json.loads(edition_bytes)
        except (TypeError, ValueError, UnicodeDecodeError) as exc:
            raise EditionReleaseMaterializationError("edition_document_blob_invalid") from exc
        if not isinstance(edition_payload, dict):
            raise EditionReleaseMaterializationError("edition_document_blob_invalid")

        try:
            pdf_content = await read_verified_render_pdf(self._artifact_store, render)
        except TypstRenderOutputIntegrityError as exc:
            raise EditionReleaseMaterializationError(
                "edition_render_output_integrity_mismatch"
            ) from exc

        return await self._workspace_materializer.materialize_release(
            period=period,
            country_code=country_code,
            edition_id=release.edition_id,
            manifest=manifest_payload,
            edition=edition_payload,
            pdf_content=pdf_content,
        )

    @staticmethod
    def _verify_payload_hash(
        payload: bytes,
        expected: str,
        *,
        error_code: str,
    ) -> None:
        if hashlib.sha256(payload).hexdigest() != expected:
            raise EditionReleaseMaterializationError(error_code)


__all__ = [
    "EditionReleaseMaterializationError",
    "EditionReleaseRematerializationService",
]
