#!/usr/bin/env python3
"""Deterministic stand-in for ``codex-cli 0.163.0`` used by the H4 profile tests.

It answers exactly the argv surface the profile uses and replays the captured
real help text for the preflight. A run reads a control file from its cwd
(``fake-codex-scenario.json``), reads the prompt from stdin, writes the ``-o``
final-message file, emits synthetic JSONL lines and appends one bounded
observation document. The JSONL event names are synthetic: they prove the
parser's tolerance, never Codex CLI semantics. No model, auth or network call
is made and the prompt text is never printed.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import time
from pathlib import Path

HELP_FIXTURE_NAME = "codex_help_0_163_0.txt"
MODE_FILE_NAME = "fake-codex-mode.json"
SCENARIO_FILE_NAME = "fake-codex-scenario.json"
OBSERVATION_FILE_NAME = "fake-codex-observations.jsonl"
DEFAULT_FINAL_MESSAGE = "codex fake final message"
MAX_SCENARIO_BYTES = 64 * 1024


def _script_directory() -> Path:
    return Path(__file__).resolve().parent


def _help_sections() -> dict[str, str]:
    """Parse the captured fixture into ``{command: text}`` sections."""
    try:
        text = (_script_directory() / HELP_FIXTURE_NAME).read_text(encoding="utf-8")
    except OSError:
        return {}
    sections: dict[str, str] = {}
    current: str | None = None
    for line in text.splitlines(keepends=True):
        if line.startswith("$ codex "):
            current = line[len("$ codex ") :].strip()
            sections[current] = ""
        elif current is not None:
            sections[current] += line
    mode = _mode()
    omitted = mode.get("omit_flags")
    if isinstance(omitted, list):
        for flag in omitted:
            if isinstance(flag, str) and flag:
                sections = {
                    name: body.replace(flag, "--omitted-flag") for name, body in sections.items()
                }
    return sections


def _mode() -> dict[str, object]:
    try:
        data = (_script_directory() / MODE_FILE_NAME).read_bytes()
    except OSError:
        return {}
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _scenario() -> dict[str, object]:
    try:
        data = (Path.cwd() / SCENARIO_FILE_NAME).read_bytes()
    except OSError:
        return {}
    if len(data) > MAX_SCENARIO_BYTES:
        return {}
    try:
        value = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _write_private(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
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
        os.replace(temporary, path)
    except OSError:
        try:
            os.unlink(temporary)
        except OSError:
            pass


def _write_final_message(argv: list[str], text: str) -> None:
    for index, token in enumerate(argv):
        if token in {"-o", "--output-last-message"} and index + 1 < len(argv):
            _write_private(Path(argv[index + 1]), text.encode("utf-8"))
            return
    raise SystemExit(2)


def _append_observation(document: dict[str, object]) -> None:
    path = Path.cwd() / OBSERVATION_FILE_NAME
    line = json.dumps(document, separators=(",", ":"), ensure_ascii=False).encode("utf-8") + b"\n"
    try:
        fd = os.open(
            path,
            os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC,
            0o600,
        )
        try:
            os.write(fd, line)
        finally:
            os.close(fd)
    except OSError:
        pass


def _write_scratch_note(text: str) -> None:
    cwd = Path.cwd()
    scratch_root = cwd.parent if cwd.name == "work" else cwd
    notes = scratch_root / "notes"
    try:
        notes.mkdir(mode=0o700, parents=True, exist_ok=True)
        (notes / "agent-note.md").write_text(text, encoding="utf-8")
        (scratch_root / "result.md").write_text("codex result body\n", encoding="utf-8")
    except OSError:
        pass


def _emit(event: dict[str, object]) -> None:
    sys.stdout.write(json.dumps(event, separators=(",", ":"), ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _run_exec(argv: list[str]) -> int:
    resume = len(argv) > 1 and argv[1] == "resume"
    prompt = sys.stdin.buffer.read()
    scenario = _scenario()
    final_message = scenario.get("final_message")
    if not isinstance(final_message, str):
        final_message = DEFAULT_FINAL_MESSAGE
    events = scenario.get("events")
    if not isinstance(events, list):
        events = [
            {"type": "thread.started"},
            {"type": "turn.completed", "usage": {"input_tokens": 1, "output_tokens": 1}},
        ]
    _append_observation(
        {
            "mode": "resume" if resume else "exec",
            "argv": list(sys.argv),
            "cwd": str(Path.cwd()),
            "stdin_bytes": len(prompt),
            "stdin_sha256": hashlib.sha256(prompt).hexdigest(),
        }
    )
    for event in events:
        if isinstance(event, dict):
            _emit({str(key): value for key, value in event.items()})
    note = scenario.get("note_text")
    _write_scratch_note(note if isinstance(note, str) else "# agent note\n")
    _write_final_message(argv, final_message)
    outcome = scenario.get("outcome")
    if outcome == "crash":
        return 3
    if outcome == "hang":
        while True:
            time.sleep(1.0)
    return 0


def main(argv: list[str]) -> int:
    sections = _help_sections()
    if argv == ["--version"]:
        sys.stdout.write("codex-cli 0.163.0\n")
        return 0
    if argv == ["--help"]:
        sys.stdout.write(sections.get("--help", "Codex CLI\n"))
        return 0
    if argv == ["exec", "--help"]:
        sys.stdout.write(sections.get("exec --help", "Run Codex non-interactively\n"))
        return 0
    if argv == ["exec", "resume", "--help"]:
        sys.stdout.write(sections.get("exec resume --help", "Resume a previous session\n"))
        return 0
    if argv[:1] == ["exec"]:
        return _run_exec(argv)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
