from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from types import TracebackType
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_composition import (
    CompositionReferenceSnapshot,
    CompositionReferenceState,
    CompositionSubjectPinStatus,
    CompositionUnitOfWork,
    EditionCompositionEventRepository,
    EditionCompositionRepository,
    EditionCompositionService,
    project_subject_composition_pin_status,
)
from cti_app.application.editorial_history import EditorialHistoryService
from cti_app.application.editorial_pdf_preview import (
    EditorialPdfRegenerationError,
    require_explicit_publication_kind,
)
from cti_app.domain.classification import TLP
from cti_app.domain.edition_composition import (
    CompositionDisposition,
    EditionComposition,
    EditionCompositionEntry,
)
from cti_app.domain.editions import Edition
from cti_app.domain.editorial_proposal import (
    EditorialProposal,
    EditorialProposalOrigin,
    EditorialProposalState,
)
from cti_app.domain.editorial_proposal_impact import (
    EditorialPdfPreviewStatus,
    project_editorial_pdf_preview,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionRunStatus,
)
from cti_app.domain.publication_kind import PublicationKind
from cti_app.domain.publication_render import PublicationRender
from cti_app.domain.typst_render import TypstRenderFormat, TypstRenderStatus


def _artifact(*, version: int) -> ProductionArtifact:
    return ProductionArtifact(
        id=uuid4(),
        production_run_id=uuid4(),
        subject_id=uuid4(),
        stage=ProductionArtifactStage.PUBLICATION,
        version=version,
        status=ProductionArtifactStatus.VERIFIED,
        input_hash=f"{version:064x}",
        canonical_blob_id=uuid4(),
    )


def _render(artifact: ProductionArtifact, *, status: TypstRenderStatus) -> PublicationRender:
    return PublicationRender(
        id=uuid4(),
        publication_artifact_id=artifact.id,
        renderer="typst",
        renderer_version="1",
        template_version="1",
        template_sha256="a" * 64,
        compiler="typst",
        compiler_version="1",
        font_bundle_version="1",
        render_policy_version="1",
        format=TypstRenderFormat.PDF,
        input_hash="b" * 64,
        source_blob_id=None,
        render_data_blob_id=None,
        output_blob_id=uuid4() if status is TypstRenderStatus.SUCCEEDED else None,
        output_sha256="c" * 64 if status is TypstRenderStatus.SUCCEEDED else None,
        output_byte_size=32 if status is TypstRenderStatus.SUCCEEDED else None,
        status=status,
        error_code="typst_binary_unavailable" if status is TypstRenderStatus.FAILED else None,
        error_message="Typst binary is unavailable" if status is TypstRenderStatus.FAILED else None,
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )


def test_acceptance_marks_pdf_stale_then_rendered_pdf_matches_current_pins() -> None:
    previous = _artifact(version=1)
    current = _artifact(version=2)
    previous_render = _render(previous, status=TypstRenderStatus.SUCCEEDED)
    current_render = _render(current, status=TypstRenderStatus.SUCCEEDED)

    stale = project_editorial_pdf_preview(
        subject_id=current.subject_id,
        current_artifact=current,
        current_canonical_sha256="d" * 64,
        render=previous_render,
        publication_stale=True,
    )
    assert stale.status is EditorialPdfPreviewStatus.STALE
    assert stale.artifact_id == current.id
    assert stale.artifact_version == current.version
    assert stale.pdf_url is None
    assert stale.render_id is None

    ready = project_editorial_pdf_preview(
        subject_id=current.subject_id,
        current_artifact=current,
        current_canonical_sha256="d" * 64,
        render=current_render,
        publication_stale=False,
    )
    assert ready.status is EditorialPdfPreviewStatus.READY
    assert ready.artifact_id == current.id
    assert ready.artifact_version == current.version
    assert ready.render_id == current_render.id
    assert ready.render_identity == current_render.input_hash
    assert ready.pdf_url == (
        f"/api/subjects/{current.subject_id}/publication/preview/{current.id}/pdf"
        f"?render_identity={current_render.input_hash}"
    )


def test_pdf_preview_reports_generating_and_actionable_french_failure() -> None:
    artifact = _artifact(version=1)
    running = project_editorial_pdf_preview(
        subject_id=artifact.subject_id,
        current_artifact=artifact,
        current_canonical_sha256="d" * 64,
        render=_render(artifact, status=TypstRenderStatus.RUNNING),
        publication_stale=False,
    )
    assert running.status is EditorialPdfPreviewStatus.GENERATING

    failed = project_editorial_pdf_preview(
        subject_id=artifact.subject_id,
        current_artifact=artifact,
        current_canonical_sha256="d" * 64,
        render=_render(artifact, status=TypstRenderStatus.FAILED),
        publication_stale=False,
    )
    assert failed.status is EditorialPdfPreviewStatus.FAILED
    assert failed.error_code == "typst_binary_unavailable"
    assert failed.error_message
    assert any(
        word in failed.error_message.lower() for word in ("typst", "réessayez", "configuration")
    )


class _CompositionRepository:
    def __init__(
        self, composition: EditionComposition, reference: CompositionReferenceState
    ) -> None:
        self.composition = composition
        self.reference = reference

    async def get_for_edition(self, _edition_id: UUID) -> EditionComposition | None:
        return self.composition

    async def get_for_edition_for_update(self, _edition_id: UUID) -> EditionComposition | None:
        return self.composition

    async def save(self, _composition: EditionComposition) -> None:
        raise AssertionError("composition status reads must not write")

    async def replace_entries(
        self, _composition_id: UUID, _entries: Sequence[EditionCompositionEntry]
    ) -> None:
        raise AssertionError("composition status reads must not write")

    async def list_subject_ids(self, _edition_id: UUID) -> tuple[UUID, ...]:
        return (self.composition.entries[0].subject_id,)

    async def validate_references(
        self, _edition_id: UUID, _entries: Sequence[EditionCompositionEntry], *, lock: bool
    ) -> CompositionReferenceSnapshot:
        assert lock is False
        subject_id = self.composition.entries[0].subject_id
        return CompositionReferenceSnapshot(
            subject_ids=frozenset({subject_id}), entries={subject_id: self.reference}
        )


class _CompositionEvents:
    async def get_by_idempotency_key(self, _composition_id: UUID, _idempotency_key: str) -> None:
        return None

    async def append(self, _event: object) -> None:
        raise AssertionError("composition status reads must not write")


class _CompositionUow:
    def __init__(self, edition: Edition, repository: _CompositionRepository) -> None:
        self.editions = self
        self.edition_compositions: EditionCompositionRepository = repository
        self.edition_composition_events: EditionCompositionEventRepository = _CompositionEvents()
        self.production_runs = object()
        self.production_artifacts = object()
        self.publication_review_decisions = object()
        self.edition = edition

    async def get(self, _edition_id: UUID) -> Edition | None:
        return self.edition

    async def __aenter__(self) -> CompositionUnitOfWork:
        return self

    async def __aexit__(
        self,
        _exc_type: type[BaseException] | None,
        _exc_value: BaseException | None,
        _traceback: TracebackType | None,
    ) -> None:
        return None

    async def commit(self) -> None:
        raise AssertionError("composition status reads must not write")


@pytest.mark.asyncio
async def test_edition_composition_detects_stale_without_auto_repin() -> None:
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=datetime(2099, 1, 1).date(),
        period_end=datetime(2099, 1, 31).date(),
        tlp=TLP.GREEN,
        languages=("fr",),
    )
    subject_id = uuid4()
    entry = EditionCompositionEntry(
        subject_id=subject_id,
        disposition=CompositionDisposition.INCLUDED,
        position=1,
        production_run_id=uuid4(),
        pipeline_generation=1,
        artifact_id=uuid4(),
        artifact_version=1,
        artifact_input_hash="a" * 64,
    )
    composition = EditionComposition(
        edition_id=edition.id,
        version=1,
        actor_id="dev-analyst",
        entries=(entry,),
    )
    repository = _CompositionRepository(
        composition,
        CompositionReferenceState(
            subject_exists=True,
            run_exists=True,
            run_belongs_to_edition=True,
            run_subject_matches=True,
            run_pipeline_generation=1,
            run_status=ProductionRunStatus.READY,
            artifact_exists=True,
            artifact_run_matches=True,
            artifact_subject_matches=True,
            artifact_stage="publication",
            artifact_status=ProductionArtifactStatus.STALE,
            artifact_version=1,
            artifact_input_hash="a" * 64,
            latest_artifact_version=2,
        ),
    )
    service = EditionCompositionService(lambda: _CompositionUow(edition, repository))

    read = await service.get(edition.id)
    status = project_subject_composition_pin_status(
        read,
        subject_id,
        composition_url=f"/api/editions/{edition.id}/composition",
    )

    assert isinstance(status, CompositionSubjectPinStatus)
    assert status.status == "stale"
    assert status.message == "Composition à mettre à jour"
    assert status.pinned_artifact_id == entry.artifact_id
    assert status.pinned_artifact_version == 1
    assert status.current_artifact_version == 2
    assert repository.composition.entries == (entry,)


def _proposal(
    *,
    subject_id: UUID,
    occurrence_id: UUID,
    origin: EditorialProposalOrigin,
    state: EditorialProposalState,
    created_at: datetime,
) -> EditorialProposal:
    is_refusal = state is EditorialProposalState.REFUSED
    return EditorialProposal(
        id=uuid4(),
        subject_id=subject_id,
        production_run_id=uuid4(),
        origin=origin,
        snapshot_id=uuid4(),
        occurrence_id=occurrence_id,
        base_artifact_id=uuid4(),
        base_artifact_version=1,
        base_input_hash="a" * 64,
        base_canonical_sha256="b" * 64,
        base_routing_hash=None,
        base_pin_hash="c" * 64,
        target_id="target",
        operation="rewrite_paragraph",
        before_hash="d" * 64,
        patch_blob_ref=None if is_refusal else uuid4(),
        patch_sha256=None if is_refusal else "e" * 64,
        evidence_refs=(),
        diagnostics={},
        thread_id=uuid4() if origin is EditorialProposalOrigin.BRIDGE_DRAFT else None,
        variant_group_id=uuid4(),
        model_run_id=uuid4() if origin is EditorialProposalOrigin.BRIDGE_DRAFT else None,
        model_turn_id=uuid4() if origin is EditorialProposalOrigin.BRIDGE_DRAFT else None,
        state=state,
        state_version=1,
        actor_id=uuid4(),
        idempotency_key=str(uuid4()),
        request_sha256="f" * 64,
        created_at=created_at,
        updated_at=created_at + timedelta(minutes=1),
    )


class _HistoryRepository:
    def __init__(self, proposals: tuple[EditorialProposal, ...]) -> None:
        self.proposals = proposals
        self.requested_subjects: list[UUID] = []
        self.requested_limits: list[int] = []

    async def list_for_subject(
        self,
        subject_id: UUID,
        *,
        occurrence_id: UUID | None,
        before: tuple[datetime, UUID] | None,
        limit: int,
    ) -> tuple[EditorialProposal, ...]:
        self.requested_subjects.append(subject_id)
        self.requested_limits.append(limit)
        values = [item for item in self.proposals if item.subject_id == subject_id]
        if occurrence_id is not None:
            values = [item for item in values if item.occurrence_id == occurrence_id]
        if before is not None:
            values = [
                item
                for item in values
                if (item.created_at, item.id.int) < (before[0], before[1].int)
            ]
        values.sort(key=lambda item: (item.created_at, item.id.int), reverse=True)
        return tuple(values[:limit])


class _HistoryUow:
    def __init__(self, subject_id: UUID, repository: _HistoryRepository) -> None:
        self.subjects = self
        self.editorial_proposals = repository
        self.subject_id = subject_id

    async def get(self, subject_id: UUID) -> object | None:
        return object() if subject_id == self.subject_id else None

    async def __aenter__(self) -> _HistoryUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_history_lists_manual_and_bridge_origin_after_reload() -> None:
    subject_id, occurrence_id = uuid4(), uuid4()
    instant = datetime(2026, 11, 21, tzinfo=UTC)
    repository = _HistoryRepository(
        (
            _proposal(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                origin=EditorialProposalOrigin.MANUAL_PATCH,
                state=EditorialProposalState.ACCEPTED,
                created_at=instant,
            ),
            _proposal(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                origin=EditorialProposalOrigin.BRIDGE_DRAFT,
                state=EditorialProposalState.REFUSED,
                created_at=instant + timedelta(minutes=2),
            ),
        )
    )

    first_reader = EditorialHistoryService(lambda: _HistoryUow(subject_id, repository))
    page = await first_reader.read(subject_id, occurrence_id=occurrence_id, limit=2)
    reloaded_reader = EditorialHistoryService(lambda: _HistoryUow(subject_id, repository))
    reloaded = await reloaded_reader.read(subject_id, occurrence_id=occurrence_id, limit=2)

    assert [item.origin for item in page.items] == ["bridge_draft", "manual_patch"]
    assert [item.origin for item in reloaded.items] == ["bridge_draft", "manual_patch"]
    assert page.items[0].thread_url == (
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}"
    )
    assert page.items[1].canonical_result_version is None


@pytest.mark.parametrize("kind", (PublicationKind.ARTICLE, PublicationKind.BRIEF))
def test_article_and_brief_pdf_use_explicit_kind(kind: PublicationKind) -> None:
    assert require_explicit_publication_kind(kind) is kind

    with pytest.raises(EditorialPdfRegenerationError, match="publication_kind_unresolved"):
        require_explicit_publication_kind(None)


@pytest.mark.asyncio
async def test_history_pagination_bounded_and_subject_scoped() -> None:
    subject_id, other_subject_id, occurrence_id = uuid4(), uuid4(), uuid4()
    instant = datetime(2026, 11, 21, tzinfo=UTC)
    proposals = (
        *(
            _proposal(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                origin=EditorialProposalOrigin.MANUAL_PATCH,
                state=EditorialProposalState.REFUSED,
                created_at=instant + timedelta(minutes=index),
            )
            for index in range(5)
        ),
        _proposal(
            subject_id=other_subject_id,
            occurrence_id=occurrence_id,
            origin=EditorialProposalOrigin.MANUAL_PATCH,
            state=EditorialProposalState.REFUSED,
            created_at=instant + timedelta(minutes=20),
        ),
    )
    repository = _HistoryRepository(proposals)
    service = EditorialHistoryService(lambda: _HistoryUow(subject_id, repository))

    first = await service.read(subject_id, limit=2)
    second = await service.read(subject_id, cursor=first.next_cursor, limit=2)

    assert len(first.items) == 2
    assert len(second.items) == 2
    assert first.next_cursor is not None
    assert all(item.subject_id == subject_id for item in (*first.items, *second.items))
    assert repository.requested_subjects == [subject_id, subject_id]
    assert repository.requested_limits == [3, 3]
