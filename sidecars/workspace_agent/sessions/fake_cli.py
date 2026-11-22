"""Deterministic scripted operator CLI used to prove the supervisor contract.

This file is a standalone stdlib script (no package import) so the supervisor
can execute it from the session scratch cwd with the closed environment. It
accepts the supervisor-built argv shape any adapter would, emits JSONL events,
writes an ``-o`` style output document, receives the one-time session secret
over the per-session AF_UNIX socket, and simulates success, crash, hang, flood,
slow, spawn-child and resume modes from a bounded JSON script:

    {"steps": [{"op": "emit", "event": {"type": "started"}}, ...],
     "final_message": "...", "outcome": "success"}

Facts that tests need (argv, environment, PIDs, secret SHA-256) are recorded in
``--output-file``; the secret itself and instruction prose are never printed.
"""

from __future__ import annotations

import argparse
import base64
import ctypes
import hashlib
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PR_SET_PDEATHSIG = 1
SECRET_SCHEMA = "autowork-session-secret.v1"
MAX_SECRET_FRAME_BYTES = 4096
MAX_SCRIPT_BYTES = 64 * 1024
DEFAULT_FINAL_MESSAGE = "fake final message"
WORK_DIRECTORY_NAME = "work"
BUILTIN_SCENARIOS: dict[str, list[dict[str, object]]] = {
    "success": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "write", "path": "notes/finding.md", "text": "# fake finding\n"},
        {"op": "write", "path": "result.md", "text": "fake result\n"},
        {"op": "write", "path": "final.md", "text": "fake final body\n"},
    ],
    "consult": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "emit", "event": {"type": "consult", "broker": "advisor.consult"}},
        {"op": "write", "path": "result.md", "text": "consult result\n"},
    ],
    "hang": [{"op": "emit", "event": {"type": "started"}}, {"op": "hang"}],
    "flood": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "log", "stream": "stdout", "count": 200, "bytes": 100},
    ],
    "crash": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "crash", "code": 3},
    ],
    "resume_ok": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "emit", "event": {"type": "resumed"}},
    ],
    "resume_unavailable": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "crash", "code": 4},
    ],
    "spawn_child": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "spawn_child", "seconds": 120},
        {"op": "hang"},
    ],
    "write_note": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "write", "path": "notes/finding.md", "text": "# fake note\n"},
        {"op": "write", "path": "result.md", "text": "fake result\n"},
    ],
    "secret": [
        {"op": "emit", "event": {"type": "started"}},
        {"op": "receive_secret", "attempts": 2},
    ],
}


class FakeState:
    """Mutable observations, output document and event emitter."""

    def __init__(self, output_path: Path | None, run: int) -> None:
        self.output_path = output_path
        self.run = run
        self.final_message = DEFAULT_FINAL_MESSAGE
        self.outcome = "success"
        self.observations: list[dict[str, object]] = []
        self._lock = threading.Lock()
        self._instruction_event = threading.Event()

    def observe(self, **entry: object) -> None:
        with self._lock:
            self.observations.append({"kind": str(entry.pop("kind", "observation")), **entry})
            self._write_locked()

    def instruction_seen(self) -> None:
        self._instruction_event.set()

    def wait_instruction(self, timeout: float) -> bool:
        return self._instruction_event.wait(timeout)

    def write_output(self) -> None:
        with self._lock:
            self._write_locked()

    def _write_locked(self) -> None:
        if self.output_path is None:
            return
        payload = {
            "final_message": self.final_message,
            "outcome": self.outcome,
            "observations": list(self.observations),
        }
        data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        temporary = self.output_path.with_name(f".{self.output_path.name}.{os.getpid()}.tmp")
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
            os.replace(temporary, self.output_path)
        except OSError:
            try:
                os.unlink(temporary)
            except OSError:
                pass


def emit(state: FakeState, event: dict[str, object]) -> None:
    payload = {"run": state.run, "pid": os.getpid(), **event}
    sys.stdout.write(json.dumps(payload, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def set_die_with_parent() -> None:
    """Ask the kernel to SIGKILL us if the creating parent dies (bwrap parity)."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        return
    if os.getppid() == 1:
        raise SystemExit(0)


def read_secret(state: FakeState, socket_path: str, attempts: int) -> None:
    try:
        connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        connection.settimeout(5.0)
        connection.connect(socket_path)
    except OSError:
        state.observe(kind="secret_error", delivered=False)
        emit(state, {"type": "secret_error"})
        return
    try:
        frame = b""
        while b"\n" not in frame and len(frame) <= MAX_SECRET_FRAME_BYTES:
            chunk = connection.recv(1024)
            if not chunk:
                break
            frame += chunk
    finally:
        connection.close()
    try:
        document = json.loads(frame.split(b"\n", 1)[0].decode("utf-8"))
        if not isinstance(document, dict) or document.get("schema") != SECRET_SCHEMA:
            raise ValueError("unexpected secret frame")
        secret = base64.b64decode(str(document.get("secret", "")), validate=True)
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        state.observe(kind="secret_error", delivered=False)
        emit(state, {"type": "secret_error"})
        return
    state.observe(
        kind="secret_received",
        sha256=hashlib.sha256(secret).hexdigest(),
        length=len(secret),
    )
    emit(state, {"type": "secret_received", "length": len(secret)})
    if attempts <= 1:
        return
    # The supervisor unlinks the socket after the one-shot delivery; poll until
    # a second connection is refused so this observation cannot race shutdown.
    deadline = time.monotonic() + 2.0
    delivered = False
    while time.monotonic() < deadline:
        try:
            connection = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            connection.settimeout(0.5)
            connection.connect(socket_path)
        except OSError:
            break
        delivered = True
        try:
            connection.recv(1024)
        except OSError:
            pass
        finally:
            connection.close()
        time.sleep(0.02)
    state.observe(kind="secret_second_attempt", delivered=delivered)
    emit(state, {"type": "secret_second_attempt", "delivered": delivered})


def read_stdin(state: FakeState) -> None:
    for line in sys.stdin.buffer:
        try:
            frame = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            continue
        if not isinstance(frame, dict):
            continue
        if "instruction_id" in frame:
            state.observe(
                kind="instruction",
                instruction_id=frame.get("instruction_id"),
                sha256=frame.get("sha256"),
                length=len(str(frame.get("text", "")).encode("utf-8")),
            )
            emit(
                state,
                {"type": "instruction_received", "instruction_id": frame.get("instruction_id")},
            )
            state.instruction_seen()
        elif frame.get("type") == "run_prompt":
            state.observe(kind="run_prompt", length=len(str(frame.get("text", ""))))


def write_scratch(scratch_root: Path, relative: str, text: str) -> None:
    parts = relative.split("/")
    if not parts or any(part in {"", ".", ".."} for part in parts) or relative.startswith("/"):
        raise SystemExit(2)
    if not (
        relative.startswith("notes/")
        or relative in {"result.md", "final.md"}
        or relative.startswith("work/")
    ):
        raise SystemExit(2)
    target = scratch_root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(text.encode("utf-8"))


def spawn_child(state: FakeState, seconds: int) -> None:
    child = subprocess.Popen(
        [sys.executable, "-B", "-c", f"import time; time.sleep({max(1, seconds)})"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
    )
    state.observe(kind="child_started", pid=child.pid)
    emit(state, {"type": "child_started", "pid": child.pid})


def run_step(state: FakeState, step: dict[str, object], scratch_root: Path) -> bool:
    operation = step.get("op")
    if operation == "emit":
        event = step.get("event")
        emit(state, dict(event) if isinstance(event, dict) else {"type": "event"})
    elif operation == "log":
        stream_name = str(step.get("stream", "stdout"))
        count = int(step.get("count", 1))
        width = int(step.get("bytes", 80))
        prefix = str(step.get("prefix", "fake-log-line"))
        for index in range(count):
            line = f"{prefix}-{index:04d}-" + ("x" * width)
            stream = sys.stdout if stream_name == "stdout" else sys.stderr
            stream.write(line + "\n")
            stream.flush()
    elif operation == "write":
        write_scratch(scratch_root, str(step["path"]), str(step.get("text", "")))
    elif operation == "probe":
        env = {
            key: value
            for key, value in os.environ.items()
            if key.startswith("AUTOWORK") or key in {"PATH", "HOME", "TMPDIR", "LANG"}
        }
        state.observe(kind="probe", argv=list(sys.argv), env=env)
        emit(state, {"type": "probe"})
    elif operation == "receive_secret":
        socket_path = os.environ.get("AUTOWORK_SESSION_SOCKET", "")
        if not socket_path:
            state.observe(kind="secret_error", delivered=False)
        else:
            read_secret(state, socket_path, int(step.get("attempts", 1)))
    elif operation == "spawn_child":
        spawn_child(state, int(step.get("seconds", 120)))
    elif operation == "sleep":
        time.sleep(float(step.get("seconds", 0.05)))
    elif operation == "wait_instruction":
        state.wait_instruction(float(step.get("timeout", 5.0)))
    elif operation == "hang":
        while True:
            time.sleep(1.0)
    elif operation == "crash":
        state.outcome = "failed"
        state.write_output()
        raise SystemExit(int(step.get("code", 1)))
    elif operation == "exit":
        return True
    else:
        raise SystemExit(2)
    return False


def load_script(path: str | None, scenario: str) -> dict[str, object]:
    if path:
        try:
            data = Path(path).read_bytes()
        except OSError:
            data = b""
        if data and len(data) <= MAX_SCRIPT_BYTES:
            try:
                value = json.loads(data.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                value = None
            if isinstance(value, dict):
                return value
    steps = BUILTIN_SCENARIOS.get(scenario, BUILTIN_SCENARIOS["success"])
    return {"steps": steps, "final_message": DEFAULT_FINAL_MESSAGE, "outcome": "success"}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="autowork-fake-cli")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--run", type=int, default=1)
    parser.add_argument("--scenario", default="success")
    parser.add_argument("--script", default=None)
    parser.add_argument("--output-file", default=None)
    parser.add_argument("--prompt-file", default=None)
    parser.add_argument("--resume", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    set_die_with_parent()
    output_path = Path(args.output_file) if args.output_file else None
    state = FakeState(output_path, args.run)
    script = load_script(args.script, args.scenario)
    final_message = script.get("final_message")
    if isinstance(final_message, str):
        state.final_message = final_message
    outcome = script.get("outcome")
    if isinstance(outcome, str):
        state.outcome = outcome
    if args.prompt_file:
        try:
            prompt = Path(args.prompt_file).read_bytes()
        except OSError:
            prompt = b""
        state.observe(
            kind="prompt",
            sha256=hashlib.sha256(prompt).hexdigest(),
            length=len(prompt),
        )
    state.observe(kind="resume", requested=bool(args.resume))
    threading.Thread(target=read_stdin, args=(state,), name="fake-stdin", daemon=True).start()
    state.write_output()
    cwd = Path.cwd()
    scratch_root = cwd.parent if cwd.name == WORK_DIRECTORY_NAME else cwd
    steps = script.get("steps")
    exit_code = 0 if isinstance(steps, list) else 2
    if isinstance(steps, list):
        try:
            for step in steps:
                if not isinstance(step, dict):
                    exit_code = 2
                    break
                if run_step(state, step, scratch_root):
                    break
        except SystemExit as exc:
            exit_code = int(exc.code or 0)
    state.write_output()
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except OSError:
        pass
    # The stdin reader is a daemon blocked in a buffered read; a normal
    # interpreter shutdown would abort on its buffered lock (SIGABRT). Exit
    # directly after the output document and stream flushes.
    os._exit(exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
