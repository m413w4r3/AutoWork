"""CLI entry point: ``python -m sidecars.workspace_agent``.

Root and socket directory are resolved when ``main`` runs, never at import.
SIGTERM/SIGINT stop the server gracefully; a second live instance exits 73.
"""

from __future__ import annotations

import argparse
import logging
import os
import signal
import sys
import threading
from collections.abc import Sequence
from pathlib import Path

from .agent import (
    EXIT_SECOND_INSTANCE,
    RUNTIME_DIR_NAME,
    AgentConfig,
    AgentStartupError,
    InstanceLockBusy,
    WorkspaceAgent,
    resolve_group,
)
from .auth import TokenFileError
from .http_server import SocketPathError

LOGGER = logging.getLogger("autowork.workspace_agent")

DEFAULT_ROOT = "~/work/autowork"
DEFAULT_POLL_INTERVAL_MS = 1000
DEFAULT_STABILITY_WINDOW_MS = 400
DEFAULT_LOG_LEVEL = "INFO"


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return parsed


def _non_negative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be zero or greater")
    return parsed


def _log_level(value: str) -> str:
    name = value.strip().upper()
    if name not in logging.getLevelNamesMapping():
        raise argparse.ArgumentTypeError(f"unknown log level: {value}")
    return name


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="autowork-workspace-agent",
        description="Host workspace agent for AutoWork (AF_UNIX HTTP/1.1, stdlib only).",
    )
    parser.add_argument(
        "--root",
        default=os.environ.get("AUTOWORK_WORKSPACE_ROOT", DEFAULT_ROOT),
        help="workspace tree root (default: %(default)s)",
    )
    parser.add_argument(
        "--socket-dir",
        default=os.environ.get("AUTOWORK_WORKSPACE_SOCKET_DIR") or None,
        help=f"runtime directory (default: $XDG_RUNTIME_DIR/{RUNTIME_DIR_NAME})",
    )
    parser.add_argument(
        "--group",
        default=os.environ.get("AUTOWORK_WORKSPACE_GID") or None,
        help="group name or gid for the socket and token",
    )
    parser.add_argument(
        "--analysis-gid",
        default=os.environ.get("AUTOWORK_WORKSPACE_ANALYSIS_GID") or None,
        help="group name or gid allowed to read materialized base samples",
    )
    parser.add_argument(
        "--poll-interval-ms",
        type=_positive_int,
        default=DEFAULT_POLL_INTERVAL_MS,
        help="watcher polling interval (default: %(default)s)",
    )
    parser.add_argument(
        "--stability-window-ms",
        type=_non_negative_int,
        default=DEFAULT_STABILITY_WINDOW_MS,
        help="required file stability window (default: %(default)s)",
    )
    parser.add_argument(
        "--log-level",
        type=_log_level,
        default=DEFAULT_LOG_LEVEL,
        help="logging level (default: %(default)s)",
    )
    parser.add_argument(
        "--allow-foreign-runtime-dir",
        action="store_true",
        help="allow a runtime directory owned by another uid (tests only)",
    )
    return parser


def default_socket_dir() -> Path:
    runtime_dir = os.environ.get("XDG_RUNTIME_DIR", "").strip()
    if not runtime_dir:
        raise AgentStartupError("XDG_RUNTIME_DIR is not set and --socket-dir was not given")
    return Path(runtime_dir) / RUNTIME_DIR_NAME


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        stream=sys.stderr,
    )
    try:
        socket_dir = Path(args.socket_dir) if args.socket_dir else default_socket_dir()
        config = AgentConfig(
            root=Path(args.root).expanduser().resolve(),
            socket_dir=socket_dir,
            group=resolve_group(args.group),
            analysis_gid=resolve_group(args.analysis_gid),
            poll_interval_ms=args.poll_interval_ms,
            stability_window_ms=args.stability_window_ms,
            log_level=args.log_level,
            allow_foreign_runtime_dir=args.allow_foreign_runtime_dir,
        )
    except AgentStartupError as exc:
        LOGGER.error("workspace agent configuration failed: %s", exc)
        return 1

    stop = threading.Event()

    def _request_stop(signum: int, frame: object) -> None:
        del signum, frame
        stop.set()

    signal.signal(signal.SIGTERM, _request_stop)
    signal.signal(signal.SIGINT, _request_stop)

    agent = WorkspaceAgent(config)
    try:
        agent.serve(stop)
    except InstanceLockBusy:
        LOGGER.error("another workspace agent instance already owns the runtime directory")
        return EXIT_SECOND_INSTANCE
    except (AgentStartupError, SocketPathError, TokenFileError) as exc:
        LOGGER.error("workspace agent startup failed: %s", exc)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
