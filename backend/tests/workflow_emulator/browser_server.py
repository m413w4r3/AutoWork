"""Serve the W10 browser scenario against an isolated PostgreSQL database."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import os
import signal
import tempfile
from dataclasses import replace
from pathlib import Path
from typing import Any
from uuid import uuid4

import uvicorn
from sqlalchemy.engine import make_url

from cti_app.api.subjects import router as subjects_router
from cti_app.application.edition_review import EditionReview, EditionReviewService
from cti_app.application.subjects import SubjectService
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config, _create_database, _drop_database
from tests.integration.test_edition_multibatch_publication_postgres import (
    _compose_three_runs,
)

from .driver import build_emulator_app


def _is_loopback_host(host: str | None) -> bool:
    if host in (None, "") or host.startswith("/"):
        return True
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _apply_proof_mutation(mutation: str | None) -> None:
    """Apply the named proof mutation in memory; disabled unless explicitly requested."""
    if mutation is None or mutation == "":
        return
    if mutation != "review_ignores_exclusions":
        raise ValueError(f"unknown WORKFLOW_BROWSER_MUTATION: {mutation}")

    original_from_rows = EditionReviewService.from_rows

    def review_ignores_exclusions(*args: Any, **kwargs: Any) -> EditionReview:
        review = original_from_rows(*args, **kwargs)
        if any(exclusion.applicable for exclusion in review.exclusions):
            return replace(review, can_accept=False)
        return review

    # Proof mutation, disabled by default: an applicable exclusion keeps Review blocked.
    EditionReviewService.from_rows = staticmethod(review_ignores_exclusions)


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

    admin_dsn = os.environ.get("TEST_POSTGRES_ADMIN_DSN")
    if not admin_dsn:
        raise RuntimeError("TEST_POSTGRES_ADMIN_DSN is required for W10-browser-exclusion")
    admin_url = make_url(admin_dsn)
    if not _is_loopback_host(admin_url.host):
        raise RuntimeError(
            "W10-browser-exclusion requires TEST_POSTGRES_ADMIN_DSN to target local PostgreSQL"
        )

    state_value = os.environ.get("WORKFLOW_BROWSER_STATE")
    if not state_value:
        raise RuntimeError("WORKFLOW_BROWSER_STATE is required for W10-browser-exclusion")
    state_path = Path(state_value)
    try:
        port = int(os.environ.get("WORKFLOW_BROWSER_API_PORT", "18765"))
    except ValueError as exc:
        raise RuntimeError("WORKFLOW_BROWSER_API_PORT must be an integer") from exc
    if not 1 <= port <= 65535:
        raise RuntimeError("WORKFLOW_BROWSER_API_PORT must be between 1 and 65535")

    _apply_proof_mutation(os.environ.get("WORKFLOW_BROWSER_MUTATION"))
    database_name = f"cti_browser_{uuid4().hex}"
    database_created = False
    engine = None
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

        with tempfile.TemporaryDirectory(prefix="workflow-browser-") as blob_directory:
            edition, artifact_store, subjects, _batches = await _compose_three_runs(
                uow_factory,
                Path(blob_directory),
                production_order=("A", "B", "C"),
                composition_order=("A", "B", "C"),
            )
            if shutdown_requested.is_set():
                return
            application = build_emulator_app(uow_factory, artifact_store)
            application.include_router(subjects_router)
            application.state.subject_service = SubjectService(uow_factory)

            @application.get("/api/health")
            async def health() -> dict[str, str]:
                return {"status": "ok"}

            state = {
                "edition_id": str(edition.id),
                "subjects": {label: str(subjects[label].id) for label in ("A", "B", "C")},
                "database_name": database_name,
                "country": edition.country,
                "initial_composition_version": 1,
                "exclusion_reason": "Hors périmètre éditorial pour cette édition.",
            }
            state_path.parent.mkdir(parents=True, exist_ok=True)
            await asyncio.to_thread(
                state_path.write_text,
                json.dumps(state, indent=2) + "\n",
                encoding="utf-8",
            )

            config = uvicorn.Config(
                application,
                host="127.0.0.1",
                port=port,
                log_level="info",
                access_log=False,
            )
            server = uvicorn.Server(config)

            @application.post("/__workflow_browser/shutdown")
            async def request_shutdown() -> dict[str, str]:
                server.should_exit = True
                return {"status": "shutting_down"}

            print(
                "W10-browser-exclusion / backend ready: "
                f"edition={edition.id} database={database_name} port={port}",
                flush=True,
            )
            await server.serve()
    finally:
        try:
            if engine is not None:
                await engine.dispose()
        finally:
            if database_created:
                await _drop_database(admin_dsn, database_name)


def main() -> None:
    asyncio.run(_serve())


if __name__ == "__main__":
    main()
