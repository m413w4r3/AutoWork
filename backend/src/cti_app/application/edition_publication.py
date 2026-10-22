"""Freeze an edition review and assemble its immutable publication release."""

from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, cast
from uuid import UUID

from pydantic import ConfigDict

from cti_app.application.edition_document import (
    EditionDocumentArtifactRef,
    EditionDocumentBuildError,
    build_edition_document,
)
from cti_app.application.edition_ioc_export import (
    EditionIOCArchive,
    build_edition_ioc_archive,
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
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_repairs import (
    publication_is_compatible_with_current_effective_inputs,
)
from cti_app.application.typst_render_output import (
    TypstRenderOutputIntegrityError,
    TypstRenderOutputStorageError,
    read_verified_render_pdf,
)
from cti_app.domain.edition_composition import CompositionDisposition
from cti_app.domain.edition_publication import (
    EditionDocumentV2,
    EditionRelease,
    PublicationManifest,
    PublicationManifestEntryV2,
    PublicationManifestExclusionV1,
    PublicationManifestV1,
    PublicationManifestV2,
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
    code = "publication_acceptance_rejected"

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class PublicationCompositionVersionConflict(PublicationAcceptanceError):
    def __init__(self, expected_version: int, actual_version: int) -> None:
        self.expected_version = expected_version
        self.actual_version = actual_version
        super().__init__("composition_version_conflict")


class PublicationCompositionChangedDuringFreeze(PublicationAcceptanceError):
    def __init__(self) -> None:
        super().__init__("composition_changed_during_freeze")


class PublicationArtifactChangedDuringFreeze(PublicationAcceptanceError):
    def __init__(self) -> None:
        super().__init__("publication_artifact_changed_during_freeze")


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


class ReleaseIOCArchiveUnreadableError(PublicationError):
    """The immutable edition document cannot be safely exported."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EditionReleasePdf:
    content: bytes
    filename: str
    lineage_manifest: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class PublicationAcceptResult:
    manifest: PublicationManifestV2
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
    composition_id: UUID
    composition_version: int
    entries: tuple[PublicationManifestEntryV2, ...]
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
        expected_composition_version: int,
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
                expected_composition_version=expected_composition_version,
            )
            manifest = await uow.publication_manifests.get_latest_manifest_for_edition(edition_id)
            if manifest is not None:
                release = await uow.edition_releases.get_by_manifest(manifest.id)
            else:
                release = None
            if (
                isinstance(manifest, PublicationManifestV2)
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
        current_composition = await uow.edition_compositions.get_for_edition(edition.id)
        if (
            current_composition is None
            or current_composition.id != inputs.composition_id
            or current_composition.version != inputs.composition_version
        ):
            raise PublicationCompositionChangedDuringFreeze()
        manifest = PublicationManifestV2.create(
            edition_id=edition.id,
            edition_version=edition.version,
            composition_id=inputs.composition_id,
            composition_version=inputs.composition_version,
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
        manifest: PublicationManifest,
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
            manifest = await uow.publication_manifests.get_latest_manifest_for_edition(edition_id)
            release = (
                await uow.edition_releases.get_by_manifest(manifest.id)
                if manifest is not None
                else None
            )
            latest_render = (
                await uow.edition_renders.get_latest_for_release(release.id)
                if release is not None
                else None
            )
            pinned_render = (
                await uow.edition_renders.get_pinned_succeeded_for_release(release.id)
                if release is not None
                else None
            )
            render = pinned_render or latest_render
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
                if isinstance(manifest, PublicationManifestV1):
                    current_inputs_match = edition.version == manifest.edition_version
                else:
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

    async def read_release_pdf(
        self, edition_id: UUID, release_id: UUID | None = None
    ) -> EditionReleasePdf:
        """Return the PDF pinned by the latest or explicitly versioned release."""
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if release_id is not None:
                release = await uow.edition_releases.get(release_id)
                manifest = (
                    await uow.publication_manifests.get(release.manifest_id)
                    if release is not None and release.edition_id == edition_id
                    else None
                )
            else:
                manifest = await uow.publication_manifests.get_latest_manifest_for_edition(
                    edition_id
                )
                release = (
                    await uow.edition_releases.get_by_manifest(manifest.id)
                    if manifest is not None
                    else None
                )
            render = (
                await uow.edition_renders.get_pinned_succeeded_for_release(release.id)
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
        lineage = await self._release_pdf_lineage(edition, release, manifest, render)
        return EditionReleasePdf(
            content=content,
            filename=edition.bulletin_pdf_filename(),
            lineage_manifest=lineage,
        )

    async def read_release_lineage(
        self, edition_id: UUID, release_id: UUID | None = None
    ) -> dict[str, Any]:
        """Return provenance without reading PDF bytes, optionally for an exact release."""
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if release_id is not None:
                release = await uow.edition_releases.get(release_id)
                manifest = (
                    await uow.publication_manifests.get(release.manifest_id)
                    if release is not None and release.edition_id == edition_id
                    else None
                )
            else:
                manifest = await uow.publication_manifests.get_latest_manifest_for_edition(
                    edition_id
                )
                release = (
                    await uow.edition_releases.get_by_manifest(manifest.id)
                    if manifest is not None
                    else None
                )
            render = (
                await uow.edition_renders.get_pinned_succeeded_for_release(release.id)
                if release is not None
                else None
            )
            await uow.commit()
        if edition is None or release is None:
            raise PublicationManifestNotFoundError("edition_release_not_found")
        if render is None or render.status is not TypstRenderStatus.SUCCEEDED:
            raise ReleasePdfNotAvailableError("release_pdf_not_available")
        return await self._release_pdf_lineage(edition, release, manifest, render)

    async def _reconstruct_release_publications(
        self, release: EditionRelease, manifest: PublicationManifest
    ) -> list[dict[str, Any]]:
        """Rebuild a legacy release's source identities from its immutable pins."""
        document_bytes = await self._artifact_store.read_bytes(
            release.edition_document_blob_id, max_bytes=MAX_ARTIFACT_BYTES
        )
        if hashlib.sha256(document_bytes).hexdigest() != release.edition_document_sha256:
            raise ValueError("edition document hash does not match the release pin")
        payload = json.loads(document_bytes)
        if not isinstance(payload, dict):
            raise ValueError("edition document is not a JSON object")
        document = EditionDocumentV2.from_json(payload)
        publication_by_subject = {
            str(item.subject_id): item.document for item in document.publications
        }
        result: list[dict[str, Any]] = []
        async with self._uow_factory() as uow:
            runtime_uow = cast(Any, uow)
            for entry in manifest.entries:
                artifact = await uow.production_artifacts.get(entry.document_artifact_id)
                publication = publication_by_subject.get(str(entry.subject_id))
                review_decision_id = getattr(entry, "review_decision_id", None)
                expected_sha256 = getattr(entry, "document_sha256", None)
                warnings: list[str] = []
                if artifact is None:
                    result.append(
                        {
                            "subject_id": str(entry.subject_id),
                            "production_run_id": str(entry.production_run_id),
                            "artifact": {
                                "id": str(entry.document_artifact_id),
                                "version": entry.document_artifact_version,
                                "sha256": expected_sha256,
                                "document_schema": getattr(publication, "schema_version", None),
                            },
                            "opinion": None,
                            "review_decision_id": (
                                str(review_decision_id) if review_decision_id is not None else None
                            ),
                            "lineage_warning": "pinned_artifact_unavailable",
                        }
                    )
                    continue
                canonical_sha256 = None
                if artifact.canonical_blob_id is not None:
                    canonical_bytes = await self._artifact_store.read_bytes(
                        artifact.canonical_blob_id, max_bytes=MAX_ARTIFACT_BYTES
                    )
                    canonical_sha256 = hashlib.sha256(canonical_bytes).hexdigest()
                else:
                    warnings.append("pinned_artifact_canonical_blob_unavailable")
                if (
                    artifact.version != entry.document_artifact_version
                    or artifact.production_run_id != entry.production_run_id
                    or artifact.subject_id != entry.subject_id
                    or (expected_sha256 is not None and canonical_sha256 != expected_sha256)
                ):
                    warnings.append("manifest_and_artifact_identity_disagree")

                artifact_metadata = artifact.metadata if isinstance(artifact.metadata, dict) else {}
                augmentation = artifact_metadata.get("publication_augmentation")
                opinion_identity = None
                schema_version = getattr(publication, "schema_version", None)
                if (
                    schema_version == "6"
                    and isinstance(augmentation, dict)
                    and augmentation.get("opinion_id") is not None
                    and augmentation.get("opinion_version") is not None
                ):
                    opinion_id = UUID(str(augmentation["opinion_id"]))
                    opinion_version = int(augmentation["opinion_version"])
                    opinion = await runtime_uow.analyst_opinions.get(
                        opinion_id, version=opinion_version
                    )
                    opinion_sha256 = str(
                        augmentation.get("opinion_content_sha256")
                        or (opinion.content_sha256 if opinion is not None else "")
                    )
                    if opinion is None:
                        warnings.append("pinned_opinion_identity_unavailable")
                    elif opinion.content_sha256 != opinion_sha256:
                        warnings.append("pinned_opinion_sha256_mismatch")
                    opinion_identity = {
                        "id": str(opinion_id),
                        "version": opinion_version,
                        "status": opinion.status.value if opinion is not None else "unknown",
                        "sha256": opinion_sha256,
                        "investigation_id": str(augmentation.get("investigation_id")),
                    }
                    provenance = getattr(publication, "provenance", None)
                    if provenance is None or any(
                        (
                            str(getattr(provenance, "opinion_id", "")) != str(opinion_id),
                            getattr(provenance, "opinion_version", None) != opinion_version,
                            str(getattr(provenance, "opinion_content_sha256", ""))
                            != opinion_sha256,
                            str(getattr(provenance, "investigation_id", ""))
                            != str(augmentation.get("investigation_id")),
                        )
                    ):
                        warnings.append("publication_and_augmentation_provenance_disagree")
                elif schema_version == "6" or isinstance(augmentation, dict):
                    warnings.append("publication_schema_and_augmentation_metadata_disagree")

                result.append(
                    {
                        "subject_id": str(entry.subject_id),
                        "production_run_id": str(entry.production_run_id),
                        "artifact": {
                            "id": str(artifact.id),
                            "version": artifact.version,
                            "sha256": canonical_sha256,
                            "document_schema": schema_version,
                        },
                        "opinion": opinion_identity,
                        "review_decision_id": (
                            str(review_decision_id) if review_decision_id is not None else None
                        ),
                        "lineage_warning": ";".join(warnings) if warnings else None,
                    }
                )
            await uow.commit()
        return result

    async def _release_pdf_lineage(
        self,
        edition: Edition,
        release: EditionRelease,
        manifest: PublicationManifest | None,
        render: EditionRender,
    ) -> dict[str, Any]:
        render_data = b""
        if render.render_data_blob_id is not None:
            try:
                render_data = await self._artifact_store.read_bytes(
                    render.render_data_blob_id, max_bytes=MAX_ARTIFACT_BYTES
                )
            except Exception:
                # A legacy/missing render-data blob must not block delivery of a
                # verified pinned PDF; the JSON sidecar marks the identity gap.
                render_data = b""
        try:
            render_data_payload = json.loads(render_data) if render_data else {}
        except (json.JSONDecodeError, UnicodeDecodeError):
            render_data_payload = {}
        stored = (
            render_data_payload.get("pdf_lineage")
            if isinstance(render_data_payload, dict)
            else None
        )
        lineage = (
            dict(stored)
            if isinstance(stored, dict)
            else {
                "schema_version": "pdf-lineage.v1",
                "nature": "edition_release",
                "edition_id": str(edition.id),
                "edition_release_id": str(release.id),
                "manifest_id": str(release.manifest_id),
                "publications": [],
                "lineage_warning": "persisted_render_lineage_unavailable",
            }
        )
        reasons: list[str] = []
        publications = lineage.get("publications")
        publications = publications if isinstance(publications, list) else []
        reconstructed = False
        if not publications and manifest is not None:
            try:
                publications = await self._reconstruct_release_publications(release, manifest)
                lineage["publications"] = publications
                reconstructed = bool(publications)
                if reconstructed:
                    lineage["lineage_warning"] = "reconstructed_from_immutable_release_pins"
            except Exception:
                lineage["lineage_warning"] = "pinned_release_source_chain_unavailable"
        if reconstructed:
            lineage["lineage_source"] = "immutable_release_pins"
        elif isinstance(stored, dict):
            lineage["lineage_source"] = "persisted_render_data"
        else:
            lineage["lineage_source"] = "unavailable"
            reasons.append("persisted_render_lineage_unavailable")
        if not publications:
            reasons.append("pinned_publication_identities_unavailable")
        async with self._uow_factory() as uow:
            runtime_uow = cast(Any, uow)
            latest_manifest = await uow.publication_manifests.get_latest_manifest_for_edition(
                edition.id
            )
            if latest_manifest is None or latest_manifest.id != release.manifest_id:
                reasons.append("edition_release_is_not_latest")
            for publication in publications:
                if not isinstance(publication, dict):
                    reasons.append("malformed_pinned_publication_lineage")
                    continue
                publication_reasons: list[str] = []
                try:
                    subject_id = UUID(str(publication["subject_id"]))
                    run_id = UUID(str(publication["production_run_id"]))
                    artifact_data = publication.get("artifact")
                    if not isinstance(artifact_data, dict) or not artifact_data.get("id"):
                        raise ValueError("pinned artifact identity unavailable")
                    artifact_id = UUID(str(artifact_data["id"]))
                    current_run = await uow.production_runs.get_current_for_subject(subject_id)
                    current_artifact = await uow.production_artifacts.get_current(
                        run_id, ProductionArtifactStage.PUBLICATION.value
                    )
                    if current_run is None or current_run.id != run_id:
                        publication_reasons.append("production_run_is_not_current")
                    if current_artifact is None or current_artifact.id != artifact_id:
                        publication_reasons.append("publication_artifact_is_not_current")
                    opinion_data = publication.get("opinion")
                    if isinstance(opinion_data, dict):
                        opinion_id = UUID(str(opinion_data["id"]))
                        pinned_version = int(opinion_data["version"])
                        pinned_opinion = await runtime_uow.analyst_opinions.get(
                            opinion_id, version=pinned_version
                        )
                        current_opinion = await runtime_uow.analyst_opinions.get(opinion_id)
                        if pinned_opinion is None or current_opinion is None:
                            publication_reasons.append("pinned_opinion_identity_unavailable")
                        else:
                            opinion_data.update(
                                {
                                    "status": pinned_opinion.status.value,
                                    "sha256": pinned_opinion.content_sha256,
                                    "investigation_id": str(pinned_opinion.investigation_id),
                                }
                            )
                            if (
                                current_opinion.version != pinned_opinion.version
                                or current_opinion.content_sha256 != pinned_opinion.content_sha256
                            ):
                                publication_reasons.append("pinned_opinion_is_older_than_current")
                            current_investigation = (
                                await runtime_uow.analyst_investigations.get_for_run(current_run.id)
                                if current_run is not None
                                else None
                            )
                            if (
                                current_investigation is not None
                                and current_investigation.id != pinned_opinion.investigation_id
                            ):
                                publication_reasons.append("pinned_opinion_investigation_mismatch")
                    elif publication.get("lineage_warning"):
                        publication_reasons.append(str(publication["lineage_warning"]))
                except (KeyError, TypeError, ValueError):
                    publication_reasons.append("pinned_publication_identity_unavailable")
                publication["history_reasons"] = list(dict.fromkeys(publication_reasons))
                publication["is_current"] = not publication_reasons
                publication["historical"] = bool(publication_reasons)
                reasons.extend(publication_reasons)
            await uow.commit()

        render_data_sha256 = hashlib.sha256(render_data).hexdigest() if render_data else None
        lineage.update(
            {
                "render_id": str(render.id),
                "render_data_sha256": render_data_sha256,
                "renderer": {
                    "name": render.renderer,
                    "version": render.renderer_version,
                    "identity_sha256": hashlib.sha256(
                        f"{render.renderer}\0{render.renderer_version}".encode()
                    ).hexdigest(),
                },
                "template": {
                    "version": render.template_version,
                    "sha256": render.template_sha256,
                },
                "compiler": {
                    "name": render.compiler,
                    "version": render.compiler_version,
                    "identity_sha256": hashlib.sha256(
                        f"{render.compiler}\0{render.compiler_version}".encode()
                    ).hexdigest(),
                    "binary_sha256": None,
                },
                "rendered_at": render.created_at.isoformat(),
                "pdf_sha256": render.output_sha256,
                "is_current": not reasons,
                "historical": bool(reasons),
                "history_reasons": list(dict.fromkeys(reasons)),
            }
        )
        if manifest is None:
            lineage["lineage_warning"] = "publication_manifest_unavailable"
            lineage["is_current"] = False
            lineage["historical"] = True
            lineage["history_reasons"] = [
                *lineage["history_reasons"],
                "publication_manifest_unavailable",
            ]
        return lineage

    async def read_release_ioc_archive(self, edition_id: UUID) -> EditionIOCArchive:
        """Export IOC values only from the document pinned by the latest release."""
        async with self._uow_factory() as uow:
            manifest = await uow.publication_manifests.get_latest_manifest_for_edition(edition_id)
            release = (
                await uow.edition_releases.get_by_manifest(manifest.id)
                if manifest is not None
                else None
            )
            await uow.commit()
        if release is None or release.edition_id != edition_id:
            raise PublicationManifestNotFoundError("edition_release_not_found")

        try:
            content = await self._artifact_store.read_bytes(
                release.edition_document_blob_id,
                max_bytes=MAX_ARTIFACT_BYTES,
            )
        except ProductionReuseStorageUnavailableError as exc:
            raise ReleaseIOCArchiveUnreadableError("release_ioc_archive_storage_error") from exc
        if hashlib.sha256(content).hexdigest() != release.edition_document_sha256:
            raise ReleaseIOCArchiveUnreadableError("release_ioc_archive_integrity_mismatch")
        try:
            payload = json.loads(content)
            if not isinstance(payload, dict):
                raise ValueError("edition document must be a JSON object")
            document = EditionDocumentV2.from_json(payload)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ReleaseIOCArchiveUnreadableError("release_ioc_archive_document_invalid") from exc
        if str(document.edition.get("id")) != str(edition_id):
            raise ReleaseIOCArchiveUnreadableError("release_ioc_archive_edition_mismatch")
        try:
            return build_edition_ioc_archive(
                document,
                edition_sha256=release.edition_document_sha256,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ReleaseIOCArchiveUnreadableError("release_ioc_archive_document_invalid") from exc

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
            manifest = await uow.publication_manifests.get_latest_manifest_for_edition(edition_id)
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
            if existing is None and isinstance(manifest, PublicationManifestV2):
                if edition.state is not EditionStatus.OPEN:
                    raise PublicationAssemblyError("edition_must_be_open")
                if edition.version != manifest.edition_version:
                    raise PublicationAssemblyError("edition_changed_after_publication_freeze")
                _assert_manifest_inputs_current = True
            else:
                _assert_manifest_inputs_current = False
            if existing is None and isinstance(manifest, PublicationManifestV1):
                if edition.state is not EditionStatus.OPEN:
                    raise PublicationAssemblyError("edition_must_be_open")
                if edition.version != manifest.edition_version:
                    raise PublicationAssemblyError("edition_changed_after_publication_freeze")
            if _assert_manifest_inputs_current:
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
                if isinstance(manifest, PublicationManifestV2):
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
    expected_composition_version: int | None = None,
) -> _ResolvedPublicationInputs:
    composition = await uow.edition_compositions.get_for_edition(edition.id)
    if composition is None:
        raise PublicationAcceptanceError("edition_has_no_composition")
    if (
        expected_composition_version is not None
        and composition.version != expected_composition_version
    ):
        raise PublicationCompositionVersionConflict(
            expected_composition_version, composition.version
        )

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
    if review.composition_version != composition.version:
        raise PublicationCompositionChangedDuringFreeze()
    if validate_review:
        if any(
            entry.disposition is CompositionDisposition.DEFERRED for entry in composition.entries
        ) or any(
            diagnostic.code.value == "subject_unaddressed" for diagnostic in review.diagnostics
        ):
            raise PublicationAcceptanceError("composition_incomplete")

    review_by_subject = {item.subject_id: item for item in review.items}
    exclusion_by_subject = {item.subject_id: item for item in review.exclusions}
    entries: list[PublicationManifestEntryV2] = []
    exclusions: list[PublicationManifestExclusionV1] = []
    for composition_entry in composition.entries:
        if composition_entry.disposition is CompositionDisposition.INCLUDED:
            item = review_by_subject.get(composition_entry.subject_id)
            if item is None or item.document_artifact_id is None:
                raise PublicationArtifactChangedDuringFreeze()
            if (
                composition_entry.position != item.position
                or composition_entry.production_run_id != item.run_id
                or composition_entry.pipeline_generation != item.pipeline_generation
                or composition_entry.artifact_id != item.document_artifact_id
                or composition_entry.artifact_version != item.document_artifact_version
                or composition_entry.artifact_input_hash != item.document_input_hash
            ):
                raise PublicationArtifactChangedDuringFreeze()
            run = await _get_run_for_update(uow, item.run_id)
            artifact_repository = uow.production_artifacts
            artifact_getter = getattr(artifact_repository, "get_for_update", None)
            artifact = await (
                artifact_getter(item.document_artifact_id)
                if artifact_getter is not None
                else artifact_repository.get(item.document_artifact_id)
            )
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
                raise PublicationArtifactChangedDuringFreeze()
            if validate_review and (not item.included or item.blocking):
                raise PublicationAcceptanceError("review_cannot_be_accepted")
            blob_repository = getattr(uow, "blobs", None)
            blob = (
                await blob_repository.get(artifact.canonical_blob_id)
                if blob_repository is not None
                else None
            )
            document_sha256 = (
                blob.descriptor.sha256 if blob is not None else composition_entry.document_sha256
            )
            if (
                composition_entry.document_sha256 is not None
                and document_sha256 != composition_entry.document_sha256
            ):
                raise PublicationArtifactChangedDuringFreeze()
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
                PublicationManifestEntryV2(
                    position=item.position,
                    subject_id=item.subject_id,
                    production_run_id=item.run_id,
                    pipeline_generation=item.pipeline_generation,
                    document_artifact_id=artifact.id,
                    document_artifact_version=artifact.version,
                    document_input_hash=artifact.input_hash,
                    document_sha256=document_sha256,
                    provenance_batch_id=composition_entry.provenance_batch_id,
                    review_decision_id=item.effective_decision_id,
                )
            )
        elif composition_entry.disposition is CompositionDisposition.EXCLUDED:
            exclusion = exclusion_by_subject.get(composition_entry.subject_id)
            if (
                composition_entry.review_decision_id is None
                or exclusion is None
                or not exclusion.applicable
                or exclusion.decision_id != composition_entry.review_decision_id
            ):
                raise PublicationAcceptanceError("excluded_item_has_no_decision")
            exclusions.append(
                PublicationManifestExclusionV1(
                    subject_id=composition_entry.subject_id,
                    review_decision_id=composition_entry.review_decision_id,
                )
            )

    if validate_review and not review.can_accept:
        raise PublicationAcceptanceError("review_cannot_be_accepted")
    if not entries:
        raise PublicationAcceptanceError("review_must_include_at_least_one_item")
    latest_composition = await uow.edition_compositions.get_for_edition(edition.id)
    if (
        latest_composition is None
        or latest_composition.id != composition.id
        or latest_composition.version != composition.version
    ):
        raise PublicationCompositionChangedDuringFreeze()
    return _ResolvedPublicationInputs(
        edition_version=edition.version,
        composition_id=composition.id,
        composition_version=composition.version,
        entries=tuple(sorted(entries, key=lambda item: item.position)),
        exclusions=tuple(sorted(exclusions, key=lambda item: str(item.subject_id))),
    )


def _publication_inputs_match(
    manifest: PublicationManifest, inputs: _ResolvedPublicationInputs
) -> bool:
    if not isinstance(manifest, PublicationManifestV2):
        return False
    return (
        manifest.edition_version == inputs.edition_version
        and manifest.composition_id == inputs.composition_id
        and manifest.composition_version == inputs.composition_version
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
