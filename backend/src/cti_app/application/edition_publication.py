"""Freeze an edition review and assemble its immutable publication release."""

from __future__ import annotations

import hashlib
import logging
import tempfile
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import ConfigDict

from cti_app.application.edition_review import EditionReviewService
from cti_app.application.jobs import (
    DuplicateJobError,
    JobDispatcher,
    JobExecutionContext,
    JobParameters,
    JobRegistry,
    JobService,
)
from cti_app.application.pandoc_export import export_markdown_docx
from cti_app.application.pandoc_rendering import render_edition_pandoc
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.edition_publication import (
    EditionDocumentV1,
    EditionPublicationV1,
    EditionRelease,
    PublicationManifestEntryV1,
    PublicationManifestExclusionV1,
    PublicationManifestV1,
)
from cti_app.domain.editions import Edition, EditionAuditEvent, EditionStatus
from cti_app.domain.jobs import Job
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus

logger = logging.getLogger(__name__)

EDITION_ASSEMBLE_JOB_KIND = "publication.edition.assemble"
MANIFEST_BLOB_BUCKET = "publication-manifests"
EDITION_DOCUMENT_BLOB_BUCKET = "edition-documents"
EDITION_MARKDOWN_BLOB_BUCKET = "edition-markdown"
EDITION_DOCX_BLOB_BUCKET = "edition-docx"
MAX_DOCX_BYTES = 32 * 1024 * 1024


class PublicationError(ValueError):
    pass


class PublicationAcceptanceError(PublicationError):
    pass


class PublicationAssemblyError(PublicationError):
    pass


class PublicationManifestNotFoundError(PublicationError):
    pass


@dataclass(frozen=True, slots=True)
class PublicationAcceptResult:
    manifest: PublicationManifestV1
    job_id: UUID | None
    job_dispatched: bool
    edition_status: EditionStatus

    @property
    def manifest_id(self) -> UUID:
        return self.manifest.id


@dataclass(frozen=True, slots=True)
class EditionReleaseStatus:
    edition_id: UUID
    edition_status: EditionStatus
    manifest_id: UUID | None
    manifest_sha256: str | None
    release: EditionRelease | None

    @property
    def json_available(self) -> bool:
        return self.release is not None

    @property
    def markdown_available(self) -> bool:
        return self.release is not None

    @property
    def docx_available(self) -> bool:
        return self.release is not None

    @property
    def published_at(self) -> datetime | None:
        return self.release.created_at if self.release is not None else None


class _WorkspaceReleaseMaterializer(Protocol):
    async def materialize_release(
        self,
        *,
        period: Any,
        country_code: str,
        edition_id: UUID,
        manifest: dict[str, Any],
        edition: dict[str, Any],
        markdown: str,
        docx: bytes,
    ) -> Any: ...


class EditionPublicationService:
    """Transaction boundary for review acceptance and recoverable dispatch."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        *,
        job_service: JobService | None = None,
        job_dispatcher: JobDispatcher | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._job_service = job_service
        self._job_dispatcher = job_dispatcher

    async def accept(
        self,
        edition_id: UUID,
        *,
        actor_id: str,
        correlation_id: str = "-",
    ) -> PublicationAcceptResult:
        """Freeze once, then re-assure the same assembly job on every retry."""
        async with self._uow_factory() as uow:
            edition = await _get_edition_for_update(uow, edition_id)
            if edition is None:
                raise PublicationAcceptanceError("edition_not_found")

            if edition.status is EditionStatus.PUBLISHED:
                manifest = await uow.publication_manifests.get_latest_for_edition(edition_id)
                if manifest is None:
                    raise PublicationAcceptanceError("published_edition_has_no_manifest")
                result = PublicationAcceptResult(manifest, None, False, EditionStatus.PUBLISHED)
            elif edition.status is EditionStatus.ASSEMBLING:
                manifest = await uow.publication_manifests.get_latest_for_edition(edition_id)
                if manifest is None:
                    raise PublicationAcceptanceError("assembling_edition_has_no_manifest")
                result = PublicationAcceptResult(manifest, None, False, EditionStatus.ASSEMBLING)
            elif edition.status is EditionStatus.REVIEW:
                result = await self._freeze(uow, edition, actor_id, correlation_id)
            else:
                raise PublicationAcceptanceError("edition_must_be_in_review_or_assembling")

        if result.manifest is not None and edition.status is not EditionStatus.PUBLISHED:
            job_id, dispatched = await self._ensure_assembly_job(
                result.manifest, correlation_id=correlation_id, actor_id=actor_id
            )
            return PublicationAcceptResult(
                result.manifest, job_id, dispatched, result.edition_status
            )
        return result

    async def _freeze(
        self, uow: Any, edition: Edition, actor_id: str, correlation_id: str
    ) -> PublicationAcceptResult:
        batch = await uow.edition_production_batches.get_latest_for_edition(edition.id)
        if batch is None:
            raise PublicationAcceptanceError("edition_has_no_production_batch")
        rows = await uow.edition_review_read_model.list_for_edition(edition.id)
        review = EditionReviewService.from_rows(edition.id, rows)
        if not review.can_accept:
            raise PublicationAcceptanceError("review_cannot_be_accepted")

        entries: list[PublicationManifestEntryV1] = []
        exclusions: list[PublicationManifestExclusionV1] = []
        for item in review.items:
            if item.included:
                if item.document_artifact_id is None:
                    raise PublicationAcceptanceError("included_item_has_no_document")
                run = await _get_run_for_update(uow, item.run_id)
                artifact = await uow.production_artifacts.get(item.document_artifact_id)
                if (
                    run is None
                    or artifact is None
                    or run.edition_id != edition.id
                    or run.subject_id != item.subject_id
                    or run.pipeline_generation != item.pipeline_generation
                    or artifact.production_run_id != item.run_id
                    or artifact.subject_id != item.subject_id
                    or artifact.id != item.document_artifact_id
                    or artifact.version != item.document_artifact_version
                    or artifact.input_hash != item.document_input_hash
                    or artifact.stage is not ProductionArtifactStage.BRIEF
                    or artifact.status is not ProductionArtifactStatus.VERIFIED
                    or artifact.canonical_blob_id is None
                ):
                    raise PublicationAcceptanceError("included_artifact_mismatch")
                entries.append(
                    PublicationManifestEntryV1(
                        position=item.position,
                        subject_id=item.subject_id,
                        production_run_id=item.run_id,
                        pipeline_generation=item.pipeline_generation,
                        document_artifact_id=artifact.id,
                        document_artifact_version=artifact.version,
                        document_input_hash=artifact.input_hash,
                    )
                )
            elif item.effective_decision is not None:
                if item.effective_decision_id is None:
                    raise PublicationAcceptanceError("excluded_item_has_no_decision")
                if item.effective_decision.value != "exclude":
                    raise PublicationAcceptanceError("non_publishable_item_is_not_excluded")
                exclusions.append(
                    PublicationManifestExclusionV1(
                        subject_id=item.subject_id,
                        review_decision_id=item.effective_decision_id,
                    )
                )
            else:
                raise PublicationAcceptanceError("review_item_has_no_effective_decision")

        if not entries:
            raise PublicationAcceptanceError("review_must_include_at_least_one_item")
        manifest = PublicationManifestV1.create(
            edition_id=edition.id,
            edition_version=edition.version,
            batch_id=batch.id,
            created_by=actor_id,
            entries=tuple(entries),
            exclusions=tuple(exclusions),
        )
        manifest_blob_id, _ = await self._artifact_store.put_canonical_json(
            manifest.to_json(), bucket=MANIFEST_BLOB_BUCKET
        )
        await uow.publication_manifests.add(manifest, manifest_blob_id)
        await uow.publication_manifest_entries.append_many(manifest.id, manifest.entries)
        await uow.publication_manifest_exclusions.append_many(manifest.id, manifest.exclusions)
        before = edition.snapshot()
        edition.transition(EditionStatus.ASSEMBLING)
        if not await uow.editions.update(edition, manifest.edition_version):
            raise PublicationAcceptanceError("edition_changed_during_freeze")
        await uow.edition_audit.append(
            EditionAuditEvent(
                edition_id=edition.id,
                actor_id=actor_id,
                action="edition.publication_manifest_created",
                before=before,
                after=edition.snapshot(),
                correlation_id=correlation_id,
            )
        )
        await uow.commit()
        return PublicationAcceptResult(manifest, None, False, EditionStatus.ASSEMBLING)

    async def _ensure_assembly_job(
        self,
        manifest: PublicationManifestV1,
        *,
        correlation_id: str,
        actor_id: str,
    ) -> tuple[UUID | None, bool]:
        if self._job_service is None or self._job_dispatcher is None:
            return None, False
        job: Job
        try:
            try:
                job = await self._job_service.submit(
                    kind=EDITION_ASSEMBLE_JOB_KIND,
                    aggregate_type="edition",
                    aggregate_id=manifest.edition_id,
                    idempotency_key=f"publication-assemble-{manifest.id}",
                    correlation_id=correlation_id,
                    input_parameters={"manifest_id": str(manifest.id)},
                    max_attempts=3,
                    actor_id=actor_id,
                )
            except DuplicateJobError as exc:
                job = await self._job_service.get(exc.existing_job_id)
        except Exception:
            logger.exception(
                "Unable to create publication assembly job for manifest %s", manifest.id
            )
            return None, False
        try:
            await self._job_dispatcher.dispatch(job.id)
        except Exception:
            # The committed freeze is the durable result.  A later accept call
            # will find this idempotent job and retry the dispatch.
            logger.exception("Unable to dispatch publication assembly job %s", job.id)
            return job.id, False
        return job.id, True

    async def release_status(self, edition_id: UUID) -> EditionReleaseStatus:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise PublicationManifestNotFoundError(str(edition_id))
            manifest = await uow.publication_manifests.get_latest_for_edition(edition_id)
            release = (
                await uow.edition_releases.get_by_manifest(manifest.id)
                if manifest is not None
                else None
            )
            return EditionReleaseStatus(
                edition_id=edition_id,
                edition_status=edition.status,
                manifest_id=manifest.id if manifest is not None else None,
                manifest_sha256=manifest.content_sha256 if manifest is not None else None,
                release=release,
            )

    async def read_docx(self, edition_id: UUID) -> tuple[EditionReleaseStatus, bytes]:
        status = await self.release_status(edition_id)
        if status.release is None:
            raise PublicationManifestNotFoundError("edition_release_not_available")
        return status, await self._artifact_store.read_bytes(
            status.release.docx_blob_id, max_bytes=MAX_DOCX_BYTES
        )


class EditionAssemblyService:
    """Assemble only the artifact IDs and hashes recorded by a manifest."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        *,
        workspace_materializer: _WorkspaceReleaseMaterializer | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._workspace_materializer = workspace_materializer

    async def assemble(
        self, manifest_id: UUID, *, context: JobExecutionContext | None = None
    ) -> EditionRelease:
        async with self._uow_factory() as uow:
            manifest = await uow.publication_manifests.get(manifest_id)
            if manifest is None:
                raise PublicationManifestNotFoundError(str(manifest_id))
            existing = await uow.edition_releases.get_by_manifest(manifest_id)
            if existing is not None:
                return await self._finish_existing(uow, existing, manifest)
            edition = await uow.editions.get(manifest.edition_id)
            if edition is None:
                raise PublicationAssemblyError("edition_not_found")
            manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest_id)
            if manifest_blob_id is None:
                raise PublicationAssemblyError("manifest_blob_missing")

        blob_payload = await self._artifact_store.read_json(manifest_blob_id)
        blob_manifest = PublicationManifestV1.from_json(blob_payload)
        if blob_manifest != manifest:
            raise PublicationAssemblyError("manifest_blob_mismatch")

        publications: list[EditionPublicationV1] = []
        for entry in manifest.entries:
            async with self._uow_factory() as uow:
                run = await uow.subject_production_runs.get(entry.production_run_id)
                artifact = await uow.production_artifacts.get(entry.document_artifact_id)
            if (
                run is None
                or artifact is None
                or run.edition_id != manifest.edition_id
                or run.subject_id != entry.subject_id
                or run.pipeline_generation != entry.pipeline_generation
                or artifact.production_run_id != entry.production_run_id
                or artifact.subject_id != entry.subject_id
                or artifact.id != entry.document_artifact_id
                or artifact.version != entry.document_artifact_version
                or artifact.input_hash != entry.document_input_hash
                or artifact.stage is not ProductionArtifactStage.BRIEF
                or artifact.status is not ProductionArtifactStatus.VERIFIED
                or artifact.canonical_blob_id is None
            ):
                raise PublicationAssemblyError("manifest_artifact_mismatch")
            payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            try:
                from cti_app.domain.publication import BriefDocumentV1

                publication = BriefDocumentV1.from_json(payload)
            except (KeyError, TypeError, ValueError) as exc:
                raise PublicationAssemblyError("publication_document_invalid") from exc
            publications.append(
                EditionPublicationV1(
                    position=entry.position, subject_id=entry.subject_id, document=publication
                )
            )

        async with self._uow_factory() as uow:
            edition = await uow.editions.get_for_update(manifest.edition_id)
            if edition is None:
                raise PublicationAssemblyError("edition_not_found")
            if edition.status not in {EditionStatus.ASSEMBLING, EditionStatus.PUBLISHED}:
                raise PublicationAssemblyError("edition_must_be_assembling")
            existing = await uow.edition_releases.get_by_manifest(manifest_id)
            if existing is not None:
                release = await self._finish_existing(uow, existing, manifest)
                workspace_payload = None
            else:
                edition_document = EditionDocumentV1(
                    edition=edition.snapshot(), publications=tuple(publications)
                )
                markdown = render_edition_pandoc(edition_document)
                edition_json = edition_document.to_json()
                edition_blob_id, edition_hash = await self._artifact_store.put_canonical_json(
                    edition_json, bucket=EDITION_DOCUMENT_BLOB_BUCKET
                )
                markdown_bytes = markdown.encode("utf-8")
                markdown_hash = hashlib.sha256(markdown_bytes).hexdigest()
                markdown_blob_id = await self._artifact_store.put_text(
                    markdown, bucket=EDITION_MARKDOWN_BLOB_BUCKET
                )
                with tempfile.TemporaryDirectory(prefix="autowork-edition-docx-") as directory:
                    docx_path = Path(directory) / "bulletin.docx"
                    export_markdown_docx(markdown, docx_path)
                    docx = docx_path.read_bytes()
                docx_hash = hashlib.sha256(docx).hexdigest()
                docx_blob_id = await self._artifact_store.put_bytes(
                    docx,
                    bucket=EDITION_DOCX_BLOB_BUCKET,
                    mime_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                )
                release = EditionRelease(
                    edition_id=edition.id,
                    manifest_id=manifest.id,
                    edition_document_blob_id=edition_blob_id,
                    markdown_blob_id=markdown_blob_id,
                    docx_blob_id=docx_blob_id,
                    edition_document_sha256=edition_hash,
                    markdown_sha256=markdown_hash,
                    docx_sha256=docx_hash,
                )
                if not await uow.edition_releases.add_if_absent(release):
                    persisted = await uow.edition_releases.get_by_manifest(manifest.id)
                    if persisted is None:
                        raise PublicationAssemblyError("release_idempotency_conflict")
                    release = persisted
                workspace_payload = (edition_document.to_json(), markdown, docx)
                if edition.status is EditionStatus.ASSEMBLING:
                    before = edition.snapshot()
                    edition.transition(EditionStatus.PUBLISHED)
                    if not await uow.editions.update(edition, before["version"]):
                        raise PublicationAssemblyError("edition_changed_during_assembly")
                    await uow.edition_audit.append(
                        EditionAuditEvent(
                            edition_id=edition.id,
                            actor_id="system:publication",
                            action="edition.published",
                            before=before,
                            after=edition.snapshot(),
                            correlation_id=await context.correlation_id() if context else "-",
                        )
                    )
            await uow.commit()

        if self._workspace_materializer is not None and workspace_payload is not None:
            try:
                edition_payload, markdown, docx = workspace_payload
                await self._workspace_materializer.materialize_release(
                    period=edition.period_start,
                    country_code=edition.country_code,
                    edition_id=edition.id,
                    manifest=manifest.to_json(),
                    edition=edition_payload,
                    markdown=markdown,
                    docx=docx,
                )
            except Exception:
                logger.exception("Unable to materialize edition release workspace")
        if context is not None:
            try:
                await context.report_progress(1, 1, "Bulletin publié")
            except Exception:
                # Publication is already durable; a progress bookkeeping
                # failure must not turn a successful release into a retry loop.
                logger.exception("Unable to report publication assembly progress")
        return release

    async def _finish_existing(
        self, uow: Any, release: EditionRelease, manifest: PublicationManifestV1
    ) -> EditionRelease:
        edition = await uow.editions.get_for_update(manifest.edition_id)
        if edition is not None and edition.status is EditionStatus.ASSEMBLING:
            before = edition.snapshot()
            edition.transition(EditionStatus.PUBLISHED)
            if not await uow.editions.update(edition, before["version"]):
                raise PublicationAssemblyError("edition_changed_during_assembly")
            await uow.edition_audit.append(
                EditionAuditEvent(
                    edition_id=edition.id,
                    actor_id="system:publication",
                    action="edition.published",
                    before=before,
                    after=edition.snapshot(),
                    correlation_id="-",
                )
            )
            await uow.commit()
        return release


class EditionAssembleParameters(JobParameters):
    model_config = ConfigDict(extra="forbid", strict=False)

    manifest_id: UUID


def register_publication_jobs(
    registry: JobRegistry,
    uow_factory: ProductionUnitOfWorkFactory,
    assembly_service: EditionAssemblyService,
) -> None:
    async def handle(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, EditionAssembleParameters):
            raise TypeError("Invalid edition assembly parameters")
        release = await assembly_service.assemble(parameters.manifest_id, context=context)
        return f"edition-release://{release.edition_id}/{release.id}"

    del uow_factory
    registry.register(
        EDITION_ASSEMBLE_JOB_KIND,
        EditionAssembleParameters,
        handle,
        resume_after_worker_loss=True,
    )


async def _get_edition_for_update(uow: Any, edition_id: UUID) -> Edition | None:
    repository = uow.editions
    getter = getattr(repository, "get_for_update", None)
    result = await getter(edition_id) if getter is not None else await repository.get(edition_id)
    return cast(Edition | None, result)


async def _get_run_for_update(uow: Any, run_id: UUID) -> Any:
    repository = uow.subject_production_runs
    getter = getattr(repository, "get_for_update", None)
    return await getter(run_id) if getter is not None else await repository.get(run_id)


__all__ = [
    "EDITION_ASSEMBLE_JOB_KIND",
    "EditionAssembleParameters",
    "EditionAssemblyService",
    "EditionPublicationService",
    "EditionReleaseStatus",
    "PublicationAcceptResult",
    "PublicationAcceptanceError",
    "PublicationAssemblyError",
    "PublicationManifestNotFoundError",
    "register_publication_jobs",
]
