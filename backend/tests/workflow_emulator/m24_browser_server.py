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
  queue.  A correction request (a prompt asking to fix the existing note) gets
  a revised two-paragraph answer, which the journey uses to prove the
  replacement proposal and its stale predecessor.  No live provider, CLI,
  network call or real CTI data is involved;
* Typst is the real pinned compiler; the journey renders and extracts a real
  PDF preview.

The launcher refuses to boot without the fake-provider attestation, a loopback
PostgreSQL DSN and a synthetic operator token of at least 32 ASCII characters.
The token is injected into the browser only through the Vite proxy's
``OPERATOR_API_TOKEN`` environment; it never appears in the state file, the
state route or the ready line.

Control routes (loopback only, outside ``/api`` so they carry no operator
token):

* ``GET /api/m24-browser/state`` — seeded ids, material, fixture counters,
  live PostgreSQL diagnostics and the host runs the browser spec asserts
  against (through the Vite proxy);
* ``POST /__m24_browser/drain-instructions`` — one real ID-2 instruction drain
  tick, because ``lifespan="off"`` disables the background loop;
* ``POST /__m24_browser/signed-session-action`` — play the host shim for the
  D7 signed routes (tool calls, instruction pull/ack).  The per-session secret
  is derived **inside this process** from the acceptance key and the session
  nonce; it is never returned, logged, traced or given to the browser.  The
  spec only receives the real route's HTTP status and body;
* ``POST /__m24_browser/shutdown`` — clean uvicorn stop.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import ipaddress
import json
import os
import re
import signal
import tempfile
from collections.abc import Awaitable, Callable
from contextlib import ExitStack
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import uvicorn
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy.engine import make_url

from cti_app.api import main as api_main
from cti_app.api.main import _instruction_drain_tick_lock, create_app
from cti_app.application.agent_session_disclosure import (
    SessionDisclosureUnitOfWork,
    build_session_disclosure_checker,
)
from cti_app.application.agent_session_verifier import build_agent_session_message
from cti_app.application.agentic_host_sessions import HostSessionsError
from cti_app.application.bridge_investigator import (
    ScriptedTurnTransport,
    TurnSubmission,
    make_investigation_turn_idempotency_key,
)
from cti_app.application.identity import StaticTokenIdentityProvider
from cti_app.application.instruction_delivery import InstructionDeliveryService
from cti_app.application.investigation_composition import OptionalInvestigationToolPorts
from cti_app.application.investigation_jobs import make_investigation_runner
from cti_app.application.investigation_transfers import (
    TransferUnitOfWork,
    WorkspaceNoteCatalog,
)
from cti_app.application.jobs import JobService, create_job_registry
from cti_app.application.media_assets import MediaAssetStore
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.shodan import ShodanBudgetEstimate, ShodanCapabilities
from cti_app.application.subject_content import SubjectContentService
from cti_app.application.subjects import SubjectService
from cti_app.application.workspace_agent import WorkspaceAgentError, WorkspaceAgentPort
from cti_app.domain.analyst_opinion import (
    investigation_opinion_id,
    note_content_blocks_to_payload,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication_document import (
    PublicationDocumentV6,
    PublicationDocumentV6ProvenanceV1,
    PublicationDocumentV9,
    publication_document_from_json,
    publication_document_text_anchors,
    publication_document_v5_sha256,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import (
    SemanticParagraphV1,
    SemanticRole,
    SemanticTextSpanV1,
)
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
    session_secret,
)
from tests.integration.conftest import _alembic_config, _create_database, _drop_database
from tests.shodan_support import FakeShodanPort
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


def two_paragraph_note(unit_label: str, *, revised: bool = False) -> list[dict[str, str]]:
    """One stored Bridge sentence that carries a real internal paragraph break.

    ``revised`` answers a correction request: the text differs from the first
    draft so the journey can prove a replacement proposal and its diff.  The
    label is reduced to a short token: a UUID would wrap inside the rendered
    PDF and make a placement assertion depend on line breaking.
    """
    token = re.sub(r"[^A-Za-z0-9]", "", unit_label)[:8] or "U1"
    prefix = "Reponse synthetique corrigee" if revised else "Reponse synthetique"
    first = f"{prefix} pour {token} : premier fait verifie sur les octets."
    second = (
        f"Second paragraphe synthetique pour {token} : la continuite "
        "du saut de paragraphe est conservee telle quelle."
        + (" Le texte a ete corrige apres revue humaine." if revised else "")
    )
    # One stored sentence carrying the internal break: the real response parser
    # strips a sentence's outer whitespace, so a separate ``\n\n``-prefixed
    # sentence would lose its break (and be dropped) instead of surviving.
    return [{"text": f"{first}\n\n{second}"}]


_CORRECTION_MARKER = "Corrige la note"


def advisory_text_for(consultation_type: str) -> str:
    """The canonical advisory wire, declared under the requested type.

    The parser compares the wire's ``TYPE:`` header with the requested
    consultation type; a fixed ``pivot_strategy`` header makes every critique
    read as unparsed.  The demonstration ``refs=E1`` alias is dropped because
    this journey cites no evidence alias it could resolve; an unresolved alias
    downgrades the answer to ``partial`` and a critique must be provably
    received and parsed.
    """
    text = advisory_consult_text().replace(" | refs=E1", "")
    if consultation_type and f"TYPE: {consultation_type}" not in text:
        text = text.replace("TYPE: pivot_strategy", f"TYPE: {consultation_type}", 1)
    return text


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
                if consultation_type == "critique":
                    # Keep a critique observably in flight so the cockpit's
                    # "envoyée — en cours" state is provable before "reçue".
                    await asyncio.sleep(0.75)
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
        return self._submission(advisory_text_for(consultation_type))

    @staticmethod
    def _submission(output_text: str) -> TurnSubmission:
        # The acceptance helper is untyped; its runtime contract is exact.  A
        # note proposal may only materialise from a consultation that carries
        # real conversation-turn provenance, including a Bridge invocation id.
        return cast(
            TurnSubmission,
            bridge_submission(output_text, bridge_invocation_id=uuid4()),
        )

    def _draft_payload(self, prompt: str) -> dict[str, Any]:
        targets = prompt_canonical_targets(prompt)
        revised = _CORRECTION_MARKER in prompt
        # The canonical unit id is the same fixed alias for every request; the
        # prompt itself is unique per anchor and intent, so its digest gives
        # each answer a stable short token the PDF assertions can locate.
        note_token = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:8]
        blocks: list[dict[str, Any]] = []
        for index, target in enumerate(targets, start=1):
            unit_id = str(target.get("id") or target.get("unit_id") or f"U{index}")
            blocks.append(
                {
                    "unit_id": unit_id,
                    "block_type": target.get("block_type"),
                    "form": target.get("form", "analyst_note"),
                    "anchor": target.get("anchor"),
                    "sentences": two_paragraph_note(note_token, revised=revised),
                }
            )
        if not blocks:
            blocks = [
                {
                    "block_type": "key_findings",
                    "form": "body",
                    "anchor": None,
                    "sentences": two_paragraph_note(note_token, revised=revised),
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


async def _advance_publication_base(
    *,
    state: BrowserState,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    artifact_store: ProductionArtifactStore,
) -> dict[str, Any]:
    """Append a synthetic publication revision to prove proposal base fencing."""
    async with uow_factory() as uow:
        current = await uow.production_artifacts.get_current_for_revision(
            state.production_run_id,
            ProductionArtifactStage.PUBLICATION.value,
        )
    if current is None or current.canonical_blob_id is None:
        raise RuntimeError("the M24 browser publication base is unavailable")
    raw = await artifact_store.read_bytes(current.canonical_blob_id, max_bytes=None)
    try:
        document = publication_document_from_json(json.loads(raw))
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise RuntimeError("the M24 browser publication base is not canonical") from exc
    if not isinstance(document, PublicationDocumentV9):
        raise RuntimeError("the M24 browser publication base must be an M23 V9 document")

    old_v7 = document.base_publication
    old_v6 = old_v7.base_publication
    new_artifact_id = uuid4()
    old_base_v5 = old_v6.base_publication
    target_section = old_base_v5.document.sections[1]
    target_paragraph = target_section.paragraphs[0]
    revised_paragraph_text = f"{target_paragraph.text} Revision D2."
    if not old_base_v5.document.tables:
        raise RuntimeError("the M24 browser publication has no D2 source table")
    target_table = old_base_v5.document.tables[0]
    if target_table.key != "d2-article-a-table":
        raise RuntimeError("the M24 browser publication source table key changed")
    revised_sections = (
        *old_base_v5.document.sections[:1],
        replace(
            target_section,
            paragraphs=(
                replace(target_paragraph, text=revised_paragraph_text),
                *target_section.paragraphs[1:],
            ),
        ),
        *old_base_v5.document.sections[2:],
    )
    revised_document_v4 = replace(
        old_base_v5.document,
        sections=revised_sections,
        tables=(
            replace(target_table, title=f"{target_table.title} Revision D2."),
            *old_base_v5.document.tables[1:],
        ),
    )
    new_base_v5 = replace(
        old_base_v5,
        document=revised_document_v4,
        semantic_text=replace(
            old_base_v5.semantic_text,
            paragraphs=tuple(
                SemanticParagraphV1(
                    anchor=anchor,
                    spans=(SemanticTextSpanV1(SemanticRole.TEXT, text),),
                )
                for anchor, text in publication_document_text_anchors(
                    revised_document_v4,
                    references=old_base_v5.references,
                ).items()
            ),
        ),
    )
    base_body_sha256 = publication_document_v5_sha256(new_base_v5)
    new_v6 = PublicationDocumentV6(
        base_publication=new_base_v5,
        provenance=PublicationDocumentV6ProvenanceV1(
            base_publication_artifact_id=new_artifact_id,
            base_publication_sha256=base_body_sha256,
            base_publication_body_sha256=base_body_sha256,
        ),
    )
    revised_document = replace(
        document,
        base_publication=replace(old_v7, base_publication=new_v6),
    )
    revised_bytes = ProductionArtifactStore.canonical_json_bytes(
        serialize_publication_document(revised_document)
    )
    revised_sha256 = hashlib.sha256(revised_bytes).hexdigest()
    revised_blob_id = await artifact_store.put_bytes(
        revised_bytes,
        bucket="m24-acceptance-publication",
        mime_type="application/json",
    )
    revised_artifact = ProductionArtifact(
        id=new_artifact_id,
        production_run_id=current.production_run_id,
        subject_id=current.subject_id,
        stage=ProductionArtifactStage.PUBLICATION,
        version=current.version + 1,
        input_hash=revised_sha256,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=revised_blob_id,
    )
    async with uow_factory() as uow:
        latest = await uow.production_artifacts.get_current_for_revision(
            state.production_run_id,
            ProductionArtifactStage.PUBLICATION.value,
        )
        if latest is None or latest.id != current.id:
            raise RuntimeError("the publication base changed during the D2 fixture update")
        await uow.production_artifacts.append(revised_artifact)
        await uow.commit()
    return {
        "artifact_id": str(revised_artifact.id),
        "version": revised_artifact.version,
        "sha256": revised_sha256,
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


def _isoformat(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


async def _investigation_diagnostics(
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    *,
    state: BrowserState,
    host_sessions: Any | None = None,
) -> list[dict[str, Any]]:
    """Live PostgreSQL projections the browser spec asserts against.

    Only identities, states, hashes and timestamps are published: no operator
    token, session secret, nonce or instruction body ever reaches this route.
    """
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
        instructions: dict[str, list[dict[str, Any]]] = {}
        for session in sessions:
            rows = await uow.agent_sessions.list_instructions(session.id)
            instructions[str(session.id)] = [
                {
                    "instruction_id": str(row.id),
                    "state": row.state.value,
                    "text_sha256": row.text_sha256,
                    "queued_at": _isoformat(row.queued_at),
                    "delivered_at": _isoformat(row.delivered_at),
                    "acknowledged_at": _isoformat(row.acknowledged_at),
                    "delivery_attempts": row.delivery_attempts,
                    "delivery_mode": (
                        None if row.delivery_mode is None else row.delivery_mode.value
                    ),
                    "last_delivery_error": row.last_delivery_error,
                    "cancelled_at": _isoformat(row.cancelled_at),
                    "cancel_reason": row.cancel_reason,
                }
                for row in rows
            ]
        proposals = await uow.investigation_note_proposals.list_for_investigation(investigation.id)
        opinion = await uow.analyst_opinions.get(investigation_opinion_id(investigation.id))
        contributions = await uow.work_contributions.list_for_investigation(investigation.id)
        invocations = await uow.tool_invocations.list_for_investigation(investigation.id)
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
    host_runs: dict[str, dict[str, Any] | None] = {}
    if host_sessions is not None:
        for session in sessions:
            try:
                status = await host_sessions.get_session_status(session.id)
            except (HostSessionsError, WorkspaceAgentError):
                host_runs[str(session.id)] = None
            else:
                host_runs[str(session.id)] = {
                    "state": status.state,
                    "runs": len(status.runs),
                    "exit": (
                        None
                        if status.exit is None
                        else {
                            "code": status.exit.code,
                            "signal": status.exit.signal,
                        }
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
            "envelope_allowed_providers": (
                None if envelope is None else list(envelope.allowed_providers)
            ),
            "envelope_external_llm_allowed": (
                None if envelope is None else envelope.disclosure.external_llm_allowed
            ),
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
                    "result_verification_status": task.result_verification_status.value,
                    "result_refs": [dict(reference) for reference in task.result_refs],
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
                    "host": host_runs.get(str(session.id)),
                    "instructions": instructions.get(str(session.id), []),
                }
                for session in sessions
            ],
            "note_proposals": [
                {
                    "proposal_id": str(proposal.id),
                    "anchor": proposal.anchor,
                    "status": proposal.status.value,
                    "status_version": proposal.status_version,
                    "proposed_text": proposal.proposed_text,
                    "proposed_text_sha256": proposal.proposed_text_sha256,
                    "bridge_block_type": proposal.bridge_block_type.value,
                    "target_block_type": proposal.target_block_type.value,
                    "base_opinion_version": proposal.base_opinion_version,
                    "base_opinion_sha256": proposal.base_opinion_sha256,
                    "base_publication_artifact_id": (
                        None
                        if proposal.base_publication_artifact_id is None
                        else str(proposal.base_publication_artifact_id)
                    ),
                    "base_publication_version": proposal.base_publication_version,
                    "base_publication_sha256": proposal.base_publication_sha256,
                    "content_blocks": note_content_blocks_to_payload(proposal.content_blocks),
                    "materialised": proposal.materialised,
                    "task_id": None if proposal.task_id is None else str(proposal.task_id),
                }
                for proposal in proposals
            ],
            "opinion": (
                None
                if opinion is None
                else {
                    "opinion_id": str(opinion.id),
                    "version": opinion.version,
                    "content_sha256": opinion.content_sha256,
                    "base_publication_sha256": opinion.base_publication_sha256,
                    "observations": [
                        {
                            "note_id": str(note.id),
                            "anchor": note.anchor,
                            "text": note.text,
                            "content_blocks": note_content_blocks_to_payload(note.content_blocks),
                        }
                        for note in opinion.observations
                    ],
                }
            ),
            "contributions": [
                {
                    "contribution_id": str(contribution.id),
                    "category": contribution.category.value,
                    "import_status": contribution.import_status.value,
                    "file_relpath": contribution.file_relpath,
                    "file_sha256": contribution.file_sha256,
                    "observation_id": (
                        None
                        if contribution.observation_id is None
                        else str(contribution.observation_id)
                    ),
                    "finding_id": (
                        None if contribution.finding_id is None else str(contribution.finding_id)
                    ),
                }
                for contribution in contributions
            ],
            # The tool-invocation journal is the PostgreSQL line behind every
            # signed call and every human consultation; only attribution and
            # status are published (never parameters or output text).
            "tool_invocations": [
                {
                    "invocation_id": str(invocation.id),
                    "tool_name": invocation.tool_name,
                    "status": invocation.status.value,
                    "task_id": None if invocation.task_id is None else str(invocation.task_id),
                    "agent_session_id": (
                        None
                        if invocation.agent_session_id is None
                        else str(invocation.agent_session_id)
                    ),
                    "context_task_id": (
                        None
                        if invocation.context_task_id is None
                        else str(invocation.context_task_id)
                    ),
                    "actor_kind": (
                        None if invocation.actor_kind is None else invocation.actor_kind.value
                    ),
                    "actor_id": invocation.actor_id,
                }
                for invocation in invocations
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


def _signed_session_headers(
    *,
    secret: bytes,
    session_id: UUID,
    path_and_query: str,
    body: bytes,
    idempotency_key: str | None,
) -> dict[str, str]:
    """Build the exact D7 host-shim headers around a raw body.

    The secret is a synthetic per-session value derived in this process; it is
    never returned by a route, written to the state file or logged.
    """
    timestamp = str(int(datetime.now(UTC).timestamp()))
    message = build_agent_session_message(
        method="POST",
        path_and_query=path_and_query,
        session_id=session_id,
        body=body,
        idempotency_key=idempotency_key,
        timestamp=timestamp,
    )
    headers = {
        "Content-Type": "application/json",
        "X-AutoWork-Agent-Session": str(session_id),
        "X-AutoWork-Agent-Timestamp": timestamp,
        "X-AutoWork-Agent-Signature": "v1=" + hmac.new(secret, message, hashlib.sha256).hexdigest(),
    }
    if idempotency_key is not None:
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _signed_action_request(
    body: dict[str, Any],
) -> tuple[str, str | None, str]:
    """Resolve a control-body into the signed route body, key and kind.

    Two kinds are exposed because the host shim calls two real routes with the
    same D7 scheme: the brokered tool catalogue and the fixed pull/ack route.
    Anything else is a test programming error and is refused before signing.
    """
    kind = body.get("kind")
    if kind == "tool_call":
        tool_name = body.get("tool_name")
        schema_version = body.get("schema_version")
        arguments = body.get("arguments", {})
        idempotency_key = body.get("idempotency_key")
        if (
            not isinstance(tool_name, str)
            or not tool_name
            or not isinstance(schema_version, str)
            or not schema_version
            or not isinstance(arguments, dict)
            or not isinstance(idempotency_key, str)
            or not idempotency_key
        ):
            raise ValueError("tool_call requires tool_name, schema_version, arguments and a key")
        payload: dict[str, Any] = {
            "tool_name": tool_name,
            "schema_version": schema_version,
            "arguments": arguments,
        }
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            idempotency_key,
            "tool_call",
        )
    if kind == "instruction":
        action = body.get("action")
        if action == "ack":
            instruction_ids = body.get("instruction_ids")
            if not isinstance(instruction_ids, list) or not instruction_ids:
                raise ValueError("an ack requires a non-empty instruction_ids list")
            payload = {"action": "ack", "instruction_ids": instruction_ids}
        elif action == "pull":
            payload = {"action": "pull"}
        else:
            raise ValueError("instruction action must be ack or pull")
        return (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            None,
            "instruction",
        )
    raise ValueError("kind must be tool_call or instruction")


def _install_routes(
    application: FastAPI,
    *,
    server_slot: list[uvicorn.Server | None],
    state: BrowserState,
    uow_factory: Callable[[], SqlAlchemyUnitOfWork],
    artifact_store: ProductionArtifactStore,
    transport: AutoRespondingBridgeTransport,
    virus_total: AcceptanceVirusTotal,
    shodan: FakeShodanPort,
    settings: Any,
    session_factory: Any,
    instruction_delivery: InstructionDeliveryService,
    host_sessions: Any | None,
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
            "shodan_calls": list(shodan.calls),
        }
        payload["investigations"] = await _investigation_diagnostics(
            uow_factory, state=state, host_sessions=host_sessions
        )
        return payload

    @application.post("/__m24_browser/advance-publication")
    async def advance_publication() -> dict[str, Any]:
        return await _advance_publication_base(
            state=state,
            uow_factory=uow_factory,
            artifact_store=artifact_store,
        )

    @application.post("/__m24_browser/drain-instructions")
    async def drain_instructions() -> dict[str, Any]:
        delivery = _service_or_fail(instruction_delivery, "instruction delivery")
        async with _instruction_drain_tick_lock(session_factory) as granted:
            if not granted:
                return {"granted": False, "drained": False}
            report = await delivery.drain_once(limit=settings.m24_instruction_drain_batch_size)
        return {"granted": True, "drained": True, **report.to_payload()}

    @application.post("/__m24_browser/signed-session-action")
    async def signed_session_action(body: dict[str, Any]) -> dict[str, Any]:
        """Play the host shim for one D7 signed call without exposing the secret."""
        try:
            session_id = UUID(str(body.get("session_id")))
            investigation_id = UUID(str(body.get("investigation_id")))
            raw_body, idempotency_key, kind = _signed_action_request(body)
        except (TypeError, ValueError) as exc:
            return {"status_code": 422, "body": {"detail": str(exc)}}
        secret = await session_secret(uow_factory, session_id)
        if kind == "tool_call":
            path = f"/api/investigations/{investigation_id}/agent-sessions/{session_id}/tool-calls"
        else:
            path = (
                f"/api/investigations/{investigation_id}/agent-sessions/{session_id}/instructions"
            )
        headers = _signed_session_headers(
            secret=secret,
            session_id=session_id,
            path_and_query=path,
            body=raw_body.encode("utf-8"),
            idempotency_key=idempotency_key,
        )
        asgi_transport = httpx.ASGITransport(app=application)
        async with httpx.AsyncClient(
            transport=asgi_transport, base_url="http://m24-browser.internal"
        ) as client:
            response = await client.post(path, content=raw_body.encode("utf-8"), headers=headers)
        try:
            payload: Any = response.json()
        except json.JSONDecodeError:
            payload = {"text": response.text}
        return {"status_code": response.status_code, "body": payload}

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
            # supervisor snapshots its registry at agent start.  The ``success``
            # scenario ends each run cleanly, which is the host's technical
            # receive point for a queued session instruction (ID-2 drain).
            install_fake_cli_profiles(patcher, scenario="success")
            agent = stack.enter_context(running_scenario_agent())
            if shutdown_requested.is_set():
                return
            material = ScenarioMaterial.synthetic()
            settings = acceptance_settings(agent).model_copy(
                update={
                    # A non-functional local test key enables registration of
                    # the deterministic Shodan fake; the adapter never opens
                    # an HTTP connection in this browser harness.
                    "shodan_api_key": SecretStr("m24-synthetic-shodan-key"),
                    "shodan_search_enabled": True,
                }
            )
            patcher.setattr(api_main, "get_settings", lambda: settings)

            blob_root = Path(
                stack.enter_context(tempfile.TemporaryDirectory(prefix="m24browser-blobs-"))
            )
            artifact_store = build_artifact_store(uow_factory, blob_root)
            transport = AutoRespondingBridgeTransport(material=material)
            virus_total = AcceptanceVirusTotal(material)
            shodan = FakeShodanPort()
            composition = composition_factory(
                uow_factory=uow_factory,
                artifact_store=artifact_store,
                settings=settings,
                transport=transport,
                virustotal=virus_total,
                shodan=shodan,
                additional_ports=OptionalInvestigationToolPorts(
                    shodan_capabilities=ShodanCapabilities(search=True),
                    shodan_budget_estimate=ShodanBudgetEstimate(search=1),
                ),
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
                # Two rendered anchors share the tail text: the note journey
                # proves the canonical selector exposes real occurrences.
                duplicate_analysis_paragraph=True,
                include_m23_article_elements=True,
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
                artifact_store=artifact_store,
                transport=transport,
                virus_total=virus_total,
                shodan=shodan,
                settings=settings,
                session_factory=session_factory,
                instruction_delivery=instruction_delivery,
                host_sessions=session_runtime.host_sessions,
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
