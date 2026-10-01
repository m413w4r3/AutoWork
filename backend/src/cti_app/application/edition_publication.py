"""Freeze an edition review and assemble its immutable publication release."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import ConfigDict

from cti_app.application.edition_document import (
    EditionDocumentArtifactRef,
    EditionDocumentBuildError,
    build_edition_document,
)
from cti_app.application.edition_release_materialization import (
    EditionReleaseRematerializationService,
)
from cti_app.application.edition_rendering import (
    EDITION_RENDER_JOB_KIND,
    EditionRenderParameters,
    EditionRenderService,
)
from cti_app.application.edition_review import (
    EditionReviewService,
    ProductionRepairIssueReader,
)
from cti_app.application.jobs import (
    DuplicateJobError,
    JobDispatcher,
    JobExecutionContext,
    JobParameters,
    JobRegistry,
    JobService,
)
from cti_app.application.persistence import JobUnitOfWorkFactory, ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_repairs import (
    publication_is_compatible_with_current_effective_inputs,
)
from cti_app.application.typst_render_output import (
    TypstRenderOutputIntegrityError,
    TypstRenderOutputStorageError,
    read_verified_render_pdf,
)
from cti_app.domain.edition_publication import (
    EditionRelease,
    PublicationManifestEntryV1,
    PublicationManifestExclusionV1,
    PublicationManifestV1,
)
from cti_app.domain.edition_render import EditionRender, EditionRenderDisplayStatus
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.jobs import InvalidJobTransitionError, Job, JobStatus
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.typst_render import TypstRenderStatus

logger = logging.getLogger(__name__)

EDITION_ASSEMBLE_JOB_KIND = "publication.edition.assemble"
MANIFEST_BLOB_BUCKET = "publication-manifests"
EDITION_DOCUMENT_BLOB_BUCKET = "edition-documents"


class PublicationError(ValueError):
    pass


class PublicationAcceptanceError(PublicationError):
    pass


class PublicationAssemblyError(PublicationError):
    pass


class PublicationManifestNotFoundError(PublicationError):
    pass


class ReleasePdfNotAvailableError(PublicationError):
    """The latest release has no successfully rendered PDF yet."""


class ReleasePdfUnreadableError(PublicationError):
    """The rendered PDF recorded for the release cannot be served."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EditionReleasePdf:
    content: bytes
    filename: str


@dataclass(frozen=True, slots=True)
class PublicationAcceptResult:
    manifest: PublicationManifestV1
    job_id: UUID | None
    job_dispatched: bool
    edition_state: EditionStatus

    @property
    def manifest_id(self) -> UUID:
        return self.manifest.id


@dataclass(frozen=True, slots=True)
class EditionReleaseStatus:
    edition_id: UUID
    edition_state: EditionStatus
    manifest_id: UUID | None
    manifest_sha256: str | None
    release: EditionRelease | None
    assembly_job_id: UUID | None
    assembly_status: JobStatus | None
    assembly_error_code: str | None
    assembly_error_message: str | None
    can_retry_assembly: bool
    render: EditionRender | None
    render_job_id: UUID | None
    render_job_status: JobStatus | None
    render_job_error_code: str | None
    render_job_error_message: str | None

    @property
    def json_available(self) -> bool:
        return self.release is not None

    @property
    def pdf_available(self) -> bool:
        return self.render is not None and self.render.status is TypstRenderStatus.SUCCEEDED

    @property
    def render_id(self) -> UUID | None:
        return self.render.id if self.render is not None else None

    @property
    def render_status(self) -> EditionRenderDisplayStatus:
        if self.render is not None and self.render.status is TypstRenderStatus.SUCCEEDED:
            return EditionRenderDisplayStatus.SUCCEEDED
        if self.release is None:
            return EditionRenderDisplayStatus.NONE
        # An active job supersedes a FAILED row: a retry stays visible as work in
        # progress until the worker takes the row over.
        if self.render_job_status is JobStatus.QUEUED:
            return EditionRenderDisplayStatus.QUEUED
        if self.render_job_status is JobStatus.RUNNING:
            return EditionRenderDisplayStatus.RUNNING
        if self.render is not None:
            return EditionRenderDisplayStatus(self.render.status.value)
        if self.render_job_status in {JobStatus.FAILED, JobStatus.CANCELLED}:
            return EditionRenderDisplayStatus.FAILED
        # Without a render row, only an active job proves work has started;
        # otherwise the honest state is not_started.
        return EditionRenderDisplayStatus.NOT_STARTED

    @property
    def render_error_code(self) -> str | None:
        return (
            self.render.error_code
            if self.render is not None and self.render.error_code is not None
            else self.render_job_error_code
        )

    @property
    def render_error_message(self) -> str | None:
        return (
            self.render.error_message
            if self.render is not None and self.render.error_message is not None
            else self.render_job_error_message
        )

    @property
    def can_retry_render(self) -> bool:
        return self.release is not None and self.render_status in {
            EditionRenderDisplayStatus.NOT_STARTED,
            EditionRenderDisplayStatus.FAILED,
        }

    @property
    def published_at(self) -> datetime | None:
        if self.render is None or self.render.status is not TypstRenderStatus.SUCCEEDED:
            return None
        return self.render.updated_at


@dataclass(frozen=True, slots=True)
class _ResolvedPublicationInputs:
    edition_version: int
    batch_id: UUID
    entries: tuple[PublicationManifestEntryV1, ...]
    exclusions: tuple[PublicationManifestExclusionV1, ...]


class EditionPublicationService:
    """Transaction boundary for review acceptance and recoverable dispatch."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        *,
        job_service: JobService | None = None,
        job_dispatcher: JobDispatcher | None = None,
        repair_issue_reader: ProductionRepairIssueReader | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._job_service = job_service
        self._job_dispatcher = job_dispatcher
        self._repair_issue_reader = repair_issue_reader

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

            if edition.state is EditionStatus.ARCHIVED:
                raise PublicationAcceptanceError("edition_must_be_open")
            current_inputs = await _resolve_current_publication_inputs(
                uow,
                edition,
                repair_issue_reader=self._repair_issue_reader,
                validate_review=True,
            )
            manifest = await uow.publication_manifests.get_latest_for_edition(edition_id)
            if manifest is not None:
                release = await uow.edition_releases.get_by_manifest(manifest.id)
            else:
                release = None
            if (
                manifest is not None
                and release is None
                and _publication_inputs_match(manifest, current_inputs)
            ):
                # A matching pending snapshot is resumed. A released snapshot
                # is never reused: every explicit accept creates a new one.
                result = PublicationAcceptResult(manifest, None, False, edition.state)
            else:
                result = await self._freeze(uow, edition, actor_id, current_inputs)

        job_id, dispatched = await self._ensure_assembly_job(
            result.manifest, correlation_id=correlation_id, actor_id=actor_id
        )
        return PublicationAcceptResult(result.manifest, job_id, dispatched, result.edition_state)

    async def _freeze(
        self,
        uow: Any,
        edition: Edition,
        actor_id: str,
        inputs: _ResolvedPublicationInputs,
    ) -> PublicationAcceptResult:
        manifest = PublicationManifestV1.create(
            edition_id=edition.id,
            edition_version=edition.version,
            batch_id=inputs.batch_id,
            created_by=actor_id,
            entries=inputs.entries,
            exclusions=inputs.exclusions,
        )
        manifest_blob_id, _ = await self._artifact_store.put_canonical_json(
            manifest.to_json(), bucket=MANIFEST_BLOB_BUCKET
        )
        await uow.publication_manifests.add(manifest, manifest_blob_id)
        await uow.publication_manifest_entries.append_many(manifest.id, manifest.entries)
        await uow.publication_manifest_exclusions.append_many(manifest.id, manifest.exclusions)
        await uow.commit()
        return PublicationAcceptResult(manifest, None, False, edition.state)

    async def _ensure_assembly_job(
        self,
        manifest: PublicationManifestV1,
        *,
        correlation_id: str,
        actor_id: str,
    ) -> tuple[UUID | None, bool]:
        if self._job_service is None or self._job_dispatcher is None:
            return None, False
        return await _ensure_queued_job(
            self._job_service,
            self._job_dispatcher,
            kind=EDITION_ASSEMBLE_JOB_KIND,
            edition_id=manifest.edition_id,
            parameter_name="manifest_id",
            parameter_value=manifest.id,
            idempotency_prefix=f"publication-assemble-{manifest.id}",
            correlation_id=correlation_id,
            actor_id=actor_id,
        )

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
            render = (
                await uow.edition_renders.get_latest_for_release(release.id)
                if release is not None
                else None
            )
            assembly_job = None
            render_job = None
            jobs = getattr(uow, "jobs", None)
            if jobs is not None and manifest is not None:
                assembly_jobs = await jobs.list_for_aggregate(
                    "edition", edition_id, kind=EDITION_ASSEMBLE_JOB_KIND
                )
                assembly_job = _latest_assembly_job(assembly_jobs, manifest.id)
            if jobs is not None and release is not None:
                render_jobs = await jobs.list_for_aggregate(
                    "edition", edition_id, kind=EDITION_RENDER_JOB_KIND
                )
                render_job = _latest_render_job(render_jobs, release.id)
            current_inputs_match = False
            if manifest is not None and release is None:
                try:
                    current_inputs = await _resolve_current_publication_inputs(
                        uow,
                        edition,
                        repair_issue_reader=None,
                        validate_review=False,
                    )
                except PublicationError:
                    current_inputs = None
                current_inputs_match = current_inputs is not None and _publication_inputs_match(
                    manifest, current_inputs
                )
            can_retry_assembly = (
                edition.state is EditionStatus.OPEN
                and manifest is not None
                and release is None
                and current_inputs_match
                and (
                    assembly_job is None
                    or assembly_job.status in {JobStatus.FAILED, JobStatus.CANCELLED}
                )
            )
            return EditionReleaseStatus(
                edition_id=edition_id,
                edition_state=edition.state,
                manifest_id=manifest.id if manifest is not None else None,
                manifest_sha256=manifest.content_sha256 if manifest is not None else None,
                release=release,
                assembly_job_id=assembly_job.id if assembly_job is not None else None,
                assembly_status=assembly_job.status if assembly_job is not None else None,
                assembly_error_code=assembly_job.error_code if assembly_job is not None else None,
                assembly_error_message=(
                    assembly_job.error_message if assembly_job is not None else None
                ),
                can_retry_assembly=can_retry_assembly,
                render=render,
                render_job_id=render_job.id if render_job is not None else None,
                render_job_status=render_job.status if render_job is not None else None,
                render_job_error_code=(render_job.error_code if render_job is not None else None),
                render_job_error_message=(
                    render_job.error_message if render_job is not None else None
                ),
            )

    async def read_release_pdf(self, edition_id: UUID) -> EditionReleasePdf:
        """Return the verified PDF of the latest release's most recent successful render."""
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            manifest = await uow.publication_manifests.get_latest_for_edition(edition_id)
            release = (
                await uow.edition_releases.get_by_manifest(manifest.id)
                if manifest is not None
                else None
            )
            render = (
                await uow.edition_renders.get_latest_for_release(release.id)
                if release is not None
                else None
            )
            await uow.commit()
        if edition is None or release is None:
            raise PublicationManifestNotFoundError("edition_release_not_found")
        if render is None or render.status is not TypstRenderStatus.SUCCEEDED:
            raise ReleasePdfNotAvailableError("release_pdf_not_available")
        try:
            content = await read_verified_render_pdf(self._artifact_store, render)
        except TypstRenderOutputStorageError as exc:
            raise ReleasePdfUnreadableError("release_pdf_storage_error") from exc
        except TypstRenderOutputIntegrityError as exc:
            raise ReleasePdfUnreadableError("release_pdf_integrity_mismatch") from exc
        return EditionReleasePdf(content=content, filename=edition.bulletin_pdf_filename())

    async def retry_render(
        self,
        edition_id: UUID,
        *,
        actor_id: str,
        correlation_id: str = "-",
    ) -> EditionReleaseStatus:
        """Ensure the render job for the current release without changing its inputs."""
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            manifest = await uow.publication_manifests.get_latest_for_edition(edition_id)
            release = (
                await uow.edition_releases.get_by_manifest(manifest.id)
                if manifest is not None
                else None
            )
            render = (
                await uow.edition_renders.get_latest_for_release(release.id)
                if release is not None
                else None
            )
            await uow.commit()
        if edition is None or release is None:
            raise PublicationManifestNotFoundError("edition_release_not_found")
        if render is not None and render.status is TypstRenderStatus.SUCCEEDED:
            return await self.release_status(edition_id)

        await ensure_edition_render_job(
            self._job_service,
            self._job_dispatcher,
            edition_id=edition_id,
            edition_release_id=release.id,
            correlation_id=correlation_id,
            actor_id=actor_id,
        )
        return await self.release_status(edition_id)


class EditionAssemblyService:
    """Assemble only the artifact IDs and hashes recorded by a manifest."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def assemble(
        self, manifest_id: UUID, *, context: JobExecutionContext | None = None
    ) -> EditionRelease:
        # Phase 1: every potentially slow read/render/blob operation runs
        # outside an Edition FOR UPDATE lock. Only immutable IDs from the
        # manifest are followed; no current artifact is resolved here.
        async with self._uow_factory() as uow:
            manifest = await uow.publication_manifests.get(manifest_id)
            if manifest is None:
                raise PublicationManifestNotFoundError(str(manifest_id))
            existing = await uow.edition_releases.get_by_manifest(manifest_id)
            edition = await uow.editions.get(manifest.edition_id)
            if edition is None:
                raise PublicationAssemblyError("edition_not_found")
            if existing is None:
                if edition.state is not EditionStatus.OPEN:
                    raise PublicationAssemblyError("edition_must_be_open")
                if edition.version != manifest.edition_version:
                    raise PublicationAssemblyError("edition_changed_after_publication_freeze")
                try:
                    current_inputs = await _resolve_current_publication_inputs(
                        uow,
                        edition,
                        repair_issue_reader=None,
                        validate_review=False,
                    )
                except PublicationError as exc:
                    raise PublicationAssemblyError(
                        "publication_inputs_changed_after_freeze"
                    ) from exc
                if not _publication_inputs_match(manifest, current_inputs):
                    raise PublicationAssemblyError("publication_inputs_changed_after_freeze")
            manifest_blob_id = None
            if existing is None:
                manifest_blob_id = await uow.publication_manifests.get_blob_id(manifest_id)
                if manifest_blob_id is None:
                    raise PublicationAssemblyError("manifest_blob_missing")

        if existing is None:
            assert manifest_blob_id is not None
            blob_payload = await self._artifact_store.read_json(manifest_blob_id)
            blob_manifest = PublicationManifestV1.from_json(blob_payload)
            if blob_manifest != manifest:
                raise PublicationAssemblyError("manifest_blob_mismatch")

            refs = tuple(
                EditionDocumentArtifactRef(
                    position=entry.position,
                    subject_id=entry.subject_id,
                    production_run_id=entry.production_run_id,
                    pipeline_generation=entry.pipeline_generation,
                    artifact_id=entry.document_artifact_id,
                    artifact_version=entry.document_artifact_version,
                    input_hash=entry.document_input_hash,
                )
                for entry in manifest.entries
            )
            async with self._uow_factory() as uow:
                try:
                    edition_document = await build_edition_document(
                        uow,
                        self._artifact_store,
                        edition,
                        refs,
                        require_current=False,
                    )
                except EditionDocumentBuildError as exc:
                    code = (
                        "manifest_artifact_mismatch"
                        if exc.code == "edition_document_artifact_mismatch"
                        else exc.code
                    )
                    raise PublicationAssemblyError(code) from exc
            edition_json = edition_document.to_json()
            edition_blob_id, edition_hash = await self._artifact_store.put_canonical_json(
                edition_json, bucket=EDITION_DOCUMENT_BLOB_BUCKET
            )
            candidate_release = EditionRelease(
                edition_id=manifest.edition_id,
                manifest_id=manifest.id,
                edition_document_blob_id=edition_blob_id,
                edition_document_sha256=edition_hash,
            )
        else:
            candidate_release = None

        # Phase 2: only short database work occurs while Edition is locked.
        async with self._uow_factory() as uow:
            edition = await uow.editions.get_for_update(manifest.edition_id)
            if edition is None:
                raise PublicationAssemblyError("edition_not_found")
            existing = await uow.edition_releases.get_by_manifest(manifest_id)
            if existing is not None:
                release = existing
            else:
                if edition.state is not EditionStatus.OPEN:
                    raise PublicationAssemblyError("edition_must_be_open")
                if edition.version != manifest.edition_version:
                    raise PublicationAssemblyError("edition_changed_after_publication_freeze")
                try:
                    current_inputs = await _resolve_current_publication_inputs(
                        uow,
                        edition,
                        repair_issue_reader=None,
                        validate_review=False,
                    )
                except PublicationError as exc:
                    raise PublicationAssemblyError(
                        "publication_inputs_changed_after_freeze"
                    ) from exc
                if not _publication_inputs_match(manifest, current_inputs):
                    raise PublicationAssemblyError("publication_inputs_changed_after_freeze")
                if candidate_release is None:
                    raise PublicationAssemblyError("release_candidate_missing")
                release = candidate_release
                if not await uow.edition_releases.add_if_absent(release):
                    persisted = await uow.edition_releases.get_by_manifest(manifest.id)
                    if persisted is None:
                        raise PublicationAssemblyError("release_idempotency_conflict")
                    release = persisted
            await uow.commit()

        if context is not None:
            try:
                await context.report_progress(1, 1, "Bulletin publié")
            except Exception:
                # Publication is already durable; a progress bookkeeping
                # failure must not turn a successful release into a retry loop.
                logger.exception("Unable to report publication assembly progress")
        return release


class EditionAssembleParameters(JobParameters):
    model_config = ConfigDict(extra="forbid", strict=False)

    manifest_id: UUID


def register_publication_jobs(
    registry: JobRegistry,
    uow_factory: ProductionUnitOfWorkFactory,
    assembly_service: EditionAssemblyService,
    render_service: EditionRenderService,
    job_dispatcher: JobDispatcher,
    release_rematerializer: EditionReleaseRematerializationService,
) -> None:
    async def handle_assembly(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, EditionAssembleParameters):
            raise TypeError("Invalid edition assembly parameters")
        release = await assembly_service.assemble(parameters.manifest_id, context=context)
        await ensure_edition_render_job(
            JobService(cast(JobUnitOfWorkFactory, uow_factory), registry),
            job_dispatcher,
            edition_id=release.edition_id,
            edition_release_id=release.id,
            correlation_id=await context.correlation_id(),
            actor_id="system:worker",
        )
        return f"edition-release://{release.edition_id}/{release.id}"

    registry.register(
        EDITION_ASSEMBLE_JOB_KIND,
        EditionAssembleParameters,
        handle_assembly,
        resume_after_worker_loss=True,
    )

    async def handle_render(parameters: JobParameters, context: JobExecutionContext) -> str:
        if not isinstance(parameters, EditionRenderParameters):
            raise TypeError("Invalid edition render parameters")
        render = await render_service.render_pdf(parameters.edition_release_id)
        try:
            await release_rematerializer.materialize(
                edition_release_id=render.edition_release_id,
                edition_render_id=render.id,
            )
        except Exception:
            logger.exception("Unable to materialize edition release after render %s", render.id)
        try:
            await context.report_progress(1, 1, "PDF du bulletin prêt")
        except Exception:
            logger.exception("Unable to report edition render progress for %s", render.id)
        return f"edition-render://{render.edition_release_id}/{render.id}"

    registry.register(
        EDITION_RENDER_JOB_KIND,
        EditionRenderParameters,
        handle_render,
        resume_after_worker_loss=True,
    )


async def _resolve_current_publication_inputs(
    uow: Any,
    edition: Edition,
    *,
    repair_issue_reader: ProductionRepairIssueReader | None,
    validate_review: bool,
) -> _ResolvedPublicationInputs:
    batch = await uow.edition_production_batches.get_latest_for_edition(edition.id)
    if batch is None:
        raise PublicationAcceptanceError("edition_has_no_production_batch")

    rows = await uow.edition_review_read_model.list_for_edition(edition.id)
    repairs: tuple[Any, ...] = ()
    if repair_issue_reader is not None:
        getter = getattr(repair_issue_reader, "list_issue_views", None)
        extraction = (
            await getter(edition.id)
            if callable(getter)
            else await repair_issue_reader.list_issues(edition.id)  # type: ignore[attr-defined]
        )
        supplemental = await repair_issue_reader.list_supplemental_source_issues(edition.id)
        repairs = tuple([*extraction, *supplemental])
    review = EditionReviewService.from_rows(edition.id, rows, repair_issues=repairs)
    if validate_review and not review.can_accept:
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
                or artifact.stage is not ProductionArtifactStage.PUBLICATION
                or artifact.status is not ProductionArtifactStatus.VERIFIED
                or artifact.canonical_blob_id is None
            ):
                raise PublicationAcceptanceError("included_artifact_mismatch")
            if validate_review:
                # Defence in depth: the manifest must never freeze a document
                # older than the repair already applied to its Extraction.
                # The proof comes from the document's own recorded inputs, not
                # from a decision marker on the Extraction.
                current_extraction = await uow.production_artifacts.get_current(
                    item.run_id, ProductionArtifactStage.EXTRACTION.value
                )
                current_references = await uow.production_artifacts.get_current(
                    item.run_id, ProductionArtifactStage.REFERENCES.value
                )
                if current_extraction is not None and (
                    not publication_is_compatible_with_current_effective_inputs(
                        publication=artifact,
                        extraction=current_extraction,
                        references=current_references,
                    )
                ):
                    raise PublicationAcceptanceError("repair_materialization_incomplete")
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
    return _ResolvedPublicationInputs(
        edition_version=edition.version,
        batch_id=batch.id,
        entries=tuple(sorted(entries, key=lambda item: item.position)),
        exclusions=tuple(sorted(exclusions, key=lambda item: str(item.subject_id))),
    )


def _publication_inputs_match(
    manifest: PublicationManifestV1, inputs: _ResolvedPublicationInputs
) -> bool:
    return (
        manifest.edition_version == inputs.edition_version
        and manifest.batch_id == inputs.batch_id
        and manifest.entries == inputs.entries
        and manifest.exclusions == inputs.exclusions
    )


async def _get_edition_for_update(uow: Any, edition_id: UUID) -> Edition | None:
    repository = uow.editions
    getter = getattr(repository, "get_for_update", None)
    result = await getter(edition_id) if getter is not None else await repository.get(edition_id)
    return cast(Edition | None, result)


async def _get_run_for_update(uow: Any, run_id: UUID) -> Any:
    repository = uow.production_runs
    getter = getattr(repository, "get_for_update", None)
    return await getter(run_id) if getter is not None else await repository.get(run_id)


def _latest_job(jobs: Any, kind: str, parameter_name: str, parameter_value: UUID) -> Job | None:
    matching = [
        job
        for job in jobs
        if job.kind == kind
        and str(job.input_parameters.get(parameter_name)) == str(parameter_value)
    ]
    return max(matching, key=lambda job: (job.created_at, str(job.id)), default=None)


def _latest_assembly_job(jobs: Any, manifest_id: UUID) -> Job | None:
    return _latest_job(jobs, EDITION_ASSEMBLE_JOB_KIND, "manifest_id", manifest_id)


def _latest_render_job(jobs: Any, edition_release_id: UUID) -> Job | None:
    return _latest_job(jobs, EDITION_RENDER_JOB_KIND, "edition_release_id", edition_release_id)


async def ensure_edition_render_job(
    job_service: JobService | None,
    job_dispatcher: JobDispatcher | None,
    *,
    edition_id: UUID,
    edition_release_id: UUID,
    correlation_id: str,
    actor_id: str,
) -> tuple[UUID | None, bool]:
    """Idempotently queue and dispatch the PDF render for a committed release."""
    if job_service is None:
        return None, False
    return await _ensure_queued_job(
        job_service,
        job_dispatcher,
        kind=EDITION_RENDER_JOB_KIND,
        edition_id=edition_id,
        parameter_name="edition_release_id",
        parameter_value=edition_release_id,
        idempotency_prefix=f"edition-render-{edition_release_id}",
        correlation_id=correlation_id,
        actor_id=actor_id,
    )


async def _ensure_queued_job(
    job_service: JobService,
    job_dispatcher: JobDispatcher | None,
    *,
    kind: str,
    edition_id: UUID,
    parameter_name: str,
    parameter_value: UUID,
    idempotency_prefix: str,
    correlation_id: str,
    actor_id: str,
) -> tuple[UUID | None, bool]:
    """Reuse, repair or succeed the job for one frozen input, then dispatch it.

    Failing to assure or dispatch the job never invalidates the committed
    canonical state; a later call finds the same idempotent job and retries.
    """

    async def submit(idempotency_key: str, max_attempts: int) -> Job:
        try:
            return await job_service.submit(
                kind=kind,
                aggregate_type="edition",
                aggregate_id=edition_id,
                idempotency_key=idempotency_key,
                correlation_id=correlation_id,
                input_parameters={parameter_name: str(parameter_value)},
                max_attempts=max_attempts,
                actor_id=actor_id,
            )
        except DuplicateJobError as exc:
            return await job_service.get(exc.existing_job_id)

    try:
        jobs = await job_service.list_for_aggregate("edition", edition_id, kind=kind)
        job = _latest_job(jobs, kind, parameter_name, parameter_value)
        if job is None:
            job = await submit(idempotency_prefix, 3)

        # A terminal failed job is reused while it still has manual attempts;
        # once exhausted (or cancelled), the successor gets a distinct
        # deterministic idempotency key.
        for _ in range(3):
            if job.status is JobStatus.QUEUED:
                break
            if job.status in {JobStatus.RUNNING, JobStatus.SUCCEEDED, JobStatus.WAITING_HUMAN}:
                return job.id, False
            if job.status is JobStatus.FAILED and job.attempt < job.max_attempts:
                try:
                    job = await job_service.retry(job.id, actor_id=actor_id)
                except InvalidJobTransitionError:
                    # A concurrent caller may have repaired the same job.
                    # Reload it and apply the state policy to the winner.
                    job = await job_service.get(job.id)
                continue
            if job.status in {JobStatus.FAILED, JobStatus.CANCELLED}:
                job = await submit(f"{idempotency_prefix}-after-{job.id}", job.max_attempts)
                continue
        else:
            raise RuntimeError(f"{kind} job state did not stabilize")
    except Exception:
        logger.exception("Unable to assure %s job for %s %s", kind, parameter_name, parameter_value)
        return None, False

    if job_dispatcher is None:
        return job.id, False
    try:
        await job_dispatcher.dispatch(job.id)
    except Exception:
        logger.exception("Unable to dispatch %s job %s", kind, job.id)
        return job.id, False
    return job.id, True


__all__ = [
    "EDITION_ASSEMBLE_JOB_KIND",
    "EDITION_RENDER_JOB_KIND",
    "EditionAssembleParameters",
    "EditionAssemblyService",
    "EditionPublicationService",
    "EditionReleasePdf",
    "EditionReleaseStatus",
    "EditionRenderParameters",
    "PublicationAcceptResult",
    "PublicationAcceptanceError",
    "PublicationAssemblyError",
    "PublicationManifestNotFoundError",
    "ReleasePdfNotAvailableError",
    "ReleasePdfUnreadableError",
    "ensure_edition_render_job",
    "register_publication_jobs",
]
