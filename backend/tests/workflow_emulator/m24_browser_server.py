"""Serve the M24 investigation workshop against isolated PostgreSQL and fake providers.

This follows the M23 editorial browser-server pattern
(``editorial_browser_server.py``) and the M24 acceptance harness
(``tests/acceptance/scenario_support/harness.py``):

* a throwaway database is created on the local integration PostgreSQL instance
  and migrated to the Alembic head with the real migrations;
* the real FastAPI application is built by ``create_app`` and served on
  loopback with ``lifespan="off"``; the state, ports and services the lifespan
  would build are injected exactly like the acceptance harness does;
* the host workspace sidecar is the real one, on a short tmpdir AF_UNIX socket,
  with the H1 fake CLI profiles registered for both adapter kinds;
* VirusTotal, Bridge and the CLIs are deterministic fakes.  The Bridge double
  *answers by itself* from the prompt (plan, two-paragraph note draft, review,
  advisory), so the browser can drive the human journey without a test-side
  queue.  No live provider, CLI, network call or real CTI data is involved;
* Typst is the real pinned compiler, wired but only exercised by PDF steps.

The launcher refuses to boot without the fake-provider attestation, a loopback
PostgreSQL DSN and a synthetic operator token of at least 32 ASCII characters.
The token is injected into the browser only through the Vite proxy's
``OPERATOR_API_TOKEN`` environment; it never appears in the state file, the
state route or the ready line.

Control routes (loopback only, outside ``/api`` so they carry no operator
token):

* ``GET /api/m24-browser/state`` — seeded ids, material, fixture counters and
  live PostgreSQL diagnostics for the browser spec (through the Vite proxy);
* ``POST /__m24_browser/drain-instructions`` — one real ID-2 instruction drain
  tick, because ``lifespan="off"`` disables the background loop;
* ``POST /__m24_browser/shutdown`` — clean uvicorn stop.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import re
import signal
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import uvicorn
from fastapi import FastAPI
from sqlalchemy.engine import make_url

from cti_app.api import main as api_main
from cti_app.api.main import _instruction_drain_tick_lock, create_app
from cti_app.application.agent_session_disclosure import (
    SessionDisclosureUnitOfWork,
    build_session_disclosure_checker,
)
from cti_app.application.bridge_investigator import (
    ScriptedTurnTransport,
    TurnSubmission,
    make_investigation_turn_idempotency_key,
)
from cti_app.application.identity import StaticTokenIdentityProvider
from cti_app.application.instruction_delivery import InstructionDeliveryService
from cti_app.application.investigation_jobs import make_investigation_runner
from cti_app.application.investigation_transfers import (
    TransferUnitOfWork,
    WorkspaceNoteCatalog,
)
from cti_app.application.jobs import JobService, create_job_registry
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.subject_content import SubjectContentService
from cti_app.application.subjects import SubjectService
from cti_app.application.workspace_agent import WorkspaceAgentPort
from cti_app.infrastructure.agentic.wiring import build_session_runtime
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from cti_app.infrastructure.workspace_agent import HttpWorkspaceAgentClient
from tests.acceptance.scenario_support.harness import (
    AcceptanceVirusTotal,
    DeferredDispatcher,
    ScenarioMaterial,
    acceptance_settings,
    advisory_consult_text,
    bridge_submission,
    build_artifact_store,
    build_publication_render_service,
    install_fake_cli_profiles,
    plan_text,
    running_scenario_agent,
    seed_article,
)
from tests.integration.conftest import _alembic_config, _create_database, _drop_database
from tests.workflow_emulator.investigation_support import composition_factory

ATTESTATION_ENV = "M24_REAL_LITE_ATTESTED_FAKE_PROVIDER"
OPERATOR_TOKEN_ENV = "OPERATOR_API_TOKEN"
STATE_PATH_ENV = "WORKFLOW_BROWSER_STATE"
API_PORT_ENV = "WORKFLOW_BROWSER_API_PORT"
DEFAULT_API_PORT = 18768
MIN_OPERATOR_TOKEN_LENGTH = 32
READY_LINE_PREFIX = "M24-investigation-browser / backend ready"

_PLAN_LINE = re.compile(r"^TYPE: (?P<type>[a-z][a-z0-9_]*)$", re.MULTILINE)
_CANONICAL_TARGETS_MARKER = "CIBLES CANONIQUES"
_SYNTHETIC_NOTE_LIMITATION = "Reponse Bridge synthetique de test; aucun fournisseur reel."


def _is_loopback_host(host: str | None) -> bool:
    if host in (None, "") or host.startswith("/"):
        return True
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _required_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        raise RuntimeError(f"{name} is required for the M24 investigation browser server")
    return value


def _api_port() -> int:
    raw = os.environ.get(API_PORT_ENV, str(DEFAULT_API_PORT))
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{API_PORT_ENV} must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError(f"{API_PORT_ENV} must be between 1 and 65535")
    return port


def _operator_token() -> str:
    token = _required_env(OPERATOR_TOKEN_ENV)
    if (
        len(token) < MIN_OPERATOR_TOKEN_LENGTH
        or not token.isascii()
        or any(character.isspace() for character in token)
    ):
        raise RuntimeError(
            f"{OPERATOR_TOKEN_ENV} must contain at least {MIN_OPERATOR_TOKEN_LENGTH} "
            "ASCII non-whitespace characters (synthetic test token, never a credential)"
        )
    return token


@dataclass(frozen=True)
class BrowserEnvironment:
    """Validated launcher contract; the token never enters ``repr`` or JSON."""

    admin_dsn: str
    state_path: Path
    api_port: int
    operator_token: str = field(repr=False)


def validate_environment() -> BrowserEnvironment:
    """Validate the harness contract before touching PostgreSQL or the app."""
    if os.environ.get(ATTESTATION_ENV) != "1":
        raise RuntimeError(
            f"{ATTESTATION_ENV}=1 is required: the backend uses fake Bridge/VT/CLI "
            "providers and no live provider credentials"
        )
    admin_dsn = _required_env("TEST_POSTGRES_ADMIN_DSN")
    admin_url = make_url(admin_dsn)
    if not _is_loopback_host(admin_url.host):
        raise RuntimeError(
            "the M24 investigation browser server requires TEST_POSTGRES_ADMIN_DSN "
            "to target local PostgreSQL"
        )
    state_path = Path(_required_env(STATE_PATH_ENV))
    return BrowserEnvironment(
        admin_dsn=admin_dsn,
        state_path=state_path,
        api_port=_api_port(),
        operator_token=_operator_token(),
    )


class _SettingsPatcher:
    """Minimal ``pytest.MonkeyPatch`` lookalike for the acceptance harness.

    ``install_fake_cli_profiles`` only needs ``setenv`` and ``setattr``; the
    acceptance builder needs ``setattr`` before ``create_app`` and keeps the
    patched values for the server's lifetime.
    """

    def __init__(self) -> None:
        self._restorations: list[Callable[[], None]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        previous = getattr(target, name)
        setattr(target, name, value)
        self._restorations.append(lambda: setattr(target, name, previous))

    def setenv(self, name: str, value: str) -> None:
        previous = os.environ.get(name)
        os.environ[name] = value
        if previous is None:
            self._restorations.append(lambda: os.environ.pop(name, None))
        else:
            self._restorations.append(lambda: os.environ.__setitem__(name, previous))

    def delenv(self, name: str, raising: bool = True) -> None:
        previous = os.environ.pop(name, None)
        if previous is None and raising:
            raise KeyError(name)
        if previous is not None:
            self._restorations.append(lambda: os.environ.__setitem__(name, previous))

    def undo(self) -> None:
        while self._restorations:
            self._restorations.pop()()


# ---------------------------------------------------------------------------
# Deterministic Bridge auto-responder
# ---------------------------------------------------------------------------


def prompt_consultation_type(prompt: str) -> str:
    """Read the ``TYPE:`` header the real advisor prompt renderer emits."""
    match = _PLAN_LINE.search(prompt)
    return "" if match is None else match.group("type")


def prompt_canonical_targets(prompt: str) -> list[dict[str, Any]]:
    """Read the canonical draft/review targets the prompt names, if any.

    The renderer emits one JSON object per line right after the
    ``CIBLES CANONIQUES`` marker; the block ends at the first non-JSON line.
    """
    targets: list[dict[str, Any]] = []
    capture = False
    for line in prompt.splitlines():
        if not capture:
            if line.startswith(_CANONICAL_TARGETS_MARKER):
                capture = True
            continue
        if not line.startswith("{"):
            break
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            targets.append(payload)
    return targets


def two_paragraph_note(unit_label: str) -> list[dict[str, str]]:
    """Two stored sentences whose boundary keeps a real paragraph break."""
    return [
        {"text": f"Reponse synthetique pour {unit_label} : premier fait verifie sur les octets."},
        {
            "text": (
                f"\n\nSecond paragraphe synthetique pour {unit_label} : la continuite "
                "du saut de paragraphe est conservee telle quelle."
            )
        },
    ]


class AutoRespondingBridgeTransport(ScriptedTurnTransport):
    """Bridge double answering each consultation from its own prompt.

    The real advisor prompt renderer starts with ``TYPE: <consultation_type>``,
    so the answer is chosen deterministically from the prompt, never from a
    test-side queue.  Plans use the acceptance markdown wire; note drafts use a
    strict ``advisor-response.v3`` payload with a two-paragraph answer; reviews
    use the same schema with per-unit remarks; every other consultation gets
    the canonical advisory wire.  Idempotent replays reuse the first answer.
    """

    def __init__(self, *, material: ScenarioMaterial) -> None:
        super().__init__(())
        self._material = material
        self._generated: set[str] = set()
        self.consultation_types: list[str] = []

    async def _submit(
        self,
        *,
        investigation_id: UUID,
        subject_id: UUID,
        turn_index: int,
        context_sha256: str,
        prompt: str,
        external_llm_allowed: bool,
        attempt: int = 0,
    ) -> TurnSubmission:
        key = make_investigation_turn_idempotency_key(
            investigation_id=investigation_id,
            turn_index=turn_index,
            context_sha256=context_sha256,
            attempt=attempt,
        )
        if key not in self._generated:
            self._generated.add(key)
            if external_llm_allowed:
                consultation_type = prompt_consultation_type(prompt)
                self.consultation_types.append(consultation_type)
                self._responses.append(
                    self._respond(consultation_type, prompt, investigation_id=investigation_id)
                )
        return await super()._submit(
            investigation_id=investigation_id,
            subject_id=subject_id,
            turn_index=turn_index,
            context_sha256=context_sha256,
            prompt=prompt,
            external_llm_allowed=external_llm_allowed,
            attempt=attempt,
        )

    def fixtures(self) -> dict[str, Any]:
        return {
            "bridge_submissions": self.submission_count,
            "bridge_consultation_types": list(self.consultation_types),
        }

    def _respond(
        self, consultation_type: str, prompt: str, *, investigation_id: UUID
    ) -> TurnSubmission:
        if consultation_type == "plan_investigation":
            snapshot_id = uuid5(NAMESPACE_URL, f"m24-browser-plan:{investigation_id}")
            return self._submission(plan_text(self._material, snapshot_id=snapshot_id))
        if consultation_type == "draft_text":
            return self._submission(json.dumps(self._draft_payload(prompt)))
        if consultation_type == "review_text":
            return self._submission(json.dumps(self._review_payload(prompt)))
        return self._submission(advisory_consult_text())

    @staticmethod
    def _submission(output_text: str) -> TurnSubmission:
        # The acceptance helper is untyped; its runtime contract is exact.
        return cast(TurnSubmission, bridge_submission(output_text))

    def _draft_payload(self, prompt: str) -> dict[str, Any]:
        targets = prompt_canonical_targets(prompt)
        blocks: list[dict[str, Any]] = []
        for index, target in enumerate(targets, start=1):
            unit_id = str(target.get("id") or target.get("unit_id") or f"U{index}")
            blocks.append(
                {
                    "unit_id": unit_id,
                    "block_type": target.get("block_type"),
                    "form": target.get("form", "analyst_note"),
                    "anchor": target.get("anchor"),
                    "sentences": two_paragraph_note(unit_id),
                }
            )
        if not blocks:
            blocks = [
                {
                    "block_type": "key_findings",
                    "form": "body",
                    "anchor": None,
                    "sentences": two_paragraph_note("U1"),
                }
            ]
        return {
            "schema": "advisor-response.v3",
            "consultation_type": "draft_text",
            "status": "succeeded",
            "language": "fr",
            "blocks": blocks,
            "untrusted": True,
            "limitations": [_SYNTHETIC_NOTE_LIMITATION],
        }

    def _review_payload(self, prompt: str) -> dict[str, Any]:
        targets = prompt_canonical_targets(prompt)
        reviews = [
            {
                "unit_id": str(target.get("id") or target.get("unit_id") or f"U{index}"),
                "remarks": [
                    "Le texte reste conforme aux faits synthetiques cites et ne les "
                    "surinterprete pas."
                ],
            }
            for index, target in enumerate(targets, start=1)
        ]
        if not reviews:
            reviews = [
                {
                    "unit_id": "U1",
                    "remarks": ["La revue synthetique confirme la coherence du texte transmis."],
                }
            ]
        return {
            "schema": "advisor-response.v3",
            "consultation_type": "review_text",
            "status": "succeeded",
            "language": "fr",
            "reviews": reviews,
            "untrusted": True,
            "limitations": [_SYNTHETIC_NOTE_LIMITATION],
        }


# ---------------------------------------------------------------------------
# Published browser state
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class BrowserState:
    """The seeded fixture facts the browser spec reads at startup."""

    subject_id: UUID
    edition_id: UUID
    production_run_id: UUID
    synthesis_artifact_id: UUID
    synthesis_sha256: str
    publication_artifact_id: UUID
    publication_sha256: str
    subject_slug: str
    database_name: str
    api_port: int
    workspace_root: Path
    material: ScenarioMaterial

    def as_json(self) -> dict[str, Any]:
        """Seed facts only: no operator token, no contract, no hidden path."""
        return {
            "fixture": "m24-investigation-browser",
            "database_name": self.database_name,
            "api_port": self.api_port,
            "subject_id": str(self.subject_id),
            "subject_slug": self.subject_slug,
            "edition_id": str(self.edition_id),
            "production_run_id": str(self.production_run_id),
            "synthesis_artifact_id": str(self.synthesis_artifact_id),
            "synthesis_sha256": self.synthesis_sha256,
            "publication_artifact_id": str(self.publication_artifact_id),
            "publication_sha256": self.publication_sha256,
            "workspace_root": str(self.workspace_root),
            "material": {
                "hashes": list(self.material.all_hashes),
                "downloadable_hashes": list(self.material.available_hashes),
                "unavailable_hashes": list(self.material.unavailable_hashes),
                "domains": list(self.material.domains),
                "ips": list(self.material.ips),
            },
        }


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _workspace_diagnostics(root: Path, delivery_code: str, article_slug: str) -> dict[str, Any]:
    """Read-only facts a browser test may assert on the real host workspace."""
    workspace_root = root / delivery_code / article_slug
    manifest = _read_json(workspace_root / ".autowork" / "manifest.json")
    base_samples_dir = workspace_root / "base_samples"
    base_samples = (
        sorted(path.name for path in base_samples_dir.iterdir() if path.is_file())
        if base_samples_dir.is_dir()
        else []
    )
    return {
        "workspace_root": str(workspace_root),
        "manifest": manifest,
        "base_samples": base_samples,
    }


async def _investigation_diagnostics(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    *,
    state: BrowserState,
) -> list[dict[str, Any]]:
    """Live PostgreSQL projections the browser spec asserts against."""
    async with uow_factory() as uow:
        investigation = await uow.analyst_investigations.get_for_run(state.production_run_id)
        if investigation is None:
            return []
        investigation_state = await uow.investigation_states.get(investigation.id)
        envelope = await uow.investigation_envelopes.get(investigation.id)
        plans = await uow.investigation_plans.list_for_investigation(investigation.id)
        head = await uow.investigation_plans.get_head(investigation.id)
        tasks = await uow.investigation_tasks.list_for_investigation(investigation.id)
        handoffs = await uow.investigation_handoffs.list_for_investigation(investigation.id)
        sessions = await uow.agent_sessions.list_for_investigation(investigation.id)
        decisions = await uow.analyst_decisions.list_for_investigation(investigation.id)
        binding = await uow.workspace_bindings.get_for_investigation(investigation.id)
    workspace: dict[str, Any] | None = None
    if binding is not None:
        workspace = {
            "workspace_id": binding.workspace_id,
            "delivery_code": binding.delivery_code,
            "article_slug": binding.article_slug,
            **_workspace_diagnostics(
                state.workspace_root,
                binding.delivery_code,
                binding.article_slug,
            ),
        }
    return [
        {
            "id": str(investigation.id),
            "prose_authority": investigation.prose_authority.value,
            "synthesis_artifact_id": str(investigation.synthesis_artifact_id),
            "execution_mode": (
                None if investigation_state is None else investigation_state.execution_mode.value
            ),
            "status": investigation.status.value,
            "version": None if investigation_state is None else investigation_state.version,
            "stop_reason": (
                None
                if investigation_state is None or investigation_state.stop_reason is None
                else investigation_state.stop_reason.value
            ),
            "envelope_version": None if envelope is None else envelope.version,
            "plan_count": len(plans),
            "current_plan_version": None if head is None else head.current_version,
            "task_count": len(tasks),
            "handoff_count": len(handoffs),
            "session_count": len(sessions),
            "decision_count": len(decisions),
            "decision_types": [decision.decision_type.value for decision in decisions],
            "tasks": [
                {
                    "id": str(task.id),
                    "task_key": task.task_key,
                    "title": task.title,
                    "status": task.status.value,
                    "status_version": task.status_version,
                    "priority": task.priority.value,
                    "assigned_actor_kind": (
                        None if task.assigned_actor_kind is None else task.assigned_actor_kind.value
                    ),
                    "assigned_actor_id": task.assigned_actor_id,
                    "lease_fence": task.lease_fence,
                    "lease_session_id": (
                        None if task.lease_session_id is None else str(task.lease_session_id)
                    ),
                    "handoff_ref": None if task.handoff_ref is None else str(task.handoff_ref),
                }
                for task in tasks
            ],
            "handoffs": [
                {
                    "handoff_id": record.handoff.handoff_id,
                    "task_id": record.handoff.task_id,
                    "version": record.handoff.version,
                }
                for record in handoffs
            ],
            "sessions": [
                {
                    "id": str(session.id),
                    "task_id": str(session.task_id),
                    "adapter_kind": session.adapter_kind.value,
                    "state": session.state.value,
                    "host_session_ref": session.host_session_ref,
                    "handoff_id": None if session.handoff_id is None else str(session.handoff_id),
                    "task_lease_fence": session.task_lease_fence,
                }
                for session in sessions
            ],
            "workspace": workspace,
        }
    ]


# ---------------------------------------------------------------------------
# Control routes
# ---------------------------------------------------------------------------


def make_shutdown_handler(server: Any) -> Callable[[], Awaitable[dict[str, str]]]:
    """Build the loopback shutdown route for a uvicorn server (unit-testable)."""

    async def request_shutdown() -> dict[str, str]:
        server.should_exit = True
        return {"status": "shutting_down"}

    return request_shutdown


def _service_or_fail(value: Any, name: str) -> Any:
    if value is None:
        raise RuntimeError(f"{name} is not wired; the browser server cannot serve this route")
    return value


def _install_routes(
    application: FastAPI,
    *,
    server_slot: list[uvicorn.Server | None],
    state: BrowserState,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    transport: AutoRespondingBridgeTransport,
    virus_total: AcceptanceVirusTotal,
    settings: Any,
    session_factory: Any,
    instruction_delivery: InstructionDeliveryService,
) -> None:
    @application.get("/api/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @application.get("/api/m24-browser/state")
    async def m24_browser_state() -> dict[str, Any]:
        payload = state.as_json()
        payload["fixtures"] = {
            **transport.fixtures(),
            "vt_calls": dict(virus_total.calls),
            "vt_download_calls": virus_total.download_calls,
            "vt_downloaded_hashes": list(virus_total.downloaded_hashes),
        }
        payload["investigations"] = await _investigation_diagnostics(uow_factory, state=state)
        return payload

    @application.post("/__m24_browser/drain-instructions")
    async def drain_instructions() -> dict[str, Any]:
        delivery = _service_or_fail(instruction_delivery, "instruction delivery")
        async with _instruction_drain_tick_lock(session_factory) as granted:
            if not granted:
                return {"granted": False, "drained": False}
            report = await delivery.drain_once(limit=settings.m24_instruction_drain_batch_size)
        return {"granted": True, "drained": True, **report.to_payload()}

    @application.post("/__m24_browser/shutdown")
    async def request_shutdown() -> dict[str, str]:
        server = server_slot[0]
        if server is None:
            return {"status": "shutdown_requested"}
        return await make_shutdown_handler(server)()


# ---------------------------------------------------------------------------
# Launcher
# ---------------------------------------------------------------------------


def _build_workspace_client(agent: Any) -> HttpWorkspaceAgentClient:
    return HttpWorkspaceAgentClient(
        socket_path=str(agent.socket_path),
        token_file=str(agent.token_file),
        timeout_seconds=60.0,
    )


def _build_instruction_delivery(
    *,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    session_runtime: Any,
    composition: Any,
    settings: Any,
) -> InstructionDeliveryService:
    """Mirror the lifespan's ID-2 service because ``lifespan="off"`` skips it."""
    host_sessions = _service_or_fail(session_runtime.host_sessions, "host sessions")
    return InstructionDeliveryService(
        uow_factory=cast(
            Callable[[], Any],
            uow_factory,
        ),
        host=host_sessions,
        notes=WorkspaceNoteCatalog(uow_factory=cast(Callable[[], TransferUnitOfWork], uow_factory)),
        disclosure=build_session_disclosure_checker(
            uow_factory=cast(
                Callable[[], SessionDisclosureUnitOfWork],
                uow_factory,
            ),
            article_snapshots=composition.article_snapshots,
            settings=settings,
            effective_policy_for=(
                composition.osint_open_policy_service.effective_for_investigation
            ),
        ),
        grace_seconds=settings.m24_instruction_delivery_grace_seconds,
        batch_size=settings.m24_instruction_drain_batch_size,
    )


async def _serve() -> None:
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    server_slot: list[uvicorn.Server | None] = [None]

    def request_termination() -> None:
        server = server_slot[0]
        if server is None:
            shutdown_requested.set()
        else:
            server.should_exit = True

    loop.add_signal_handler(signal.SIGINT, request_termination)
    loop.add_signal_handler(signal.SIGTERM, request_termination)

    env = validate_environment()
    admin_url = make_url(env.admin_dsn)
    database_name = f"cti_m24_browser_{uuid4().hex}"
    database_created = False
    engine = None
    patcher = _SettingsPatcher()
    workspace_client: WorkspaceAgentPort | None = None
    session_runtime: Any | None = None
    with ExitStack() as stack:
        try:
            await _create_database(env.admin_dsn, database_name)
            database_created = True
            if shutdown_requested.is_set():
                return
            database_dsn = admin_url.set(database=database_name).render_as_string(
                hide_password=False
            )
            from alembic import command

            await asyncio.to_thread(command.upgrade, _alembic_config(database_dsn), "head")
            if shutdown_requested.is_set():
                return

            engine = create_postgres_engine(database_dsn)
            session_factory = create_session_factory(engine)

            def uow_factory() -> SqlAlchemyUnitOfWork:
                return SqlAlchemyUnitOfWork(session_factory)

            # The H1 fake profiles must be registered before the sidecar's
            # supervisor snapshots its registry at agent start.
            install_fake_cli_profiles(patcher)
            agent = stack.enter_context(running_scenario_agent())
            if shutdown_requested.is_set():
                return
            material = ScenarioMaterial.synthetic()
            settings = acceptance_settings(agent)
            patcher.setattr(api_main, "get_settings", lambda: settings)

            blob_root = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="m24browser-blobs-"))
            )
            artifact_store = build_artifact_store(uow_factory, blob_root)
            transport = AutoRespondingBridgeTransport(material=material)
            virus_total = AcceptanceVirusTotal(material)
            composition = composition_factory(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
                settings=settings,
                transport=transport,
                virustotal=virus_total,
                external_effect_session_factory=session_factory,
            )
            application = create_app()
            application.state.identity_provider = StaticTokenIdentityProvider(env.operator_token)
            application.state.settings = settings
            application.state.uow_factory = uow_factory
            application.state.production_artifact_store = artifact_store
            application.state.investigation_composition_factory = composition
            workspace_client = _build_workspace_client(agent)
            application.state.workspace_agent_port = workspace_client
            session_runtime = build_session_runtime(application.state, settings)
            if not session_runtime.available:
                raise RuntimeError(
                    "the real host session runtime is unavailable: "
                    f"{session_runtime.unavailable_reason}"
                )
            application.state.agentic_operator_port = session_runtime.operator
            application.state.agent_session_secret_delivery = session_runtime.secret_delivery
            application.state.agent_session_runtime = session_runtime
            application.state.host_sessions = session_runtime.host_sessions
            runner = make_investigation_runner(uow_factory, composition)
            registry = create_job_registry(investigation_runner=runner)
            dispatcher = DeferredDispatcher()
            application.state.job_service = JobService(uow_factory, registry)
            application.state.job_dispatcher = dispatcher
            application.state.publication_render_service = build_publication_render_service(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
            )
            application.state.media_asset_store = MediaAssetStore(artifact_store, uow_factory)
            application.state.subject_service = SubjectService(uow_factory)
            application.state.subject_content_service = SubjectContentService(
                uow_factory, artifact_store
            )
            instruction_delivery = _build_instruction_delivery(
                uow_factory=uow_factory,
                session_runtime=session_runtime,
                composition=composition,
                settings=settings,
            )
            application.state.instruction_delivery_service = instruction_delivery

            seeded = await seed_article(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
                material=material,
            )
            state = BrowserState(
                subject_id=seeded.subject.id,
                edition_id=seeded.edition.id,
                production_run_id=seeded.run.id,
                synthesis_artifact_id=seeded.synthesis.id,
                synthesis_sha256=seeded.synthesis_sha256,
                publication_artifact_id=seeded.publication.id,
                publication_sha256=seeded.publication_sha256,
                subject_slug=seeded.subject.slug,
                database_name=database_name,
                api_port=env.api_port,
                workspace_root=agent.root,
                material=material,
            )
            state_json = state.as_json()
            if shutdown_requested.is_set():
                return
            await asyncio.to_thread(_write_state, env.state_path, state_json)

            _install_routes(
                application,
                server_slot=server_slot,
                state=state,
                uow_factory=uow_factory,
                transport=transport,
                virus_total=virus_total,
                settings=settings,
                session_factory=session_factory,
                instruction_delivery=instruction_delivery,
            )

            config = uvicorn.Config(
                application,
                host="127.0.0.1",
                port=env.api_port,
                log_level="info",
                access_log=False,
                lifespan="off",
            )
            server_slot[0] = uvicorn.Server(config)
            print(
                f"{READY_LINE_PREFIX}: subject={seeded.subject.id} "
                f"edition={seeded.edition.id} database={database_name} "
                f"port={env.api_port}",
                flush=True,
            )
            await server_slot[0].serve()
        finally:
            if workspace_client is not None:
                await workspace_client.aclose()
            if session_runtime is not None:
                await session_runtime.aclose()
            try:
                if engine is not None:
                    await engine.dispose()
            finally:
                if database_created:
                    await _drop_database(env.admin_dsn, database_name)
                patcher.undo()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
