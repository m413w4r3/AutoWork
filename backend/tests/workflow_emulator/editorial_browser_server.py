"""Serve the M23 editorial workshop against isolated PostgreSQL and a fake provider.

This follows the W10 browser-server pattern: create a throwaway database on the
local integration PostgreSQL instance, migrate it to head, seed a READY
production run with the shared F5 acceptance builder, and serve the real
FastAPI application on loopback.

No live provider is reachable from this process:

* the model gateway is the acceptance builder's ``_NoModelCalls`` double, which
  raises on any attribute use;
* the application lifespan (MinIO, Redis, Dramatiq, VT/Shodan/Bridge clients) is
  disabled, so none of those clients are constructed;
* the launcher refuses to boot without an explicit fake-provider attestation and
  a loopback PostgreSQL DSN.

The seeded document is the F5 acceptance fixture: six paragraphs, one archived
table and four archived figures. The fixture routes to BRIEF because the same
builder's ARTICLE routing does not also reshape the synthesis into the ARTICLE
overview outline; visual revisions are identical under both kinds and a BRIEF
still allows the three integrated figures the flows need. The figures are
integrated through the real manual figure-proposal routes so the browser flows
start from a canonical state; the fourth stays available for replacement.
"""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import uvicorn
from sqlalchemy.engine import make_url

from cti_app.application.subjects import SubjectService
from cti_app.domain.publication_kind import PublicationKind
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config, _create_database, _drop_database
from tests.integration.editorial.acceptance_support import seed_acceptance_workspace

ATTESTATION_ENV = "EDITORIAL_REAL_LITE_ATTESTED_FAKE_PROVIDER"
DEFAULT_API_PORT = 18767
BASE_URL = "http://editorial.test"
PUBLICATION_KIND = PublicationKind.BRIEF

# The acceptance builder archives four figures. Captions are the editorial text
# the browser asserts on; the figure catalog order is the source order.
FIGURE_CAPTIONS: tuple[str, ...] = (
    "Figure alpha archivée",
    "Figure beta archivée",
    "Figure gamma archivée",
    "Figure delta archivée",
)
# Two figures share paragraph four so the reorder flow has a real group; the
# first moves to paragraph five in the move flow; the third is removed; the
# second is replaced with the spare fourth archive entry.
FIGURES_AFTER_PARAGRAPH_TWO = 1
FIGURES_AFTER_PARAGRAPH_FOUR = 3


class _SettingsPatcher:
    """Minimal ``pytest.MonkeyPatch`` lookalike for the acceptance builder.

    ``seed_acceptance_workspace`` only uses ``setattr`` before creating the
    application, then the launcher keeps the patched settings for its lifetime.
    """

    def __init__(self) -> None:
        self._restorations: list[tuple[object, str, object]] = []

    def setattr(self, target: object, name: str, value: object) -> None:
        self._restorations.append((target, name, getattr(target, name)))
        setattr(target, name, value)

    def undo(self) -> None:
        while self._restorations:
            target, name, previous = self._restorations.pop()
            setattr(target, name, previous)


@dataclass(frozen=True)
class _BrowserState:
    subject_id: UUID
    edition_id: UUID
    production_run_id: UUID
    database_name: str
    api_port: int
    paragraphs: tuple[dict[str, str], ...]
    figures: tuple[dict[str, str], ...]
    table: dict[str, str | None]
    spare_figure: dict[str, str]
    snapshot_id: str
    enrichment_version: int
    publication_version: int

    def as_json(self) -> dict[str, Any]:
        return {
            "subject_id": str(self.subject_id),
            "edition_id": str(self.edition_id),
            "production_run_id": str(self.production_run_id),
            "database_name": self.database_name,
            "api_port": self.api_port,
            "publication_kind": PUBLICATION_KIND.value,
            "paragraphs": list(self.paragraphs),
            "figures": list(self.figures),
            "table": self.table,
            "spare_figure": self.spare_figure,
            "snapshot_id": self.snapshot_id,
            "enrichment_version": self.enrichment_version,
            "publication_version": self.publication_version,
        }


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
        raise RuntimeError(f"{name} is required for the editorial browser server")
    return value


def _api_port() -> int:
    raw = os.environ.get("WORKFLOW_BROWSER_API_PORT", str(DEFAULT_API_PORT))
    try:
        port = int(raw)
    except ValueError as exc:
        raise RuntimeError("WORKFLOW_BROWSER_API_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("WORKFLOW_BROWSER_API_PORT must be between 1 and 65535")
    return port


def validate_environment() -> tuple[str, Path, int]:
    """Validate the harness contract before touching PostgreSQL or the app."""
    if os.environ.get(ATTESTATION_ENV) != "1":
        raise RuntimeError(
            f"{ATTESTATION_ENV}=1 is required: the backend uses a fake model provider "
            "and no live provider credentials"
        )
    admin_dsn = _required_env("TEST_POSTGRES_ADMIN_DSN")
    admin_url = make_url(admin_dsn)
    if not _is_loopback_host(admin_url.host):
        raise RuntimeError(
            "the editorial browser server requires TEST_POSTGRES_ADMIN_DSN "
            "to target local PostgreSQL"
        )
    state_path = Path(_required_env("WORKFLOW_BROWSER_STATE"))
    return admin_dsn, state_path, _api_port()


async def _get_json(response: httpx.Response) -> dict[str, Any]:
    if response.status_code != 200:
        raise RuntimeError(
            f"editorial seed request failed: {response.request.method} "
            f"{response.request.url} -> {response.status_code} {response.text}"
        )
    payload = response.json()
    if not isinstance(payload, dict):
        raise RuntimeError("editorial seed request returned a non-object payload")
    return payload


async def _snapshot(client: httpx.AsyncClient, subject_id: UUID) -> dict[str, Any]:
    return await _get_json(await client.get(f"/api/subjects/{subject_id}/editor/snapshot"))


async def _available_figures(client: httpx.AsyncClient, subject_id: UUID) -> list[dict[str, Any]]:
    payload = await _get_json(
        await client.get(f"/api/subjects/{subject_id}/editor/figures?status=available")
    )
    items = payload.get("items")
    if not isinstance(items, list):
        raise RuntimeError("editorial figure catalog did not return an items list")
    return [item for item in items if isinstance(item, dict)]


def _paragraphs(snapshot: dict[str, Any]) -> list[dict[str, Any]]:
    blocks = snapshot.get("blocks")
    if not isinstance(blocks, list):
        raise RuntimeError("editorial snapshot did not return blocks")
    return [
        block
        for block in blocks
        if isinstance(block, dict) and block.get("kind") in {"paragraph", "lead_paragraph"}
    ]


async def _accept(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    occurrence_id: str,
    proposal: dict[str, Any],
) -> None:
    response = await client.post(
        f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/proposals/"
        f"{proposal['id']}/accept",
        headers={"Idempotency-Key": f"editorial-browser-seed-{uuid4().hex}"},
        json={
            "expected_state_version": proposal["state_version"],
            "patch_sha256": proposal["patch_sha256"],
        },
    )
    await _get_json(response)


async def _insert_figure(
    client: httpx.AsyncClient,
    *,
    subject_id: UUID,
    snapshot: dict[str, Any],
    anchor_block_id: str,
    figure_id: str,
    caption: str,
) -> None:
    proposal = await _get_json(
        await client.post(
            f"/api/subjects/{subject_id}/editor/blocks/{anchor_block_id}/figure-proposals",
            headers={"Idempotency-Key": f"editorial-browser-figure-{uuid4().hex}"},
            json={
                "snapshot_id": snapshot["snapshot_id"],
                "anchor_block_id": anchor_block_id,
                "figures": [{"figure_id": figure_id, "caption": caption, "order": 0}],
            },
        )
    )
    await _accept(
        client,
        subject_id=subject_id,
        occurrence_id=anchor_block_id,
        proposal=proposal,
    )


async def _seed_figures(
    app: Any, subject_id: UUID
) -> tuple[dict[str, Any], dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Integrate three archived figures through the real manual routes."""
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url=BASE_URL
    ) as client:
        snapshot = await _snapshot(client, subject_id)
        paragraphs = _paragraphs(snapshot)
        if len(paragraphs) != 6:
            raise RuntimeError(
                f"the acceptance fixture must expose six paragraphs, found {len(paragraphs)}"
            )
        # The acceptance builder archives exactly four figures in source order.
        # The catalog keeps that order; names come from the synthetic HTML alt
        # text and are only an opaque label to the browser suite.
        catalog_items = await _available_figures(client, subject_id)
        if len(catalog_items) != 4:
            raise RuntimeError(
                "the acceptance fixture must archive four figures, found "
                f"{[item.get('original_name') for item in catalog_items]}"
            )
        first, second, third, spare_item = catalog_items
        for anchor_index, item, caption in (
            (FIGURES_AFTER_PARAGRAPH_TWO, first, FIGURE_CAPTIONS[0]),
            (FIGURES_AFTER_PARAGRAPH_FOUR, second, FIGURE_CAPTIONS[1]),
            (FIGURES_AFTER_PARAGRAPH_FOUR, third, FIGURE_CAPTIONS[2]),
        ):
            await _insert_figure(
                client,
                subject_id=subject_id,
                snapshot=snapshot,
                anchor_block_id=str(paragraphs[anchor_index]["block_id"]),
                figure_id=str(item["figure_id"]),
                caption=caption,
            )
            snapshot = await _snapshot(client, subject_id)
        available = await _available_figures(client, subject_id)
        if [str(item.get("figure_id")) for item in available] != [str(spare_item["figure_id"])]:
            raise RuntimeError(
                "exactly the spare archived figure must remain available, found "
                f"{[item.get('original_name') for item in available]}"
            )
        catalog = {
            str(item.get("original_name")): item for item in (first, second, third, spare_item)
        }
        return (
            snapshot,
            catalog,
            {str(spare_item["original_name"]): spare_item},
        )


def _browser_state(
    *,
    snapshot: dict[str, Any],
    figures: dict[str, dict[str, Any]],
    spare: dict[str, dict[str, Any]],
    subject_id: UUID,
    edition_id: UUID,
    production_run_id: UUID,
    database_name: str,
    api_port: int,
) -> _BrowserState:
    enrichments = snapshot.get("enrichments")
    if not isinstance(enrichments, list):
        raise RuntimeError("editorial snapshot did not return enrichments")
    figure_rows: list[dict[str, str]] = []
    for block in enrichments:
        if not isinstance(block, dict) or block.get("kind") != "figure":
            continue
        figure = block.get("figure")
        if not isinstance(figure, dict):
            raise RuntimeError("figure enrichment block has no projection")
        caption = str(figure.get("caption") or block.get("caption") or "")
        source_name = next(
            (
                name
                for name, item in figures.items()
                if str(item.get("figure_id")) == str(figure.get("catalog_figure_id"))
            ),
            None,
        )
        if source_name is None:
            raise RuntimeError(f"figure projection has an unknown catalog id: {figure}")
        figure_rows.append(
            {
                "original_name": source_name,
                "caption": caption,
                "element_key": str(block["element_key"]),
                "block_id": str(block["block_id"]),
                "anchor_block_id": str(figure.get("anchor_block_id") or ""),
            }
        )
    expected = sorted(FIGURE_CAPTIONS[:3])
    if sorted(row["caption"] for row in figure_rows) != expected:
        raise RuntimeError(f"seeded figures do not match the expected captions: {figure_rows}")
    tables = [
        block for block in enrichments if isinstance(block, dict) and block.get("kind") == "table"
    ]
    table = tables[0] if tables else None
    spare_name, spare_item = next(iter(spare.items()))
    return _BrowserState(
        subject_id=subject_id,
        edition_id=edition_id,
        production_run_id=production_run_id,
        database_name=database_name,
        api_port=api_port,
        paragraphs=tuple(
            {
                "block_id": str(block["block_id"]),
                "kind": str(block["kind"]),
                "text": str(block.get("text") or ""),
            }
            for block in _paragraphs(snapshot)
        ),
        figures=tuple(figure_rows),
        table={
            "element_key": str(table["element_key"]) if table else None,
            "block_id": str(table["block_id"]) if table else None,
        },
        spare_figure={
            "original_name": spare_name,
            "figure_id": str(spare_item["figure_id"]),
        },
        snapshot_id=str(snapshot["snapshot_id"]),
        enrichment_version=int(snapshot["enrichment_version"]),
        publication_version=int(snapshot["publication_version"]),
    )


def _write_state(path: Path, state: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2) + "\n", encoding="utf-8")


async def _serve() -> None:
    loop = asyncio.get_running_loop()
    shutdown_requested = asyncio.Event()
    server: uvicorn.Server | None = None

    def request_termination() -> None:
        if server is None:
            shutdown_requested.set()
        else:
            server.should_exit = True

    loop.add_signal_handler(signal.SIGINT, request_termination)
    loop.add_signal_handler(signal.SIGTERM, request_termination)

    admin_dsn, state_path, port = validate_environment()
    admin_url = make_url(admin_dsn)
    database_name = f"cti_editorial_browser_{uuid4().hex}"
    database_created = False
    engine = None
    patcher = _SettingsPatcher()
    try:
        await _create_database(admin_dsn, database_name)
        database_created = True
        if shutdown_requested.is_set():
            return
        database_dsn = admin_url.set(database=database_name).render_as_string(hide_password=False)
        from alembic import command

        await asyncio.to_thread(command.upgrade, _alembic_config(database_dsn), "head")
        if shutdown_requested.is_set():
            return

        engine = create_postgres_engine(database_dsn)
        session_factory = create_session_factory(engine)

        def uow_factory() -> SqlAlchemyUnitOfWork:
            return SqlAlchemyUnitOfWork(session_factory)

        # The acceptance builder uses its own in-memory blob store, so nothing
        # outside this process (MinIO, filesystem, network) is touched.
        seed = await seed_acceptance_workspace(
            uow_factory,
            patcher,
            publication_kind=PUBLICATION_KIND,
        )
        subject_id = seed.seeded.subject.id
        snapshot, catalog, spare = await _seed_figures(seed.app, subject_id)
        state = _browser_state(
            snapshot=snapshot,
            figures=catalog,
            spare=spare,
            subject_id=subject_id,
            edition_id=seed.seeded.subject.edition_id,
            production_run_id=seed.seeded.run.id,
            database_name=database_name,
            api_port=port,
        )
        state_json = state.as_json()
        if shutdown_requested.is_set():
            return

        application = seed.app
        application.state.subject_service = SubjectService(uow_factory)

        @application.get("/api/health")
        async def health() -> dict[str, str]:
            return {"status": "ok"}

        @application.get("/api/editorial-browser/state")
        async def editorial_browser_state() -> dict[str, Any]:
            return state_json

        await asyncio.to_thread(_write_state, state_path, state_json)

        config = uvicorn.Config(
            application,
            host="127.0.0.1",
            port=port,
            log_level="info",
            access_log=False,
            lifespan="off",
        )
        server = uvicorn.Server(config)

        @application.post("/__editorial_browser/shutdown")
        async def request_shutdown() -> dict[str, str]:
            server.should_exit = True
            return {"status": "shutting_down"}

        print(
            "M23-editorial-browser / backend ready: "
            f"subject={subject_id} edition={state.edition_id} "
            f"database={database_name} port={port}",
            flush=True,
        )
        await server.serve()
    finally:
        try:
            if engine is not None:
                await engine.dispose()
        finally:
            try:
                if database_created:
                    await _drop_database(admin_dsn, database_name)
            finally:
                patcher.undo()


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
