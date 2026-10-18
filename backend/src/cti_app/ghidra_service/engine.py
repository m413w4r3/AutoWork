from __future__ import annotations

import logging
import os
import re
import signal
import subprocess
import threading
from pathlib import Path
from typing import BinaryIO
from urllib.parse import quote

_VERSION = re.compile(r"^application\.version=(.+)$", re.MULTILINE)
_JAVA_VERSION = re.compile(r'version "([^"]+)"')
_MAX_DIAGNOSTIC_BYTES = 2 * 1024
_logger = logging.getLogger(__name__)


class HeadlessGhidraEngine:
    """Invoke the fixed in-tree Java script through Ghidra's official headless runner."""

    def __init__(self, install_dir: Path, *, timeout_seconds: int) -> None:
        self.install_dir = install_dir.resolve(strict=True)
        self.timeout_seconds = timeout_seconds
        properties = (self.install_dir / "Ghidra" / "application.properties").read_text(
            encoding="utf-8"
        )
        version = _VERSION.search(properties)
        if version is None:
            raise RuntimeError("Ghidra application.version is unavailable")
        self.ghidra_version = version.group(1).strip()
        java_home_value = os.environ.get("JAVA_HOME", "")
        if not java_home_value:
            raise RuntimeError("Pinned JAVA_HOME is unavailable")
        java_home = Path(java_home_value).resolve(strict=True)
        java_executable = java_home / "bin" / "java"
        if not java_executable.is_file() or not os.access(java_executable, os.X_OK):
            raise RuntimeError("Pinned JAVA_HOME does not contain an executable JRE")
        java_environment = {
            "JAVA_HOME": str(java_home),
            "PATH": f"{java_home / 'bin'}:{os.environ.get('PATH', '')}",
        }
        java = subprocess.run(
            [str(java_executable), "-version"],
            check=False,
            capture_output=True,
            text=True,
            timeout=5,
            env=java_environment,
        )
        java_match = _JAVA_VERSION.search(java.stderr + java.stdout)
        if java.returncode != 0 or java_match is None:
            raise RuntimeError("Pinned Java runtime is unavailable")
        self.java_version = java_match.group(1)

    def analyze(
        self,
        sample_path: Path,
        *,
        operation: str,
        address: str | None,
        query: str | None,
        limit: int,
        workspace: Path,
    ) -> dict[str, object]:
        script_dir = Path(__file__).with_name("scripts")
        request_path = workspace / "request.properties"
        output_path = workspace / "result.json"
        values = {
            "operation": operation,
            "address": address or "",
            "query": query or "",
            "limit": str(limit),
        }
        request_path.write_text(
            "".join(f"{key}={quote(value, safe='')}\n" for key, value in values.items()),
            encoding="ascii",
        )
        project_dir = workspace / "project"
        project_dir.mkdir()
        home_dir = workspace / "home"
        java_tmp_dir = home_dir / "java-tmp"
        process_tmp_dir = home_dir / "tmp"
        config_dir = home_dir / ".config"
        cache_dir = home_dir / ".cache"
        for directory in (java_tmp_dir, process_tmp_dir, config_dir, cache_dir):
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        java_home = Path(os.environ["JAVA_HOME"]).resolve(strict=True)
        command = [
            str(self.install_dir / "support" / "analyzeHeadless"),
            str(project_dir),
            "autowork",
            "-import",
            str(sample_path),
            "-scriptPath",
            str(script_dir),
            "-postScript",
            "GhidraReadOnlyScript.java",
            str(request_path),
            str(output_path),
        ]
        environment = {
            "HOME": str(home_dir),
            "XDG_CONFIG_HOME": str(config_dir),
            "XDG_CACHE_HOME": str(cache_dir),
            "GHIDRA_USER_SETTINGS_DIR": str(config_dir / "ghidra"),
            "JAVA_HOME": str(java_home),
            "JAVA_TOOL_OPTIONS": (f"-Duser.home={home_dir} -Djava.io.tmpdir={java_tmp_dir}"),
            "PATH": f"{java_home / 'bin'}:{os.environ.get('PATH', '')}",
            "TMPDIR": str(process_tmp_dir),
            "LANG": "C.UTF-8",
            "LC_ALL": "C.UTF-8",
        }
        diagnostic_tail = bytearray()
        diagnostic_lock = threading.Lock()
        process = subprocess.Popen(
            command,
            cwd=workspace,
            env=environment,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            bufsize=0,
            start_new_session=True,
        )
        if process.stdout is None:
            raise RuntimeError("Ghidra headless output pipe could not be opened")
        output_reader = threading.Thread(
            target=_retain_output_tail,
            args=(process.stdout, diagnostic_tail, diagnostic_lock),
            daemon=True,
        )
        output_reader.start()
        try:
            return_code = process.wait(timeout=self.timeout_seconds)
        except subprocess.TimeoutExpired as exc:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            output_reader.join(timeout=2)
            _log_failure_tail("Ghidra headless timed out", diagnostic_tail, diagnostic_lock)
            raise TimeoutError("Ghidra headless analysis exceeded its hard deadline") from exc
        output_reader.join(timeout=2)
        if return_code != 0 or not output_path.is_file():
            _log_failure_tail(
                f"Ghidra headless exited with status {return_code}",
                diagnostic_tail,
                diagnostic_lock,
            )
            raise RuntimeError("Ghidra headless analysis failed")
        if output_path.stat().st_size > 64 * 1024:
            raise RuntimeError("Ghidra headless result exceeded its output limit")
        import json

        result = json.loads(output_path.read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise RuntimeError("Ghidra headless result is not an object")
        return result


def _retain_output_tail(
    stream: BinaryIO,
    tail: bytearray,
    lock: threading.Lock,
) -> None:
    while chunk := stream.read(1024):
        with lock:
            tail.extend(chunk)
            if len(tail) > _MAX_DIAGNOSTIC_BYTES:
                del tail[:-_MAX_DIAGNOSTIC_BYTES]


def _log_failure_tail(message: str, tail: bytearray, lock: threading.Lock) -> None:
    with lock:
        diagnostic = bytes(tail)
    text = diagnostic.decode("utf-8", errors="replace")
    text = re.sub(
        r"(?i)\b(password|passwd|token|secret|api[_-]?key|authorization)\b\s*[:=]\s*\S+",
        r"\1=[REDACTED]",
        text,
    )
    text = re.sub(r"(?i)\bBearer\s+[^\s]+", "Bearer [REDACTED]", text)
    text = re.sub(r"https?://[^\s/@:]+:[^\s/@]+@", "https://[REDACTED]@", text)
    text = "".join(
        character for character in text if character in "\n\t" or character.isprintable()
    )
    encoded = text.encode("utf-8", errors="replace")[-_MAX_DIAGNOSTIC_BYTES:]
    rendered = encoded.decode("utf-8", errors="replace").strip() or "<no diagnostic output>"
    _logger.error("%s; bounded output tail: %s", message, rendered)


def create_minimal_elf() -> bytes:
    """Return a tiny benign ELF64 executable whose entry is `mov eax, 42; ret`."""
    import struct

    ident = b"\x7fELF\x02\x01\x01" + b"\x00" * 9
    entry = 0x400078
    header = struct.pack(
        "<16sHHIQQQIHHHHHH",
        ident,
        2,
        62,
        1,
        entry,
        64,
        0,
        0,
        64,
        56,
        1,
        64,
        0,
        0,
    )
    load_segment = struct.pack(
        "<IIQQQQQQ",
        1,
        5,
        0,
        0x400000,
        0x400000,
        0x7E,
        0x7E,
        0x1000,
    )
    return (
        header
        + load_segment
        + b"\x90" * (0x78 - len(header) - len(load_segment))
        + b"\xb8\x2a\x00\x00\x00\xc3"
    )
