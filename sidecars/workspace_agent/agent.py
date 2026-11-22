"""Process wiring: runtime directory, instance lock, token rotation, listener.

A second instance holding ``agent.lock`` fails with ``InstanceLockBusy`` (the CLI
exits 73). The token is rotated only after the lock is held, so a losing start
never invalidates the running instance's bearer token.
"""

from __future__ import annotations

import fcntl
import grp
import logging
import os
import stat
import threading
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .auth import generate_token, rotate_token_file
from .http_server import WorkspaceAgentHTTPServer
from .protocol import PROTOCOL_VERSION, format_rfc3339_utc

LOGGER = logging.getLogger("autowork.workspace_agent")

RUNTIME_DIR_MODE = 0o750
RUNTIME_DIR_FORBIDDEN_BITS = 0o022
RUNTIME_DIR_NAME = "autowork-workspace"
SOCKET_FILE_NAME = "workspace-agent.sock"
LOCK_FILE_NAME = "agent.lock"
LOCK_FILE_MODE = 0o640
EXIT_SECOND_INSTANCE = 73
SERVE_POLL_SECONDS = 0.25


class AgentStartupError(RuntimeError):
    """The agent cannot start safely."""


class RuntimeDirectoryError(AgentStartupError):
    """The runtime directory is missing, unsafe or owned by another user."""


class InstanceLockBusy(AgentStartupError):
    """Another live agent instance owns the runtime directory."""


@dataclass(frozen=True)
class AgentConfig:
    root: Path
    socket_dir: Path
    group: int | None = None
    poll_interval_ms: int = 1000
    stability_window_ms: int = 400
    log_level: str = "INFO"
    allow_foreign_runtime_dir: bool = False


def resolve_group(value: str | int | None) -> int | None:
    """Resolve ``--group`` (numeric gid or group name) to a gid."""
    if value is None:
        return None
    if isinstance(value, int):
        gid = value
    else:
        text = value.strip()
        if not text:
            return None
        if text.isdigit():
            gid = int(text)
        else:
            try:
                gid = grp.getgrnam(text).gr_gid
            except KeyError as exc:
                raise AgentStartupError(f"unknown group: {text}") from exc
    if not 0 <= gid <= 2**31 - 1:
        raise AgentStartupError("group id out of range")
    return gid


def prepare_runtime_dir(config: AgentConfig) -> Path:
    """Create or validate the 0750 runtime directory; refuse unsafe ones."""
    path = config.socket_dir
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        try:
            path.mkdir(parents=True, exist_ok=True, mode=0o700)
            os.chmod(path, RUNTIME_DIR_MODE)
            if config.group is not None:
                os.chown(path, -1, config.group)
            info = os.lstat(path)
        except OSError as exc:
            raise RuntimeDirectoryError("cannot create the runtime directory") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise RuntimeDirectoryError("runtime directory is not a real directory")
    if stat.S_IMODE(info.st_mode) & RUNTIME_DIR_FORBIDDEN_BITS:
        raise RuntimeDirectoryError("runtime directory is group/other writable")
    if info.st_uid != os.geteuid() and not config.allow_foreign_runtime_dir:
        raise RuntimeDirectoryError("runtime directory is owned by another user")
    if config.group is not None and info.st_gid != config.group:
        try:
            os.chown(path, -1, config.group)
        except OSError as exc:
            raise RuntimeDirectoryError("cannot set the runtime directory group") from exc
    return path


class InstanceLock:
    """Non-blocking exclusive ``flock`` on ``agent.lock``."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self) -> None:
        flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC
        try:
            fd = os.open(self.path, flags, LOCK_FILE_MODE)
        except OSError as exc:
            raise AgentStartupError("cannot open the instance lock file") from exc
        try:
            os.fchmod(fd, LOCK_FILE_MODE)
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise InstanceLockBusy("another workspace agent instance is running") from exc
        except OSError:
            os.close(fd)
            raise
        self._fd = fd

    def release(self) -> None:
        if self._fd is None:
            return
        try:
            fcntl.flock(self._fd, fcntl.LOCK_UN)
        finally:
            os.close(self._fd)
            self._fd = None


class WorkspaceAgent:
    """One agent process: identity, runtime directory and HTTP server."""

    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.instance_id = uuid.uuid4().hex
        self.started_at = format_rfc3339_utc(datetime.now(UTC))
        self.token: str | None = None
        self.runtime_dir: Path | None = None
        self._lock = InstanceLock(config.socket_dir / LOCK_FILE_NAME)
        self._server: WorkspaceAgentHTTPServer | None = None
        self._ready = threading.Event()

    @property
    def socket_path(self) -> Path:
        return self.config.socket_dir / SOCKET_FILE_NAME

    def start(self) -> None:
        runtime_dir = prepare_runtime_dir(self.config)
        self.runtime_dir = runtime_dir
        self._lock.acquire()
        try:
            token = generate_token()
            rotate_token_file(runtime_dir, token, gid=self.config.group)
            self.token = token
            self._server = WorkspaceAgentHTTPServer(self.socket_path, self, gid=self.config.group)
        except Exception:
            self.token = None
            self._lock.release()
            raise
        self._ready.set()
        LOGGER.info("workspace agent ready instance_id=%s", self.instance_id)

    def serve(self, stop_event: threading.Event | None = None) -> None:
        stop = threading.Event() if stop_event is None else stop_event
        try:
            self.start()
            server = self._server
            assert server is not None
            server.timeout = SERVE_POLL_SECONDS
            while not stop.is_set():
                server.handle_request()
        finally:
            self.stop()

    def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.server_close()
        self._lock.release()
        self._ready.clear()
        self.token = None
        LOGGER.info("workspace agent stopped instance_id=%s", self.instance_id)

    def wait_ready(self, timeout: float) -> bool:
        return self._ready.wait(timeout)

    def health_payload(self) -> dict[str, object]:
        """Liveness only: never a host path, token or workspace name."""
        return {
            "status": "ready",
            "protocol": PROTOCOL_VERSION,
            "instance_id": self.instance_id,
            "pid": os.getpid(),
            "started_at": self.started_at,
        }
