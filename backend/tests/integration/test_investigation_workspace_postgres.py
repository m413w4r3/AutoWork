"""PostgreSQL end-to-end oracle for the M24-WS1 workspace integration chain.

Runs the real host sidecar on a tmpdir AF_UNIX socket, the real
``HttpWorkspaceAgentClient``, the real ``InvestigationWorkspaceService`` and
the real PostgreSQL Unit of Work migrated to head. Covers: canonical binding,
snapshot/event ingest, file view + verified content read, a UI write vs a
filesystem edit conflict, the human resolution decision row, and a generated
export. The synthesis port is a fake: a verified synthesis artifact row is not
the subject of this lot.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.investigation_bootstrap import (
    BootstrapVtCapabilities,
    InvestigationBootstrapService,
)
from cti_app.application.investigation_contracts import ArticleContextV1
from cti_app.application.investigation_workspace import (
    RESULTS_SUMMARY_PATH,
    InvestigationWorkspaceService,
    WorkspaceRevisionConflictError,
)
from cti_app.application.research_context_builder import ArticleSnapshot
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import AnalystDecisionType
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
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient
from tests.integration.edition_codes import reserve_edition_code
from tests.workspace_agent.ws1_support import FakeArticleSnapshots, FakeArtifactStore

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPOSITORY_ROOT))

from sidecars.workspace_agent.agent import AgentConfig, WorkspaceAgent  # noqa: E402
from sidecars.workspace_agent.auth import TOKEN_FILE_NAME  # noqa: E402

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SYNTHESIS_SHA256 = "a" * 64
NOTE_PATH = "notes/humaines/note.md"


@pytest.fixture
async def session_factory(
    migrated_postgres_url: str,
) -> AsyncIterator[async_sessionmaker[AsyncSession]]:
    engine = create_postgres_engine(migrated_postgres_url)
    try:
        yield create_session_factory(engine)
    finally:
        await engine.dispose()


def _uow_factory(session_factory: async_sessionmaker[AsyncSession]) -> Any:
    return lambda: SqlAlchemyUnitOfWork(session_factory)


@asynccontextmanager
async def _running_agent(tmp_path: Path) -> AsyncIterator[tuple[Any, Path]]:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = tmp_path / "run"
    agent = WorkspaceAgent(
        AgentConfig(
            root=root,
            socket_dir=runtime_dir,
            group=os.getgid(),
            poll_interval_ms=1000,
            stability_window_ms=400,
            log_level="WARNING",
        )
    )
    stop = threading.Event()
    thread = threading.Thread(target=agent.serve, args=(stop,), daemon=True)
    thread.start()
    try:
        ready = await asyncio.to_thread(agent.wait_ready, 15)
        assert ready, "workspace agent did not become ready"
        yield agent, root
    finally:
        stop.set()
        await asyncio.to_thread(thread.join, 15)


async def _seed_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Edition, Subject, ProductionRun, AnalystInvestigation]:
    suffix = uuid4().hex
    edition = Edition(
        country=f"WS1 integration {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="WS1 integration fixture",
        slug=f"ws1-{suffix[:12]}",
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
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return edition, subject, run, investigation


async def _settle_and_sync(service: InvestigationWorkspaceService, investigation_id: UUID) -> None:
    for _ in range(3):
        await asyncio.sleep(0.5)
        await service.sync_workspace(investigation_id)


async def test_workspace_chain_end_to_end_on_postgres(
    tmp_path: Path, session_factory: async_sessionmaker[AsyncSession]
) -> None:
    _edition, subject, run, investigation = await _seed_investigation(session_factory)
    snapshot = ArticleSnapshot(
        subject_id=subject.id,
        production_run_id=run.id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        synthesis_sha256=SYNTHESIS_SHA256,
        article=ArticleContextV1(
            short_summary="WS1 integration summary",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
    )
    article_snapshots = FakeArticleSnapshots(snapshot)
    artifacts = FakeArtifactStore()
    uow_factory = _uow_factory(session_factory)
    bootstrap = InvestigationBootstrapService(
        uow_factory=uow_factory,
        article_snapshots=article_snapshots,
        artifact_store=artifacts,
        capabilities=BootstrapVtCapabilities(),
    )

    async with _running_agent(tmp_path) as (agent, root):
        client = HttpWorkspaceAgentClient(
            socket_path=str(agent.socket_path),
            token_file=str(agent.config.socket_dir / TOKEN_FILE_NAME),
            timeout_seconds=30.0,
        )
        service = InvestigationWorkspaceService(
            uow_factory=uow_factory,
            workspace_agent=client,
            article_snapshots=article_snapshots,
            bootstrap=bootstrap,
        )
        try:
            ensured = await service.ensure_workspace(investigation.id)
            assert ensured.binding.delivery_code == "IR202611"
            assert ensured.binding.article_slug == subject.slug
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                binding = await uow.workspace_bindings.get_for_investigation(investigation.id)
            assert binding is not None
            assert binding.synthesis_sha256 == SYNTHESIS_SHA256

            note = root / "IR202611" / subject.slug / NOTE_PATH
            note.parent.mkdir(parents=True, exist_ok=True)
            note.write_text("note humaine sur disque", encoding="utf-8")
            await _settle_and_sync(service, investigation.id)

            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                revisions = await uow.workspace_file_revisions.list_for_workspace(
                    binding.workspace_id
                )
                events = await uow.workspace_file_events.list_for_workspace(binding.workspace_id)
                cursor = await uow.workspace_sync_cursors.get(binding.workspace_id)
            assert any(item.path == NOTE_PATH for item in revisions)
            assert events
            assert cursor is not None and cursor.cursor >= 1

            listing = await service.list_files(investigation.id)
            view = next(item for item in listing.files if item.path == NOTE_PATH)
            assert view.origin == "filesystem"
            content = await service.read_file(investigation.id, path=NOTE_PATH)
            assert content.text == "note humaine sur disque"

            first = await service.write_note(
                investigation.id,
                path=NOTE_PATH,
                expected_sha256=view.sha256,
                text="version UI 1",
                actor_id="analyst:integration",
                idempotency_key="pg-note-write-1",
            )
            assert first.revision.sha256 == hashlib.sha256(b"version UI 1").hexdigest()
            note.write_text("edition humaine sur disque", encoding="utf-8")
            await _settle_and_sync(service, investigation.id)

            with pytest.raises(WorkspaceRevisionConflictError):
                await service.write_note(
                    investigation.id,
                    path=NOTE_PATH,
                    expected_sha256=first.revision.sha256,
                    text="version UI 2",
                    actor_id="analyst:integration",
                    idempotency_key="pg-note-write-2",
                )
            await _settle_and_sync(service, investigation.id)
            conflicts = await service.list_conflicts(investigation.id)
            assert len(conflicts.conflicts) == 1
            conflict = conflicts.conflicts[0]
            resolution = await service.resolve_conflict(
                investigation.id,
                conflict_id=conflict.conflict_id,
                choice="keep_proposed",
                expected_current_sha256=conflict.current_sha256 or "new",
                expected_proposed_sha256=conflict.proposed_sha256,
                merged_text=None,
                actor_id="analyst:integration",
                idempotency_key="pg-resolve-1",
            )
            assert resolution.result_revision is not None
            assert note.read_text() == "version UI 2"

            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
            assert len(decisions) == 1
            assert decisions[0].decision_type is AnalystDecisionType.WORKSPACE_CONFLICT_RESOLVE
            assert decisions[0].details["result_sha256"] == conflict.proposed_sha256

            exported = await service.export_pending(investigation.id)
            assert any(entry.path == RESULTS_SUMMARY_PATH for entry in exported.entries)
            summary = root / "IR202611" / subject.slug / RESULTS_SUMMARY_PATH
            assert summary.is_file()
            payload = json.loads(summary.read_text())
            assert payload["schema"] == "autowork-workspace-results.v1"
            assert payload["investigation_id"] == str(investigation.id)
        finally:
            await client.aclose()
