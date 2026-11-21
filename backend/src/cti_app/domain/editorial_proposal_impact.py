"""Versioned outbox identity for work derived from an adopted proposal."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from uuid import UUID

from cti_app.domain.production import ProductionArtifact
from cti_app.domain.publication_render import PublicationPreviewStatus, PublicationRender
from cti_app.domain.typst_render import TypstRenderStatus


class EditorialProposalImpactState(StrEnum):
    PENDING = "pending"
    RUNNING = "running"
    DONE = "done"
    FAILED = "failed"
    SUPERSEDED = "superseded"


class EditorialWorkspaceImpactStatus(StrEnum):
    NONE = "none"
    PENDING = "pending"
    RUNNING = "running"
    SUPERSEDED = "superseded"
    UP_TO_DATE = "up_to_date"
    FAILED = "failed"


class EditorialPdfStatus(StrEnum):
    CURRENT = "CURRENT"
    STALE = "STALE"


class EditorialPdfPreviewStatus(StrEnum):
    READY = "READY"
    STALE = "STALE"
    GENERATING = "GENERATING"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class EditorialPdfPreview:
    status: EditorialPdfPreviewStatus
    artifact_id: UUID | None
    artifact_version: int | None
    artifact_input_hash: str | None
    artifact_canonical_sha256: str | None
    render_id: UUID | None
    render_identity: str | None
    error_code: str | None
    error_message: str | None
    pdf_url: str | None


def project_editorial_pdf_preview(
    *,
    subject_id: UUID,
    current_artifact: ProductionArtifact | None,
    current_canonical_sha256: str | None,
    render: PublicationRender | None,
    publication_stale: bool,
    generation_error_code: str | None = None,
) -> EditorialPdfPreview:
    """Project only a PDF render whose immutable identity matches the current publication."""
    render_matches = (
        current_artifact is not None
        and render is not None
        and render.publication_artifact_id == current_artifact.id
    )
    artifact_is_current = current_artifact is not None and not publication_stale
    # Keep the latest canonical publication identity visible while its PDF is stale.
    # Only the render identity and URL are withheld until the artifact is current.
    artifact_id = current_artifact.id if current_artifact is not None else None
    artifact_version = current_artifact.version if current_artifact is not None else None
    artifact_input_hash = current_artifact.input_hash if current_artifact is not None else None
    artifact_sha = current_canonical_sha256 if current_artifact is not None else None

    if artifact_is_current and render is None and generation_error_code is not None:
        return EditorialPdfPreview(
            status=EditorialPdfPreviewStatus.FAILED,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            artifact_input_hash=artifact_input_hash,
            artifact_canonical_sha256=artifact_sha,
            render_id=None,
            render_identity=None,
            error_code=generation_error_code,
            error_message=(
                "La génération du PDF a échoué. Vérifiez la configuration Typst puis réessayez."
            ),
            pdf_url=None,
        )
    if not artifact_is_current or not render_matches or render is None:
        return EditorialPdfPreview(
            status=EditorialPdfPreviewStatus.STALE,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            artifact_input_hash=artifact_input_hash,
            artifact_canonical_sha256=artifact_sha,
            render_id=None,
            render_identity=None,
            error_code=None,
            error_message=None,
            pdf_url=None,
        )

    assert current_artifact is not None
    if render.status is TypstRenderStatus.RUNNING:
        return EditorialPdfPreview(
            status=EditorialPdfPreviewStatus.GENERATING,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            artifact_input_hash=artifact_input_hash,
            artifact_canonical_sha256=artifact_sha,
            render_id=render.id,
            render_identity=render.input_hash,
            error_code=None,
            error_message=None,
            pdf_url=None,
        )
    if render.status is TypstRenderStatus.FAILED:
        code = render.error_code or "publication_render_failed"
        messages = {
            "typst_binary_unavailable": (
                "Le moteur Typst est indisponible. Configurez TYPST_BINARY puis réessayez."
            ),
            "typst_template_invalid": (
                "Le modèle Typst est invalide. Vérifiez le modèle et le jeu de polices, "
                "puis réessayez."
            ),
        }
        return EditorialPdfPreview(
            status=EditorialPdfPreviewStatus.FAILED,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            artifact_input_hash=artifact_input_hash,
            artifact_canonical_sha256=artifact_sha,
            render_id=render.id,
            render_identity=render.input_hash,
            error_code=code,
            error_message=messages.get(
                code,
                "La génération du PDF a échoué. Vérifiez la configuration Typst puis réessayez.",
            ),
            pdf_url=None,
        )
    return EditorialPdfPreview(
        status=EditorialPdfPreviewStatus.READY,
        artifact_id=artifact_id,
        artifact_version=artifact_version,
        artifact_input_hash=artifact_input_hash,
        artifact_canonical_sha256=artifact_sha,
        render_id=render.id,
        render_identity=render.input_hash,
        error_code=None,
        error_message=None,
        pdf_url=(
            f"/api/subjects/{subject_id}/publication/preview/{current_artifact.id}/pdf"
            f"?render_identity={render.input_hash}"
        ),
    )


@dataclass(frozen=True, slots=True)
class EditorialProposalImpact:
    impact_id: UUID
    subject_id: UUID
    production_run_id: UUID
    proposal_id: UUID
    accepted_artifact_id: UUID
    accepted_snapshot_id: UUID
    base_pin_hash: str
    accepted_pin_hash: str
    sequence: int
    state: EditorialProposalImpactState = EditorialProposalImpactState.PENDING
    attempt_count: int = 0
    last_error_code: str | None = None
    available_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    claimed_at: datetime | None = None
    completed_at: datetime | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        if self.sequence < 1 or self.attempt_count < 0:
            raise ValueError("editorial_proposal_impact_counter_invalid")
        for value in (self.base_pin_hash, self.accepted_pin_hash):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError("editorial_proposal_impact_hash_invalid")
        if (
            self.state
            not in {
                EditorialProposalImpactState.PENDING,
                EditorialProposalImpactState.RUNNING,
            }
            and self.completed_at is None
        ):
            raise ValueError("editorial_proposal_impact_completion_missing")


def project_editorial_impact_status(
    impact: EditorialProposalImpact | None,
    *,
    render_status: PublicationPreviewStatus | EditorialPdfPreviewStatus | str | None,
    publication_stale: bool,
) -> tuple[EditorialWorkspaceImpactStatus, EditorialPdfPreviewStatus, EditorialPdfStatus]:
    """Combine the durable impact state with the real publication render state."""
    if impact is None:
        impact_status = EditorialWorkspaceImpactStatus.NONE
    elif impact.state is EditorialProposalImpactState.DONE:
        impact_status = EditorialWorkspaceImpactStatus.UP_TO_DATE
    else:
        impact_status = EditorialWorkspaceImpactStatus(impact.state.value)

    if publication_stale or render_status is None:
        preview_status = EditorialPdfPreviewStatus.STALE
    elif render_status in {PublicationPreviewStatus.IN_PROGRESS, "IN_PROGRESS", "GENERATING"}:
        preview_status = EditorialPdfPreviewStatus.GENERATING
    elif render_status in {PublicationPreviewStatus.READY, "READY"}:
        preview_status = EditorialPdfPreviewStatus.READY
    elif render_status in {PublicationPreviewStatus.FAILED, "FAILED"}:
        preview_status = EditorialPdfPreviewStatus.FAILED
    else:
        preview_status = EditorialPdfPreviewStatus.STALE

    pdf_status = (
        EditorialPdfStatus.CURRENT
        if preview_status is EditorialPdfPreviewStatus.READY
        else EditorialPdfStatus.STALE
    )
    return impact_status, preview_status, pdf_status
