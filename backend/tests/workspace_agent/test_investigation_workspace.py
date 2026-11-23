"""M24-WS1 workspace integration oracles (A01-A07, C02, B exports).

Every test that touches the filesystem drives the **real** host sidecar: a
``ThreadingHTTPServer`` on a tmpdir AF_UNIX socket, the real
``HttpWorkspaceAgentClient`` and the canonical in-memory ingest repositories.
No network, no database, no live provider.
"""

from __future__ import annotations

import asyncio
import calendar
import hashlib
import json
import os
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, date, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI
from httpx import ASGITransport
from sidecars.workspace_agent.agent import AgentConfig, WorkspaceAgent
from sidecars.workspace_agent.auth import TOKEN_FILE_NAME

from cti_app.api.investigation_workspace import router as workspace_router
from cti_app.application.identity import Identity, IdentityScope, LocalIdentityProvider
from cti_app.application.investigation_bootstrap import (
    BOOTSTRAP_SNAPSHOT_OBJECT_TYPE,
    BOOTSTRAP_SOURCE,
    SNAPSHOT_SCHEMA_VERSION,
    BootstrapIocKind,
    BootstrapIocOrigin,
    BootstrapPlanState,
    BootstrapSnapshotIocV1,
    BootstrapSnapshotLimitsV1,
    BootstrapSnapshotV1,
)
from cti_app.application.investigation_workspace import (
    RESULTS_SUMMARY_PATH,
    InvestigationWorkspaceService,
    WorkspaceBindingConflictError,
    WorkspaceExportRefusedError,
    WorkspaceFileNotFoundError,
    WorkspacePathNotEditableError,
    WorkspaceRevisionConflictError,
    WorkspaceServiceError,
    WorkspaceSyncBusyError,
    article_slug_for_subject,
    delivery_code_for_edition,
    plan_export_path,
)
from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceIdentityConflictError,
    WorkspaceLocalUnavailableError,
)
from cti_app.config import Settings
from cti_app.domain.editorial import AnalystDecisionTargetType, AnalystDecisionType
from cti_app.domain.investigation import EvidenceKind, EvidenceObservation, ToolInvocationStatus
from cti_app.domain.investigation_plan import (
    InvestigationLeadV1,
    InvestigationPlanV1,
    PlanParseStatus,
)
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient
from tests.workspace_agent.conftest import RunningAgent
from tests.workspace_agent.ws1_support import (
    NOW,
    SYNTHESIS_SHA256,
    ChainStore,
    FakeArticleSnapshots,
    make_chain,
    workspace_dir,
)

NOTE_PATH = "notes/humaines/note.md"


def _client(agent_server: RunningAgent) -> HttpWorkspaceAgentClient:
    return HttpWorkspaceAgentClient(
        socket_path=str(agent_server.socket_path),
        token_file=str(agent_server.runtime_dir / TOKEN_FILE_NAME),
        timeout_seconds=30.0,
    )


@pytest.fixture
async def agent_port(agent_server: RunningAgent) -> Any:
    client = _client(agent_server)
    try:
        yield client
    finally:
        await client.aclose()


async def _settle_and_sync(
    service: InvestigationWorkspaceService, investigation_id: UUID, *, rounds: int = 3
) -> Any:
    """Let the agent's stability window confirm a disk write, then ingest it.

    Merges the rounds so a caller observes everything a write produced even
    when one sync emitted the event and the next only confirmed the revision.
    """
    reports = []
    for _ in range(rounds):
        await asyncio.sleep(0.5)
        reports.append(await service.sync_workspace(investigation_id))
    last = reports[-1]
    return SimpleNamespace(
        new_human_notes=tuple(
            sorted({path for report in reports for path in report.new_human_notes})
        ),
        changed_human_notes=tuple(
            sorted({path for report in reports for path in report.changed_human_notes})
        ),
        events_inserted=sum(report.events_inserted for report in reports),
        revisions_inserted=sum(report.revisions_inserted for report in reports),
        policy_violations=max(report.policy_violations for report in reports),
        generation=last.generation,
        cursor=last.cursor,
    )


def _write_disk_note(
    root: Path, delivery: str, slug: str, text: str, *, name: str = "note.md"
) -> Path:
    target = workspace_dir(root, delivery, slug) / "notes" / "humaines" / name
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


# ---------------------------------------------------------------- identity


def test_delivery_code_is_ir_plus_year_and_month() -> None:
    from cti_app.domain.classification import TLP
    from cti_app.domain.editions import Edition

    def edition(month: int) -> Edition:
        return Edition(
            country="France",
            country_code="FR",
            period_start=date(2026, month, 1),
            period_end=date(2026, month, calendar.monthrange(2026, month)[1]),
            tlp=TLP.AMBER,
            languages=("fr",),
        )

    assert delivery_code_for_edition(edition(11)) == "IR202611"
    assert delivery_code_for_edition(edition(9)) == "IR202609"


def test_article_slug_reuses_a_stable_subject_slug() -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    assert article_slug_for_subject(store.subject, (store.subject,)) == "bluemoon-cn"


def test_article_slug_falls_back_to_the_subject_ordinal() -> None:
    too_long = "a" * 90
    first = make_chain(subject_slug=too_long)
    second_subject = first.subject.__class__(
        edition_id=first.edition.id,
        title="Second article",
        slug="b" * 91,
        tlp=first.subject.tlp,
        created_at=datetime(2026, 11, 22, 13, 0, tzinfo=UTC),
        updated_at=datetime(2026, 11, 22, 13, 0, tzinfo=UTC),
    )
    assert article_slug_for_subject(first.subject, (second_subject, first.subject)) == "article01"
    assert article_slug_for_subject(second_subject, (first.subject, second_subject)) == "article02"


# -------------------------------------------------------------------- A01


async def test_a01_creation_ir202611_article01_is_idempotent(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="a" * 90, period_start=date(2026, 11, 1))
    service = store.service(agent_port)

    first = await service.ensure_workspace(store.investigation.id)
    assert first.binding.delivery_code == "IR202611"
    assert first.binding.article_slug == "article01"
    assert first.binding.synthesis_sha256 == SYNTHESIS_SHA256
    assert first.binding_created is True
    assert first.workspace_created is True

    root = workspace_dir(agent_server.root, "IR202611", "article01")
    manifest = json.loads((root / ".autowork" / "manifest.json").read_text())
    assert manifest["subject_id"] == str(store.subject.id)
    assert manifest["investigation_id"] == str(store.investigation.id)
    assert manifest["synthesis_sha256"] == SYNTHESIS_SHA256
    manifest_bytes = (root / ".autowork" / "manifest.json").read_bytes()

    second = await service.ensure_workspace(store.investigation.id)
    assert second.binding == first.binding
    assert second.binding_created is False
    assert (root / ".autowork" / "manifest.json").read_bytes() == manifest_bytes
    stored = await store.bindings.get_for_investigation(store.investigation.id)
    assert stored == first.binding


# -------------------------------------------------------------------- A02


async def test_a02_refresh_keeps_human_content_and_the_same_binding(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    first = await service.ensure_workspace(store.investigation.id)
    note = _write_disk_note(agent_server.root, "IR202611", "bluemoon-cn", "human note")

    second = await service.ensure_workspace(store.investigation.id)
    assert note.read_text() == "human note"
    assert second.binding == first.binding
    assert second.binding.created_at == first.binding.created_at


# -------------------------------------------------------------------- A03


async def test_a03_path_collision_with_another_identity_is_refused(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    first = make_chain(subject_slug="a" * 90, period_start=date(2026, 11, 1))
    second = make_chain(subject_slug="b" * 91, period_start=date(2026, 11, 1))
    service_a = first.service(agent_port)
    service_b = second.service(agent_port)

    await service_a.ensure_workspace(first.investigation.id)
    with pytest.raises(WorkspaceIdentityConflictError) as refused:
        await service_b.ensure_workspace(second.investigation.id)
    assert refused.value.code == "workspace_identity_conflict"
    stored = await second.bindings.get_for_investigation(second.investigation.id)
    assert stored is None


async def test_a03_canonical_binding_collision_is_refused(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    intruder = make_chain(subject_slug="c" * 90, period_start=date(2026, 11, 1))
    workspace_id = "ws-" + hashlib.sha256(b"IR202611\x00article01").hexdigest()[:32]
    await intruder.bindings.add_if_absent(
        WorkspaceBindingV1(
            workspace_id=workspace_id,
            investigation_id=uuid4(),
            subject_id=uuid4(),
            delivery_code="IR202611",
            article_slug="article01",
            synthesis_sha256=SYNTHESIS_SHA256,
            created_at=NOW,
        )
    )
    with pytest.raises(WorkspaceBindingConflictError):
        await intruder.service(agent_port).ensure_workspace(intruder.investigation.id)
    assert await intruder.bindings.get_for_investigation(intruder.investigation.id) is None


# -------------------------------------------------------------------- A04


async def test_a04_disk_note_is_synced_versioned_and_visible(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    await service.ensure_workspace(store.investigation.id)
    _write_disk_note(agent_server.root, "IR202611", "bluemoon-cn", "note écrite sur disque")

    report = await _settle_and_sync(service, store.investigation.id)
    assert NOTE_PATH in report.new_human_notes
    assert report.events_inserted >= 1
    assert report.revisions_inserted >= 1

    listing = await service.list_files(store.investigation.id)
    view = next(item for item in listing.files if item.path == NOTE_PATH)
    assert view.origin == "filesystem"
    assert view.policy_class == "human_editable"
    assert view.import_status == "received"
    assert len(view.history) == 1
    assert view.task_id is None

    content = await service.read_file(store.investigation.id, path=NOTE_PATH)
    assert content.text == "note écrite sur disque"
    assert content.metadata_only is False
    assert content.sha256 == view.sha256

    replay = await service.sync_workspace(store.investigation.id)
    assert replay.events_inserted == 0
    assert len(await store.revisions.list_for_workspace(listing.workspace_id)) == 1


@asynccontextmanager
async def _local_agent(root: Path, runtime_dir: Path) -> AsyncIterator[WorkspaceAgent]:
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
    scan_thread: threading.Thread | None = None
    try:
        ready = await asyncio.to_thread(agent.wait_ready, 15)
        assert ready, "workspace agent did not become ready"
        scan_thread = agent._scan_thread
        yield agent
    finally:
        stop.set()
        await asyncio.to_thread(thread.join)
        if scan_thread is not None:
            await asyncio.to_thread(scan_thread.join)


async def test_sync_resyncs_after_a_generation_change(
    tmp_path: Path, socket_runtime_dir: Path
) -> None:
    root = tmp_path / "workspaces"
    root.mkdir()
    runtime_dir = socket_runtime_dir
    store = make_chain(subject_slug="bluemoon-cn")
    client = HttpWorkspaceAgentClient(
        socket_path=str(runtime_dir / "workspace-agent.sock"),
        token_file=str(runtime_dir / TOKEN_FILE_NAME),
        timeout_seconds=30.0,
    )
    service = store.service(client)
    try:
        async with _local_agent(root, runtime_dir):
            await service.ensure_workspace(store.investigation.id)
            _write_disk_note(root, "IR202611", "bluemoon-cn", "note avant redémarrage")
            await _settle_and_sync(service, store.investigation.id)
            workspace = workspace_dir(root, "IR202611", "bluemoon-cn")
        # Stop and join the periodic scanner before removing its projections;
        # an in-flight scan could otherwise recreate them with the old
        # generation between the unlink and agent shutdown.
        (workspace / ".autowork" / "file-index.json").unlink()
        (workspace / ".autowork" / "sync-state.json").unlink()
        async with _local_agent(root, runtime_dir):
            report = await service.sync_workspace(store.investigation.id)
    finally:
        await client.aclose()

    assert report.resync is True
    assert report.generation > 1
    listing = await service.list_files(store.investigation.id)
    view = next(item for item in listing.files if item.path == NOTE_PATH)
    assert len(view.history) == 1


# -------------------------------------------------------------------- A06


async def test_a06_ui_write_vs_filesystem_edit_keeps_both_and_records_one_decision(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    await service.ensure_workspace(store.investigation.id)

    first = await service.write_note(
        store.investigation.id,
        path=NOTE_PATH,
        expected_sha256="new",
        text="version UI 1",
        actor_id="analyst:test",
        idempotency_key="note-write-1",
    )
    disk = workspace_dir(agent_server.root, "IR202611", "bluemoon-cn") / NOTE_PATH
    disk.write_text("édition humaine sur disque", encoding="utf-8")
    await _settle_and_sync(service, store.investigation.id)

    with pytest.raises(WorkspaceRevisionConflictError) as raised:
        await service.write_note(
            store.investigation.id,
            path=NOTE_PATH,
            expected_sha256=first.revision.sha256,
            text="version UI 2",
            actor_id="analyst:test",
            idempotency_key="note-write-2",
        )
    details = raised.value.details
    current_version = cast(Any, details["current"])
    proposed_version = cast(Any, details["proposed"])
    assert current_version["text"] == "édition humaine sur disque"
    assert proposed_version["text"] == "version UI 2"
    assert "version UI 2" in str(details["diff"])
    assert details["conflict_path"] is not None
    assert disk.read_text() == "édition humaine sur disque"

    await _settle_and_sync(service, store.investigation.id)
    conflicts = await service.list_conflicts(store.investigation.id)
    assert len(conflicts.conflicts) == 1
    conflict = conflicts.conflicts[0]
    assert conflict.path == NOTE_PATH
    # The agent's 409 conflict id and the canonical derivation must agree.
    assert conflict.conflict_id == details["conflict_id"]
    assert conflict.conflict_path is not None
    assert conflict.current is not None and conflict.current.text == "édition humaine sur disque"
    assert conflict.proposed is not None and conflict.proposed.text == "version UI 2"
    assert conflict.diff is not None

    resolution = await service.resolve_conflict(
        store.investigation.id,
        conflict_id=conflict.conflict_id,
        choice="keep_proposed",
        expected_current_sha256=conflict.current_sha256 or "new",
        expected_proposed_sha256=conflict.proposed_sha256,
        merged_text=None,
        actor_id="analyst:test",
        idempotency_key="resolve-conflict-1",
    )
    assert resolution.result_revision is not None
    assert resolution.result_revision.sha256 == conflict.proposed_sha256
    assert disk.read_text() == "version UI 2"
    assert len(store.decisions.rows) == 1
    decision = store.decisions.rows[0]
    assert decision.decision_type is AnalystDecisionType.WORKSPACE_CONFLICT_RESOLVE
    assert decision.target_type is AnalystDecisionTargetType.WORKSPACE_CONFLICT
    assert decision.id == resolution.decision_id
    assert decision.details["choice"] == "keep_proposed"
    assert decision.details["path"] == NOTE_PATH
    assert decision.details["proposed_sha256"] == conflict.proposed_sha256
    loser = conflict.conflict_path
    assert loser is not None
    assert (workspace_dir(agent_server.root, "IR202611", "bluemoon-cn") / loser).is_file()

    after = await service.list_conflicts(store.investigation.id)
    assert after.conflicts == ()

    replay = await service.resolve_conflict(
        store.investigation.id,
        conflict_id=conflict.conflict_id,
        choice="keep_proposed",
        expected_current_sha256=conflict.current_sha256 or "new",
        expected_proposed_sha256=conflict.proposed_sha256,
        merged_text=None,
        actor_id="analyst:test",
        idempotency_key="resolve-conflict-1",
    )
    assert replay.idempotent_replay is True
    assert len(store.decisions.rows) == 1


# -------------------------------------------------------------------- A07


async def test_a07_traversal_symlink_and_generated_paths_are_refused(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    await service.ensure_workspace(store.investigation.id)

    with pytest.raises(WorkspaceServiceError):
        await service.write_note(
            store.investigation.id,
            path="../escaped.md",
            expected_sha256="new",
            text="escape",
            actor_id="analyst:test",
            idempotency_key="traversal-write",
        )
    with pytest.raises(WorkspacePathNotEditableError):
        await service.write_note(
            store.investigation.id,
            path="plans/evil.md",
            expected_sha256="new",
            text="generated",
            actor_id="analyst:test",
            idempotency_key="generated-write",
        )
    with pytest.raises(WorkspacePathNotEditableError):
        await service.write_note(
            store.investigation.id,
            path="notes/agents/task-1/note.md",
            expected_sha256="new",
            text="agent owned",
            actor_id="analyst:test",
            idempotency_key="agent-write-1",
        )

    root = workspace_dir(agent_server.root, "IR202611", "bluemoon-cn")
    link = root / "notes" / "humaines" / "link.md"
    os.symlink("/etc/passwd", link)
    report = await _settle_and_sync(service, store.investigation.id)
    assert report.policy_violations >= 1
    listing = await service.list_files(store.investigation.id)
    assert all(item.path != "notes/humaines/link.md" for item in listing.files)
    with pytest.raises(WorkspaceFileNotFoundError):
        await service.read_file(store.investigation.id, path="notes/humaines/link.md")


# -------------------------------------------------------------------- C02


async def _plan_version(store: ChainStore, version: int, title: str) -> Any:
    plan = InvestigationPlanV1(
        investigation_id=store.investigation.id,
        version=version,
        leads=(InvestigationLeadV1(title=title, objective=f"Objectif {version}"),),
    )
    from cti_app.domain.investigation_plan import InvestigationPlanVersion

    row = InvestigationPlanVersion(
        investigation_id=store.investigation.id,
        version=version,
        content=plan,
        parse_status=PlanParseStatus.PARSED,
        source_invocation_id=uuid4(),
        source_observation_id=uuid4(),
        created_at=NOW,
    )
    expected = None if version == 1 else version - 1
    stored, _created = await store.plans.create_version(row, expected_current_version=expected)
    return stored


async def test_c02_plan_export_is_versioned_and_never_rewrites_previous(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    await service.ensure_workspace(store.investigation.id)
    version_one = await _plan_version(store, 1, "Vérifier le chargeur")

    first = await service.export_pending(store.investigation.id)
    initial = workspace_dir(agent_server.root, "IR202611", "bluemoon-cn") / plan_export_path(1)
    assert initial.is_file()
    initial_bytes = initial.read_bytes()
    text = initial_bytes.decode("utf-8")
    assert "version 1" in text
    assert version_one.content_sha256 in text
    assert any(
        entry.path == plan_export_path(1) and entry.action == "exported" for entry in first.entries
    )

    await _plan_version(store, 2, "Cartographier le C2")
    second = await service.export_pending(store.investigation.id)
    revision_two = workspace_dir(agent_server.root, "IR202611", "bluemoon-cn") / plan_export_path(2)
    assert revision_two.is_file()
    assert "version 2" in revision_two.read_text()
    assert initial.read_bytes() == initial_bytes
    assert any(
        entry.path == plan_export_path(1) and entry.action == "already_exported"
        for entry in second.entries
    )

    third = await service.export_pending(store.investigation.id)
    assert third.exported == 0
    assert third.already_exported == len(third.entries)
    assert any(entry.path == RESULTS_SUMMARY_PATH for entry in third.entries)
    summary_path = (
        workspace_dir(agent_server.root, "IR202611", "bluemoon-cn") / RESULTS_SUMMARY_PATH
    )
    summary = json.loads(summary_path.read_text())
    assert summary["plan"]["current_version"] == 2


# --------------------------------------------------------------- B exports


def _snapshot(*, snapshot_id: UUID, investigation_id: UUID, value: str) -> BootstrapSnapshotV1:
    return BootstrapSnapshotV1(
        snapshot_id=snapshot_id,
        investigation_id=investigation_id,
        pinned_synthesis_sha256=SYNTHESIS_SHA256,
        generated_at=NOW,
        plan_state=BootstrapPlanState.EMPTY,
        inventory_sha256="b" * 64,
        plan_sha256="c" * 64,
        iocs=(
            BootstrapSnapshotIocV1(
                ioc_id="ioc-" + "a" * 24,
                kind=BootstrapIocKind.SAMPLE,
                value=value,
                hash_type="sha256",
                origin=BootstrapIocOrigin.SEED,
                status=ToolInvocationStatus.SUCCEEDED,
            ),
        ),
        limits=BootstrapSnapshotLimitsV1(
            max_iocs=1,
            iocs_included=1,
            max_operations=1,
            operations_recorded=0,
        ),
    )


def _persist_snapshot(
    store: ChainStore, snapshot: BootstrapSnapshotV1, *, observed_at: datetime
) -> EvidenceObservation:
    payload = snapshot.model_dump(mode="json")
    blob_id, sha256 = store.artifacts.put_json(payload)
    observation = EvidenceObservation(
        id=uuid4(),
        investigation_id=store.investigation.id,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256=sha256,
        source=BOOTSTRAP_SOURCE,
        source_version=SNAPSHOT_SCHEMA_VERSION,
        observed_at=observed_at,
        object_type=BOOTSTRAP_SNAPSHOT_OBJECT_TYPE,
        object_id=str(snapshot.snapshot_id),
    )
    return observation


async def test_b_exports_vt_files_without_secrets(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    await service.ensure_workspace(store.investigation.id)
    snapshot = _snapshot(
        snapshot_id=uuid4(), investigation_id=store.investigation.id, value="d" * 64
    )
    await store.observations.append_if_absent(_persist_snapshot(store, snapshot, observed_at=NOW))

    report = await service.export_pending(store.investigation.id)
    assert report.vt_snapshot == "exported"
    root = workspace_dir(agent_server.root, "IR202611", "bluemoon-cn")
    vt_manifest = root / "vt" / "manifest.json"
    assert vt_manifest.is_file()
    assert any(entry.path.startswith("vt/files/") for entry in report.entries)
    for entry in report.entries:
        if not entry.path.startswith("vt/"):
            continue
        text = (root / entry.path).read_text(encoding="utf-8")
        assert "api_key" not in text
        assert "access_token" not in text
        assert "x-amz-signature" not in text
        assert hashlib.sha256(text.encode("utf-8")).hexdigest() == entry.sha256


async def test_b_exports_refuse_credential_material(
    agent_server: RunningAgent, agent_port: Any
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    await service.ensure_workspace(store.investigation.id)
    tampered = _snapshot(
        snapshot_id=uuid4(),
        investigation_id=store.investigation.id,
        value="api_key=SUPERSECRET",
    )
    await store.observations.append_if_absent(
        _persist_snapshot(store, tampered, observed_at=datetime(2026, 11, 23, tzinfo=UTC))
    )
    with pytest.raises(WorkspaceExportRefusedError):
        await service.export_pending(store.investigation.id)


# ------------------------------------------------ availability and 503 paths


async def test_agent_unavailable_degrades_reads_and_refuses_writes(
    tmp_path: Path,
) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    unavailable = HttpWorkspaceAgentClient(
        socket_path=str(tmp_path / "missing.sock"),
        token_file=str(tmp_path / "missing-token"),
    )
    try:
        service = store.service(unavailable)
        binding = WorkspaceBindingV1(
            workspace_id="ws-" + "1" * 32,
            investigation_id=store.investigation.id,
            subject_id=store.subject.id,
            delivery_code="IR202611",
            article_slug="bluemoon-cn",
            synthesis_sha256=SYNTHESIS_SHA256,
            created_at=NOW,
        )
        await store.bindings.add_if_absent(binding)
        status = await service.status(store.investigation.id)
        assert status.workspace_available is False
        assert status.availability_code in {"unavailable", "unconfigured"}
        assert status.availability_reason == "Workspace local indisponible"
        listing = await service.list_files(store.investigation.id)
        assert listing.workspace_available is False
        conflicts = await service.list_conflicts(store.investigation.id)
        assert conflicts.workspace_available is False
        with pytest.raises(WorkspaceLocalUnavailableError):
            await service.sync_workspace(store.investigation.id)
        with pytest.raises(WorkspaceLocalUnavailableError):
            await service.write_note(
                store.investigation.id,
                path=NOTE_PATH,
                expected_sha256="new",
                text="x",
                actor_id="analyst:test",
                idempotency_key="unavailable-write",
            )
        with pytest.raises(WorkspaceLocalUnavailableError):
            await service.ensure_workspace(store.investigation.id)
    finally:
        await unavailable.aclose()


class _SyncASGIClient:
    def __init__(self, app: FastAPI) -> None:
        self.app = app

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        async def send() -> httpx.Response:
            async with httpx.AsyncClient(
                transport=ASGITransport(app=self.app), base_url="http://test"
            ) as client:
                return await client.request(method, path, **kwargs)

        return asyncio.run(send())

    def get(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("GET", path, **kwargs)

    def post(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("POST", path, **kwargs)

    def put(self, path: str, **kwargs: Any) -> httpx.Response:
        return self._request("PUT", path, **kwargs)


class _CompositionFactory:
    def __init__(self, store: ChainStore) -> None:
        self.article_snapshots = FakeArticleSnapshots(store.article_snapshot)
        self.artifact_store = store.artifacts


def _api_client(
    store: ChainStore,
    agent: Any,
    *,
    identity: Any | None = None,
) -> _SyncASGIClient:
    app = FastAPI()
    app.include_router(workspace_router)
    app.state.uow_factory = store.uow
    app.state.investigation_composition_factory = _CompositionFactory(store)
    app.state.identity_provider = identity or LocalIdentityProvider("analyst:test")
    app.state.settings = Settings(_env_file=None)
    app.state.workspace_agent_port = agent
    return _SyncASGIClient(app)


def test_api_writes_are_typed_503_when_the_agent_is_unavailable(tmp_path: Path) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    unavailable = HttpWorkspaceAgentClient(
        socket_path=str(tmp_path / "missing.sock"),
        token_file=str(tmp_path / "missing-token"),
    )
    try:
        client = _api_client(store, unavailable)
        base = f"/api/investigations/{store.investigation.id}/workspace"
        ensure = client.post(f"{base}/ensure")
        assert ensure.status_code == 503
        assert ensure.json()["detail"]["code"] == "workspace_local_unavailable"
        unbound_sync = client.post(f"{base}/sync")
        assert unbound_sync.status_code == 409
        assert unbound_sync.json()["detail"]["code"] == "workspace_not_bound"

        binding = WorkspaceBindingV1(
            workspace_id="ws-" + "2" * 32,
            investigation_id=store.investigation.id,
            subject_id=store.subject.id,
            delivery_code="IR202611",
            article_slug="bluemoon-cn",
            synthesis_sha256=SYNTHESIS_SHA256,
            created_at=NOW,
        )
        asyncio.run(store.bindings.add_if_absent(binding))
        sync = client.post(f"{base}/sync")
        assert sync.status_code == 503
        assert sync.json()["detail"]["code"] == "workspace_local_unavailable"
        write = client.put(
            f"{base}/files",
            json={"path": NOTE_PATH, "expected_sha256": "new", "text": "x"},
            headers={"Idempotency-Key": "api-unavailable-1"},
        )
        assert write.status_code == 503
        assert write.json()["detail"]["code"] == "workspace_local_unavailable"
        status = client.get(base)
        assert status.status_code == 200
        body = status.json()
        assert body["workspace_available"] is False
        assert body["availability_reason"] == "Workspace local indisponible"
    finally:
        asyncio.run(unavailable.aclose())


def test_api_refuses_operator_clients() -> None:
    class OperatorIdentityProvider:
        async def current(self) -> Identity:
            return Identity(
                actor_id="operator:codex-client",
                scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
            )

    store = make_chain(subject_slug="bluemoon-cn")
    unavailable = HttpWorkspaceAgentClient(socket_path=None, token_file=None)
    client = _api_client(store, unavailable, identity=OperatorIdentityProvider())
    response = client.get(f"/api/investigations/{store.investigation.id}/workspace")
    assert response.status_code == 403
    assert response.json()["detail"]["code"] == "verified_human_operator_required"


def test_api_happy_path_routes(agent_server: RunningAgent, agent_port: Any) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    client = _api_client(store, agent_port)
    base = f"/api/investigations/{store.investigation.id}/workspace"
    ensured = client.post(f"{base}/ensure")
    assert ensured.status_code == 200, ensured.text
    assert ensured.json()["binding"]["delivery_code"] == "IR202611"

    status = client.get(base)
    assert status.status_code == 200, status.text
    assert status.json()["workspace_available"] is True
    assert status.json()["manifest"]["article_slug"] == "bluemoon-cn"

    written = client.put(
        f"{base}/files",
        json={"path": NOTE_PATH, "expected_sha256": "new", "text": "note via API"},
        headers={"Idempotency-Key": "api-note-write-1"},
    )
    assert written.status_code == 200, written.text
    assert written.json()["created"] is True

    asyncio.run(_settle_and_sync(store.service(agent_port), store.investigation.id))
    listing = client.get(f"{base}/files")
    assert listing.status_code == 200, listing.text
    assert any(item["path"] == NOTE_PATH for item in listing.json()["files"])
    content = client.get(f"{base}/files/content", params={"path": NOTE_PATH})
    assert content.status_code == 200, content.text
    assert content.json()["text"] == "note via API"

    exported = client.post(f"{base}/exports")
    assert exported.status_code == 200, exported.text
    assert any(entry["path"] == RESULTS_SUMMARY_PATH for entry in exported.json()["entries"])
    conflicts = client.get(f"{base}/conflicts")
    assert conflicts.status_code == 200, conflicts.text
    assert conflicts.json()["conflicts"] == []


# --------------------------------------------------------------- single-flight


async def test_sync_workspace_is_single_flight(agent_server: RunningAgent, agent_port: Any) -> None:
    store = make_chain(subject_slug="bluemoon-cn")
    service = store.service(agent_port)
    await service.ensure_workspace(store.investigation.id)

    started = asyncio.Event()
    release = asyncio.Event()
    original = agent_port.scan_workspace

    async def blocked(*args: Any, **kwargs: Any) -> Any:
        started.set()
        await release.wait()
        return await original(*args, **kwargs)

    agent_port.scan_workspace = blocked
    try:
        first = asyncio.create_task(service.sync_workspace(store.investigation.id))
        await asyncio.wait_for(started.wait(), timeout=5)
        with pytest.raises(WorkspaceSyncBusyError) as busy:
            await service.sync_workspace(store.investigation.id)
        assert busy.value.code == "workspace_sync_in_progress"
        release.set()
        report = await asyncio.wait_for(first, timeout=15)
        assert report.workspace_id == first_binding_workspace(store)
    finally:
        agent_port.scan_workspace = original


def first_binding_workspace(store: ChainStore) -> str:
    return "ws-" + hashlib.sha256(b"IR202611\x00bluemoon-cn").hexdigest()[:32]
