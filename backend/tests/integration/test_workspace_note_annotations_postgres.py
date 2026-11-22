"""PostgreSQL oracle for the M24-WS2 note annotation layer.

One disposable migrated database: a real investigation, a real workspace
binding, two per-file note revisions and the real append-only repository.  The
service links a note revision to a task it creates through the 04B engine,
writes an explicit human attribution, rebuilds the note view and replays the
same idempotency key.  UPDATE, DELETE and TRUNCATE are refused by the
migration's triggers, and a downgrade with annotation rows is refused on a
second disposable database.
"""

from __future__ import annotations

import asyncio
import hashlib
from datetime import UTC, date, datetime
from typing import Any
from uuid import uuid4

import pytest
import sqlalchemy as sa
from alembic import command
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceFileRevisionV1,
    workspace_revision_id,
)
from cti_app.application.workspace_notes import (
    NoteError,
    NoteErrorCode,
    NotePassageRangeV1,
    WorkspaceNoteService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation_task import TaskStatus
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.domain.workspace_note_annotation import NoteAnnotationKind
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
NOTE_PATH = "notes/humaines/hypothese.md"


async def _make_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Note annotation fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Note annotation fixture",
            slug=f"note-annotation-fixture-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="d" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


def _revision(
    workspace_id: str, path: str, content: bytes, *, minute: int
) -> WorkspaceFileRevisionV1:
    sha256 = hashlib.sha256(content).hexdigest()
    return WorkspaceFileRevisionV1(
        workspace_id=workspace_id,
        revision_id=workspace_revision_id(workspace_id=workspace_id, path=path, sha256=sha256),
        path=path,
        sha256=sha256,
        size=len(content),
        mode="0640",
        mtime_ns=1_700_000_000_000_000_000,
        observed_at=NOW.replace(minute=minute),
        origin="filesystem",
    )


async def _assert_append_only(
    engine: Any, statement: sa.TextClause, parameters: dict[str, object]
) -> None:
    with pytest.raises(DBAPIError, match="append-only"):
        async with engine.begin() as connection:
            await connection.execute(statement, parameters)


async def _seed_workspace(
    session_factory: async_sessionmaker[AsyncSession],
    investigation: AnalystInvestigation,
) -> tuple[str, WorkspaceFileRevisionV1, WorkspaceFileRevisionV1]:
    workspace_id = "ws-" + uuid4().hex
    first = _revision(workspace_id, NOTE_PATH, b"# Hypothese v1\n", minute=0)
    second = _revision(
        workspace_id,
        NOTE_PATH,
        b"# Hypothese v2\n\nLe chargeur ouvrirait une URL.\n",
        minute=5,
    )
    binding = WorkspaceBindingV1(
        workspace_id=workspace_id,
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        delivery_code="IR202611",
        article_slug="note-annotation-fixture",
        synthesis_sha256="a" * 64,
        created_at=NOW,
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.workspace_bindings.add_if_absent(binding)
        await uow.workspace_file_revisions.append_if_absent(first)
        await uow.workspace_file_revisions.append_if_absent(second)
        await uow.commit()
    return workspace_id, first, second


async def test_note_annotation_repository_view_and_append_only_triggers(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        investigation = await _make_investigation(session_factory)
        _workspace_id, first, second = await _seed_workspace(session_factory, investigation)
        service = WorkspaceNoteService(uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory))

        verification = await service.propose_verification_task(
            investigation.id,
            second.revision_id,
            passage=NotePassageRangeV1(start=0, end=18),
            question="L'URL est-elle réellement contactée ?",
            actor_id="analyst:pg-human",
            idempotency_key="pg-note-verification-0001",
        )
        assert verification.task.status is TaskStatus.PROPOSED
        assert verification.task.created_by.actor_id == "analyst:pg-human"

        linked = await service.annotate(
            investigation.id,
            second.revision_id,
            kind=NoteAnnotationKind.LINK_TASK,
            task_id=verification.task.id,
            actor_id="analyst:pg-human",
            idempotency_key="pg-note-link-task-0001",
        )
        replay = await service.annotate(
            investigation.id,
            second.revision_id,
            kind=NoteAnnotationKind.LINK_TASK,
            task_id=verification.task.id,
            actor_id="analyst:pg-human",
            idempotency_key="pg-note-link-task-0001",
        )
        assert replay.id == linked.id
        with pytest.raises(NoteError) as conflict:
            await service.annotate(
                investigation.id,
                second.revision_id,
                kind=NoteAnnotationKind.LINK_TASK,
                task_id=verification.task.id,
                actor_id="analyst:another-human",
                idempotency_key="pg-note-link-task-0001",
            )
        assert conflict.value.code is NoteErrorCode.IDEMPOTENCY_CONFLICT

        author = await service.annotate(
            investigation.id,
            first.revision_id,
            kind=NoteAnnotationKind.ATTRIBUTE_AUTHOR,
            detail="Analyste PG (attribution explicite)",
            actor_id="analyst:pg-human",
            idempotency_key="pg-note-author-0001",
        )
        assert author.file_relpath == NOTE_PATH

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            stored = await uow.workspace_note_annotations.get_by_idempotency(
                investigation.id, "pg-note-author-0001"
            )
            assert stored is not None and stored.id == author.id
            for_revision = await uow.workspace_note_annotations.list_for_revision(
                author.workspace_id, first.revision_id
            )
            assert [item.id for item in for_revision] == [author.id]

        view = await service.list_note_view(investigation.id, NOTE_PATH)
        assert view.latest.revision_id == second.revision_id
        assert [item.revision_id for item in view.versions] == [
            first.revision_id,
            second.revision_id,
        ]
        assert view.linked_task_ids == (verification.task.id,)
        assert view.attributed_authors == ("Analyste PG (attribution explicite)",)
        assert view.is_evidence is False
        assert view.unverified_hypothesis is True
        assert view.useful is False

        await _assert_append_only(
            engine,
            sa.text("UPDATE workspace_note_annotations SET actor_id = 'x' WHERE id = :id"),
            {"id": linked.id},
        )
        await _assert_append_only(
            engine,
            sa.text("DELETE FROM workspace_note_annotations WHERE id = :id"),
            {"id": linked.id},
        )
        await _assert_append_only(engine, sa.text("TRUNCATE workspace_note_annotations"), {})
    finally:
        await engine.dispose()


def test_note_annotation_downgrade_is_guarded(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")
    engine = create_postgres_engine(temporary_postgres_url)
    session_factory = create_session_factory(engine)

    async def seed() -> None:
        try:
            investigation = await _make_investigation(session_factory)
            _workspace_id, first, _second = await _seed_workspace(session_factory, investigation)
            service = WorkspaceNoteService(
                uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory)
            )
            await service.annotate(
                investigation.id,
                first.revision_id,
                kind=NoteAnnotationKind.MARK_USEFUL,
                actor_id="analyst:pg-human",
                idempotency_key="pg-note-downgrade-0001",
            )
        finally:
            await engine.dispose()

    asyncio.run(seed())

    with pytest.raises(RuntimeError, match="refusing to downgrade"):
        command.downgrade(config, "0025_opinion_note_proposals")
