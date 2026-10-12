from __future__ import annotations

import asyncio
import ipaddress
import os
import socket
from collections.abc import Iterator, Mapping
from pathlib import Path

import pytest
from sqlalchemy.engine import make_url

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from tests.integration.conftest import (
    migrated_postgres_url,  # noqa: F401
    temporary_postgres_url,  # noqa: F401
    uow_factory,  # noqa: F401
)
from tests.integration.production.support import ProductionScenario

from .driver import WorkflowDriver


def _allow_local_host(host: object, *, allow_socket_path: bool = False) -> bool:
    if not isinstance(host, str):
        return False
    if allow_socket_path and host.startswith("/"):
        return True
    if host.rstrip(".").lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except (ValueError, TypeError):
        return False


def _allow_loopback(address: object) -> bool:
    if isinstance(address, str):
        return _allow_local_host(address, allow_socket_path=True)
    if not isinstance(address, tuple) or not address:
        return False
    return _allow_local_host(address[0])


@pytest.fixture(scope="session", autouse=True)
def validate_workflow_postgres_dsn_before_database_fixtures() -> None:
    """Reject a non-local integration DSN before session database setup can connect."""
    dsn = os.getenv("TEST_POSTGRES_ADMIN_DSN")
    if not dsn:
        return
    try:
        host = make_url(dsn).host
    except Exception as exc:
        pytest.fail(
            "workflow emulator requires TEST_POSTGRES_ADMIN_DSN to be a valid local PostgreSQL DSN",
            pytrace=False,
        )
        raise AssertionError("unreachable") from exc
    if host in (None, "") or (isinstance(host, str) and host.startswith("/")):
        return
    if not _allow_local_host(host):
        pytest.fail(
            "workflow emulator requires TEST_POSTGRES_ADMIN_DSN to use a local PostgreSQL host "
            "(localhost, 127.0.0.0/8, ::1, or a Unix socket)",
            pytrace=False,
        )


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: pytest.Item, call: pytest.CallInfo[object]) -> Iterator[None]:
    outcome = yield
    report = outcome.get_result()
    workflow_dir = Path(__file__).resolve().parent
    if (
        Path(item.path).resolve().is_relative_to(workflow_dir)
        and os.getenv("WORKFLOW_EMULATOR_REQUIRED") == "1"
        and report.skipped
    ):
        cause = report.longrepr
        report.outcome = "failed"
        report.longrepr = (
            f"{item.nodeid}: WORKFLOW_EMULATOR_REQUIRED=1 forbids a skipped workflow test; "
            f"skip cause: {cause}"
        )


@pytest.fixture(autouse=True)
def block_non_loopback_sockets(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep HTTP/model/Bridge adapters local; PostgreSQL loopback remains available."""
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex
    original_getaddrinfo = socket.getaddrinfo
    original_loop_getaddrinfo = asyncio.BaseEventLoop.getaddrinfo
    original_gethostbyname = socket.gethostbyname
    original_gethostbyname_ex = socket.gethostbyname_ex
    original_gethostbyaddr = socket.gethostbyaddr

    def checked_connect(sock: socket.socket, address: object) -> None:
        if not _allow_loopback(address):
            raise AssertionError(f"workflow emulator blocked non-loopback socket: {address!r}")
        original_connect(sock, address)  # type: ignore[arg-type]

    def checked_connect_ex(sock: socket.socket, address: object) -> int:
        if not _allow_loopback(address):
            raise AssertionError(f"workflow emulator blocked non-loopback socket: {address!r}")
        return original_connect_ex(sock, address)  # type: ignore[arg-type]

    def checked_getaddrinfo(host: object, *args: object, **kwargs: object) -> object:
        if host is not None and not _allow_local_host(host):
            raise AssertionError(f"workflow emulator blocked non-loopback DNS lookup: {host!r}")
        return original_getaddrinfo(host, *args, **kwargs)  # type: ignore[arg-type]

    async def checked_loop_getaddrinfo(
        loop: asyncio.BaseEventLoop, host: object, *args: object, **kwargs: object
    ) -> object:
        if host is not None and not _allow_local_host(host):
            raise AssertionError(f"workflow emulator blocked non-loopback DNS lookup: {host!r}")
        return await original_loop_getaddrinfo(loop, host, *args, **kwargs)  # type: ignore[arg-type]

    def checked_gethostbyname(host: str) -> str:
        if not _allow_local_host(host):
            raise AssertionError(f"workflow emulator blocked non-loopback DNS lookup: {host!r}")
        return original_gethostbyname(host)

    def checked_gethostbyname_ex(host: str) -> tuple[str, list[str], list[str]]:
        if not _allow_local_host(host):
            raise AssertionError(f"workflow emulator blocked non-loopback DNS lookup: {host!r}")
        return original_gethostbyname_ex(host)

    def checked_gethostbyaddr(address: str) -> tuple[str, list[str], list[str]]:
        if not _allow_local_host(address):
            raise AssertionError(f"workflow emulator blocked non-loopback DNS lookup: {address!r}")
        return original_gethostbyaddr(address)

    monkeypatch.setattr(socket.socket, "connect", checked_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", checked_connect_ex)
    monkeypatch.setattr(socket, "getaddrinfo", checked_getaddrinfo)
    monkeypatch.setattr(asyncio.BaseEventLoop, "getaddrinfo", checked_loop_getaddrinfo)
    monkeypatch.setattr(socket, "gethostbyname", checked_gethostbyname)
    monkeypatch.setattr(socket, "gethostbyname_ex", checked_gethostbyname_ex)
    monkeypatch.setattr(socket, "gethostbyaddr", checked_gethostbyaddr)


@pytest.fixture
def workflow_driver(
    uow_factory: UnitOfWorkFactory,  # noqa: F811
    tmp_path: Path,
) -> Iterator[WorkflowDriver]:
    blob_root = tmp_path / "workflow-blobs"
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(blob_root), uow_factory)
    )

    def production_scenario_factory(
        sources: Mapping[str, Mapping[str, object]],
    ) -> ProductionScenario:
        return ProductionScenario(uow_factory, blob_root, sources)

    driver = WorkflowDriver(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        production_scenario_factory=production_scenario_factory,
        trace_root=tmp_path,
    )
    yield driver
