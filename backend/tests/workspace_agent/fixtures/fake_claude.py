#!/usr/bin/env python3
"""Deterministic stand-in for ``claude`` 2.1.284 used by the M24-05 H5 tests.

It proves the supervisor/CLI *contract* only: every run parses the exact argv
the ``ClaudeProfile`` builds (unknown flags are a hard error), records bounded
observations in the cwd, reads the one-shot stdin frames in a background
thread, emits a deterministic stream-json sequence and writes the H1 collect
oracle file. It never calls a model, a provider, a network endpoint or an auth
API.

``--version``/``--help`` answer the profile's non-destructive probe and print
the real recorded help text next to this file. A run selects
``success``/``note``/``crash``/``hang``/``flood``/``bad_json``/``no_result``
from ``claude-scenario.json`` in the cwd (default ``success``).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from pathlib import Path

VERSION_TEXT = "2.1.284 (Claude Code)"
HELP_FIXTURE_NAME = "claude_help_2_1_284.txt"
DEFAULT_FINAL_MESSAGE = "fake claude final message"
OBSERVATION_FILE = "claude-observations.json"
SCENARIO_FILE = "claude-scenario.json"
ORACLE_FILE = "fake-output.json"
WORK_DIRECTORY_NAME = "work"
MAX_STDIN_FRAMES = 32
MAX_STDIN_LINE_BYTES = 64 * 1024
MAX_MCP_CONFIG_BYTES = 64 * 1024


class FakeState:
    """Bounded observations plus the event emitter for one fake run."""

    def __init__(self, args: argparse.Namespace, scenario: dict[str, object]) -> None:
        self.args = args
        self.scenario = scenario
        self.observations: dict[str, object] = {
            "argv": list(sys.argv[1:]),
            "cwd": str(Path.cwd()),
            "pid": os.getpid(),
            "ppid": os.getppid(),
            "env": dict(os.environ),
            "parse": _parsed_contract(args),
            "emitted": [],
            "stdin_frames": [],
        }
        self._lock = threading.Lock()

    def add(self, **entry: object) -> None:
        with self._lock:
            self.observations.update(entry)
            self.write()

    def emit(self, event: dict[str, object]) -> None:
        payload = dict(event)
        with self._lock:
            emitted = self.observations["emitted"]
            assert isinstance(emitted, list)
            emitted.append(payload)
            self.write()
        sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
        sys.stdout.flush()

    def write(self) -> None:
        payload = self.observations
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        target = Path.cwd() / OBSERVATION_FILE
        temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(
                temporary,
                os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
                0o600,
            )
            try:
                os.write(fd, data)
                os.fsync(fd)
            finally:
                os.close(fd)
            os.replace(temporary, target)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def write_oracle(final_message: str | None, outcome: str) -> None:
    payload: dict[str, object] = {"outcome": outcome}
    if final_message is not None:
        payload["final_message"] = final_message
    target = Path.cwd() / ORACLE_FILE
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    data = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    try:
        fd = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(fd, data)
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(temporary, target)
    except OSError:
        pass


def _parsed_contract(args: argparse.Namespace) -> dict[str, object]:
    return {
        "print_mode": bool(args.print_mode),
        "output_format": args.output_format,
        "input_format": args.input_format,
        "verbose": bool(args.verbose),
        "model": args.model,
        "session_id": args.session_id,
        "resume": args.resume,
        "fork_session": bool(args.fork_session),
        "no_session_persistence": bool(args.no_session_persistence),
        "permission_mode": args.permission_mode,
        "permission_prompts": args.permission_prompts,
        "allowed_tools": args.allowedTools,
        "disallowed_tools": args.disallowedTools,
        "tools": args.tools,
        "restricted": bool(args.restricted),
        "bare": bool(args.bare),
        "strict_mcp_config": bool(args.strict_mcp_config),
        "mcp_configs": list(args.mcp_config or []),
        "max_budget_usd": args.max_budget_usd,
        "append_system_prompt_sha256": (
            None
            if args.append_system_prompt is None
            else hashlib.sha256(args.append_system_prompt.encode("utf-8")).hexdigest()
        ),
        "append_system_prompt": args.append_system_prompt,
        "append_system_prompt_bytes": (
            None
            if args.append_system_prompt is None
            else len(args.append_system_prompt.encode("utf-8"))
        ),
        "json_schema": None if args.json_schema is None else True,
        "settings": args.settings,
        "setting_sources": args.setting_sources,
        "add_dirs": list(args.add_dir or []),
        "prompt_sha256": hashlib.sha256(args.prompt.encode("utf-8")).hexdigest(),
        "prompt_bytes": len(args.prompt.encode("utf-8")),
    }


def _inspect_mcp_config(paths: list[str]) -> dict[str, object]:
    """Read the generated MCP config without ever following an escaping symlink."""
    if not paths:
        return {"present": False}
    source = Path(paths[0])
    try:
        info = source.lstat()
        data = source.read_bytes()
    except OSError:
        return {"present": False, "error": "unreadable"}
    if not source.is_absolute() or len(data) > MAX_MCP_CONFIG_BYTES:
        return {"present": False, "error": "not_an_absolute_bounded_file"}
    try:
        document = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"present": True, "error": "invalid_json"}
    if not isinstance(document, dict):
        return {"present": True, "error": "not_an_object"}
    servers = document.get("mcpServers")
    if not isinstance(servers, dict):
        return {"present": True, "error": "no_mcp_servers"}
    summary: dict[str, object] = {}
    helpers: dict[str, object] = {}
    for name, raw in servers.items():
        if not isinstance(raw, dict):
            continue
        raw_args = raw.get("args")
        args = [str(item) for item in raw_args] if isinstance(raw_args, list) else []
        shim = Path(args[0]) if args else None
        shim_info: dict[str, object] = {"exists": False}
        if shim is not None:
            try:
                shim_stat = shim.lstat()
            except OSError:
                shim_info = {"exists": False}
            else:
                shim_info = {
                    "exists": True,
                    "mode": shim_stat.st_mode & 0o777,
                    "symlink": shim.is_symlink(),
                }
        helpers[str(name)] = {
            "command": raw.get("command"),
            "args": args,
            "env_keys": sorted(
                str(key) for key in (raw.get("env") or {}) if isinstance(raw.get("env"), dict)
            ),
            "shim": shim_info,
        }
        summary[str(name)] = dict(raw)
    return {
        "present": True,
        "mode": info.st_mode & 0o777,
        "server_names": sorted(summary),
        "servers": summary,
        "helpers": helpers,
    }


def _read_stdin(state: FakeState) -> None:
    for line in sys.stdin.buffer:
        if len(line) > MAX_STDIN_LINE_BYTES:
            continue
        with state._lock:  # the fake records raw frames
            frames = state.observations["stdin_frames"]
            assert isinstance(frames, list)
            if len(frames) >= MAX_STDIN_FRAMES:
                continue
            frames.append(line.decode("utf-8", errors="replace").strip())
            state.write()


def _write_note_files() -> None:
    cwd = Path.cwd()
    root = cwd.parent if cwd.name == WORK_DIRECTORY_NAME else cwd
    for relative, text in (
        ("notes/finding.md", "# fake claude note\n"),
        ("result.md", "fake claude result\n"),
        ("final.md", "fake claude final body\n"),
    ):
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")


def _load_scenario() -> dict[str, object]:
    path = Path.cwd() / SCENARIO_FILE
    try:
        data = path.read_bytes()
    except OSError:
        return {"scenario": "success"}
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {"scenario": "success"}
    return value if isinstance(value, dict) else {"scenario": "success"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="claude", add_help=False, allow_abbrev=False)
    parser.add_argument("-h", "--help", action="store_true")
    parser.add_argument("-v", "--version", action="store_true")
    parser.add_argument("-p", "--print", dest="print_mode", action="store_true")
    parser.add_argument("--output-format", choices=("text", "json", "stream-json"))
    parser.add_argument("--input-format", choices=("text", "stream-json"))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--model")
    parser.add_argument("--session-id")
    parser.add_argument("-r", "--resume", nargs="?", const=True)
    parser.add_argument("--fork-session", action="store_true")
    parser.add_argument("--no-session-persistence", action="store_true")
    parser.add_argument("--permission-mode")
    parser.add_argument("--permission-prompts")
    parser.add_argument("--allowedTools")
    parser.add_argument("--disallowedTools")
    parser.add_argument("--tools")
    parser.add_argument("--restricted", action="store_true")
    parser.add_argument("--bare", action="store_true")
    parser.add_argument("--strict-mcp-config", action="store_true")
    parser.add_argument("--mcp-config", action="append")
    parser.add_argument("--max-budget-usd")
    parser.add_argument("--append-system-prompt")
    parser.add_argument("--json-schema")
    parser.add_argument("--settings")
    parser.add_argument("--setting-sources")
    parser.add_argument("--add-dir", action="append")
    parser.add_argument("prompt", nargs="?", default="")
    return parser


def main(argv: list[str] | None = None) -> int:
    arguments = sys.argv[1:] if argv is None else argv
    args = build_parser().parse_args(arguments)
    if args.version:
        sys.stdout.write(VERSION_TEXT + "\n")
        sys.stdout.flush()
        return 0
    if args.help:
        sys.stdout.write(Path(__file__).with_name(HELP_FIXTURE_NAME).read_text(encoding="utf-8"))
        sys.stdout.flush()
        return 0
    scenario = _load_scenario()
    name = str(scenario.get("scenario", "success"))
    final_message = scenario.get("final_message")
    if not isinstance(final_message, str):
        final_message = DEFAULT_FINAL_MESSAGE
    state = FakeState(args, scenario)
    state.add(mcp=_inspect_mcp_config(list(args.mcp_config or [])))
    threading.Thread(target=_read_stdin, args=(state,), name="fake-stdin", daemon=True).start()
    session_id = args.session_id
    if not isinstance(session_id, str) or not session_id:
        session_id = args.resume if isinstance(args.resume, str) else "fake-session-id"
    state.emit({"type": "system", "subtype": "init", "session_id": session_id})
    if name == "crash":
        write_oracle(None, "failed")
        state.write()
        sys.stdout.flush()
        os._exit(3)
    if name == "hang":
        write_oracle(None, "interrupted")
        state.write()
        sys.stdout.flush()
        while True:
            time.sleep(1.0)
    if name == "flood":
        for index in range(200):
            state.emit({"type": "system", "subtype": "noise", "index": index})
    if name == "bad_json":
        sys.stdout.write('{"type": "assistant", broken\n')
        sys.stdout.flush()
    if name == "note":
        _write_note_files()
    if name == "no_result":
        write_oracle(None, "success")
        state.write()
        sys.stdout.flush()
        os._exit(0)
    state.emit(
        {
            "type": "assistant",
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "working"},
                    {
                        "type": "tool_use",
                        "id": "toolu_fake_1",
                        "name": "autowork_task_heartbeat",
                        "input": {"note": "synthetic"},
                    },
                ],
            },
        }
    )
    state.emit(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_fake_1",
                        "is_error": False,
                        "content": "ok",
                    }
                ],
            },
        }
    )
    state.emit(
        {
            "type": "result",
            "subtype": "success",
            "is_error": False,
            "result": final_message,
            "session_id": session_id,
        }
    )
    write_oracle(final_message, "success")
    state.add(exit_code=0)
    sys.stdout.flush()
    sys.stderr.flush()
    # The stdin reader is a daemon blocked in a buffered read; exit directly so
    # interpreter shutdown never aborts on its buffered lock.
    os._exit(0)


if __name__ == "__main__":
    raise SystemExit(main())
