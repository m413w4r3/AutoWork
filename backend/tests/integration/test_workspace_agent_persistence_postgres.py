"""PostgreSQL oracles for the canonical workspace binding and ingest rows.

Runs against a disposable database migrated to head. Every test creates its own
investigation chain and workspace id, so the session-scoped database is shared
without collisions.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.workspace_agent import (
    EventsPageV1,
    EventV1,
    FileStateV1,
    SnapshotPageV1,
    WorkspaceBindingV1,
    WorkspaceFileEventV1,
    WorkspaceFileRevisionV1,
    WorkspaceIngestError,
    WorkspaceIngestService,
    WorkspaceSyncCursorV1,
    workspace_revision_id,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SHA_A = "a" * 64
SHA_B = "b" * 64


@pytest.fixture
async def session_factory(
    migrated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _uow_factory(
    session_factory: async_sessionmaker[AsyncSession],
) -> SqlAlchemyUnitOfWork:
    return SqlAlchemyUnitOfWork(session_factory)


async def make_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    from tests.integration.edition_codes import reserve_edition_code

    suffix = uuid4().hex
    edition = Edition(
        country=f"Workspace binding {suffix}",
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
            title="Workspace binding fixture",
            slug=f"workspace-binding-{suffix}",
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


def binding_for(investigation: AnalystInvestigation) -> WorkspaceBindingV1:
    return WorkspaceBindingV1(
        workspace_id="ws-" + uuid4().hex,
        investigation_id=investigation.id,
        subject_id=investigation.subject_id,
        delivery_code="IR2609",
        article_slug="bluemoon-cn",
        synthesis_sha256=SHA_A,
        created_at=NOW,
    )


def revision_for(
    workspace_id: str, *, path: str = "notes/humaines/note.md", sha256: str = SHA_B
) -> WorkspaceFileRevisionV1:
    return WorkspaceFileRevisionV1(
        workspace_id=workspace_id,
        revision_id=workspace_revision_id(workspace_id=workspace_id, path=path, sha256=sha256),
        path=path,
        sha256=sha256,
        size=12,
        mode="0640",
        mtime_ns=1_700_000_000_000_000_000,
        observed_at=NOW,
        origin="ui",
        task_id=None,
        supersedes_revision_id=None,
    )


def event_for(
    workspace_id: str,
    *,
    seq: int,
    generation: int = 1,
    path: str = "notes/humaines/note.md",
    sha256: str = SHA_B,
) -> WorkspaceFileEventV1:
    return WorkspaceFileEventV1(
        workspace_id=workspace_id,
        event_id="evt-" + uuid4().hex,
        seq=seq,
        generation=generation,
        kind="file_created",
        path=path,
        sha256=sha256,
        size=12,
        origin="filesystem",
        observed_at=NOW,
        policy_class="human_editable",
        revision_id=workspace_revision_id(workspace_id=workspace_id, path=path, sha256=sha256),
    )


def wire_event(event: WorkspaceFileEventV1) -> EventV1:
    return EventV1.model_validate(event.model_dump(mode="json", include=set(EventV1.model_fields)))


async def _bound_workspace(
    session_factory: async_sessionmaker[AsyncSession],
) -> WorkspaceBindingV1:
    investigation = await make_investigation(session_factory)
    binding = binding_for(investigation)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored, created = await uow.workspace_bindings.add_if_absent(binding)
        assert created
        assert stored == binding
        await uow.commit()
    return binding


async def test_workspace_repositories_roundtrip_and_idempotent_append(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    investigation = await make_investigation(session_factory)
    binding = binding_for(investigation)
    revision = revision_for(binding.workspace_id)
    event = event_for(binding.workspace_id, seq=1)

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored, created = await uow.workspace_bindings.add_if_absent(binding)
        assert created is True
        assert stored == binding
        replayed, created_again = await uow.workspace_bindings.add_if_absent(binding)
        assert created_again is False
        assert replayed == binding
        assert await uow.workspace_bindings.get(binding.workspace_id) == binding
        assert await uow.workspace_bindings.get_for_investigation(investigation.id) == binding
        stored_revision, created_revision = await uow.workspace_file_revisions.append_if_absent(
            revision
        )
        assert created_revision is True
        assert stored_revision == revision
        assert (await uow.workspace_file_revisions.append_if_absent(revision)) == (revision, False)
        stored_event, created_event = await uow.workspace_file_events.append_if_absent(event)
        assert created_event is True
        assert stored_event == event
        assert await uow.workspace_file_events.append_if_absent(event) == (event, False)
        await uow.commit()

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert (
            await uow.workspace_file_revisions.get(binding.workspace_id, revision.revision_id)
            == revision
        )
        assert await uow.workspace_file_revisions.list_for_workspace(binding.workspace_id) == (
            revision,
        )
        assert await uow.workspace_file_events.get(binding.workspace_id, event.event_id) == event
        assert await uow.workspace_file_events.list_for_workspace(binding.workspace_id) == (event,)


async def test_workspace_revisions_unique_per_file(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    binding = await _bound_workspace(session_factory)
    revision = revision_for(binding.workspace_id, path="notes/humaines/note.md")
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        await uow.workspace_file_revisions.append_if_absent(revision)
        tampered = revision.model_copy(update={"path": "notes/humaines/other.md"})
        with pytest.raises(ValueError, match="different workspace file"):
            await uow.workspace_file_revisions.append_if_absent(tampered)
        await uow.commit()
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.workspace_file_revisions.list_for_workspace(binding.workspace_id) == (
            revision,
        )


async def test_workspace_events_unique_sequence_per_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    binding = await _bound_workspace(session_factory)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        first = event_for(binding.workspace_id, seq=1)
        await uow.workspace_file_events.append_if_absent(first)
        with pytest.raises(ValueError, match="event sequence"):
            await uow.workspace_file_events.append_if_absent(event_for(binding.workspace_id, seq=1))
        await uow.commit()


async def test_workspace_binding_is_unique_per_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    investigation = await make_investigation(session_factory)
    first = binding_for(investigation)
    second = binding_for(investigation)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        stored, created = await uow.workspace_bindings.add_if_absent(first)
        assert created is True
        with pytest.raises(ValueError, match="bind only one workspace"):
            await uow.workspace_bindings.add_if_absent(second)
        await uow.commit()
    assert stored == first


async def test_workspace_cursor_advances_only_forward_per_generation(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    binding = await _bound_workspace(session_factory)
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        first = await uow.workspace_sync_cursors.save(
            WorkspaceSyncCursorV1(
                workspace_id=binding.workspace_id,
                generation=1,
                cursor=5,
                last_scan_at=NOW,
            )
        )
        assert first.cursor == 5
        lower = await uow.workspace_sync_cursors.save(
            WorkspaceSyncCursorV1(
                workspace_id=binding.workspace_id,
                generation=1,
                cursor=3,
                last_scan_at=NOW,
            )
        )
        assert lower.cursor == 5
        higher = await uow.workspace_sync_cursors.save(
            WorkspaceSyncCursorV1(
                workspace_id=binding.workspace_id,
                generation=1,
                cursor=7,
                last_scan_at=NOW,
            )
        )
        assert higher.cursor == 7
        new_generation = await uow.workspace_sync_cursors.save(
            WorkspaceSyncCursorV1(
                workspace_id=binding.workspace_id,
                generation=2,
                cursor=1,
                last_scan_at=NOW,
            )
        )
        assert new_generation.generation == 2
        assert new_generation.cursor == 1
        await uow.commit()


async def test_workspace_ingest_one_transaction_per_page_and_resync(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    binding = await _bound_workspace(session_factory)
    service = WorkspaceIngestService(lambda: SqlAlchemyUnitOfWork(session_factory))
    first = event_for(binding.workspace_id, seq=1)
    second = event_for(binding.workspace_id, seq=2)
    page = EventsPageV1(
        generation=1,
        events=[wire_event(first), wire_event(second)],
        next_cursor=2,
        oldest_retained=1,
        resync_required=False,
    )

    inserted = await service.ingest_events_page(
        workspace_id=binding.workspace_id, page=page, observed_at=NOW
    )
    assert inserted.inserted_events == 2
    assert inserted.existing_events == 0
    assert inserted.cursor == 2

    replay = await service.ingest_events_page(
        workspace_id=binding.workspace_id, page=page, observed_at=NOW
    )
    assert replay.inserted_events == 0
    assert replay.existing_events == 2
    assert replay.cursor == 2

    stale = EventsPageV1(
        generation=1,
        events=[],
        next_cursor=1,
        oldest_retained=1,
        resync_required=False,
    )
    lower = await service.ingest_events_page(
        workspace_id=binding.workspace_id, page=stale, observed_at=NOW
    )
    assert lower.cursor == 2

    next_generation = EventsPageV1(
        generation=2,
        events=[wire_event(event_for(binding.workspace_id, seq=1, generation=2))],
        next_cursor=5,
        oldest_retained=5,
        resync_required=False,
    )
    resynced = await service.ingest_events_page(
        workspace_id=binding.workspace_id, page=next_generation, observed_at=NOW
    )
    assert resynced.resync_required is True
    assert resynced.generation == 2
    assert resynced.cursor == 5

    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert len(await uow.workspace_file_events.list_for_workspace(binding.workspace_id)) == 3
        cursor = await uow.workspace_sync_cursors.get(binding.workspace_id)
        assert cursor is not None
        assert (cursor.generation, cursor.cursor) == (2, 5)


async def test_workspace_ingest_rejects_tampered_revision_identity(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    binding = await _bound_workspace(session_factory)
    service = WorkspaceIngestService(lambda: SqlAlchemyUnitOfWork(session_factory))
    valid = event_for(binding.workspace_id, seq=1)
    tampered = event_for(binding.workspace_id, seq=2).model_copy(
        update={"revision_id": "rev-" + "0" * 32}
    )
    page = EventsPageV1(
        generation=1,
        events=[wire_event(valid), wire_event(tampered)],
        next_cursor=2,
        oldest_retained=1,
        resync_required=False,
    )
    with pytest.raises(WorkspaceIngestError, match="revision identity"):
        await service.ingest_events_page(
            workspace_id=binding.workspace_id, page=page, observed_at=NOW
        )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.workspace_file_events.list_for_workspace(binding.workspace_id) == ()
        assert await uow.workspace_sync_cursors.get(binding.workspace_id) is None


async def test_workspace_ingest_requires_a_canonical_binding(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    service = WorkspaceIngestService(lambda: SqlAlchemyUnitOfWork(session_factory))
    page = EventsPageV1(
        generation=1,
        events=[],
        next_cursor=0,
        oldest_retained=0,
        resync_required=False,
    )
    with pytest.raises(WorkspaceIngestError, match="not bound"):
        await service.ingest_events_page(
            workspace_id="ws-" + uuid4().hex, page=page, observed_at=NOW
        )


async def test_workspace_snapshot_ingest_inserts_revisions_once(
    session_factory: async_sessionmaker[AsyncSession],
) -> None:
    binding = await _bound_workspace(session_factory)
    service = WorkspaceIngestService(lambda: SqlAlchemyUnitOfWork(session_factory))
    revision = revision_for(binding.workspace_id, path="scripts/tool.py")
    metadata_only = FileStateV1(
        path="pcaps/large.pcap",
        revision_id=None,
        sha256=None,
        size=64 * 1024 * 1024,
        mode="0640",
        mtime_ns=1_700_000_000_000_000_000,
        origin="filesystem",
        observed_at=NOW,
        policy_class="generated_immutable",
        task_id=None,
    )
    page = SnapshotPageV1(
        generation=4,
        files=[
            FileStateV1(
                path=revision.path,
                revision_id=revision.revision_id,
                sha256=revision.sha256,
                size=revision.size,
                mode=revision.mode,
                mtime_ns=revision.mtime_ns,
                origin=revision.origin,
                observed_at=revision.observed_at,
                policy_class="human_editable",
                task_id=None,
            ),
            metadata_only,
        ],
        next_page_token=None,
    )
    first = await service.ingest_snapshot_page(workspace_id=binding.workspace_id, page=page)
    assert first.inserted_revisions == 1
    replay = await service.ingest_snapshot_page(workspace_id=binding.workspace_id, page=page)
    assert replay.inserted_revisions == 0
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.workspace_file_revisions.list_for_workspace(binding.workspace_id) == (
            revision,
        )
