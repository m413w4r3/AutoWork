from __future__ import annotations

import json
import math
import re
import resource
import socket
import sys
import time
from pathlib import Path
from typing import Any

from cti_app.infrastructure.seccomp_network import install_network_seccomp_filter


def _deny_network(*_args: Any, **_kwargs: Any) -> None:
    raise OSError("network access is disabled in the YARA worker")


socket.socket = _deny_network  # type: ignore[misc,assignment]
socket.create_connection = _deny_network  # type: ignore[assignment]
socket.getaddrinfo = _deny_network  # type: ignore[assignment]


def _emit(
    status: str,
    *,
    engine_version: str = "unknown",
    matches: tuple[str, ...] = (),
    message: str = "",
    line: int | None = None,
    column: int | None = None,
) -> None:
    payload = {
        "status": status,
        "engine_version": engine_version,
        "matches_sha256": matches,
        "message": message[:1000],
        "line": line,
        "column": column,
    }
    sys.stdout.write(json.dumps(payload, separators=(",", ":")))


def _syntax_error_column(source: str, line: int | None, message: str) -> int | None:
    explicit = re.search(r"(?i)\bcolumn\s*[:=]?\s*(\d+)\b", message)
    if explicit is not None:
        return max(1, int(explicit.group(1)))
    lines = source.splitlines()
    if line is None or line < 1 or line > len(lines):
        return None
    source_line = lines[line - 1]
    unexpected = re.search(r"(?i)unexpected\s+(.+?)(?:,|$)", message)
    if unexpected is None:
        return None
    token = unexpected.group(1).strip()
    if token.casefold() in {"$end", "<eof>", "end of file"}:
        return len(source_line) + 1
    if len(token) >= 2 and token[0] in "'\"" and token[-1] == token[0]:
        token = token[1:-1]
    elif len(token) >= 2 and token[0] == "<" and token[-1] == ">":
        token = token[1:-1]
    if not token:
        return None
    position = source_line.rfind(token)
    return position + 1 if position >= 0 else None


def _set_limits(memory_bytes: int, cpu_seconds: int, output_bytes: int) -> None:
    resource.setrlimit(resource.RLIMIT_AS, (memory_bytes, memory_bytes))
    resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
    resource.setrlimit(resource.RLIMIT_FSIZE, (output_bytes, output_bytes))
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_NOFILE, (128, 128))


def main(arguments: list[str]) -> int:
    if len(arguments) < 8:
        _emit("invalid_input", message="worker arguments are incomplete")
        return 0
    operation, source_argument = arguments[1], arguments[2]
    try:
        memory_bytes = int(arguments[3])
        cpu_seconds = int(arguments[4])
        output_bytes = int(arguments[5])
        timeout_seconds = float(arguments[6])
        maximum_strings_per_rule = int(arguments[7])
    except ValueError:
        _emit("invalid_input", message="worker limits are invalid")
        return 0
    sample_arguments = arguments[8:]
    if not 1 <= maximum_strings_per_rule <= 65_536:
        _emit("invalid_input", message="YARA strings-per-rule limit is invalid")
        return 0
    if operation not in {"compile", "match"}:
        _emit("invalid_input", message="worker operation is invalid")
        return 0
    try:
        _set_limits(memory_bytes, cpu_seconds, output_bytes)
    except (OSError, ValueError):
        _emit("engine_crashed", message="worker resource limits could not be applied")
        return 0

    try:
        install_network_seccomp_filter()
    except Exception:
        _emit("engine_unavailable", message="worker network isolation could not be applied")
        return 0

    try:
        import yara  # type: ignore[import-not-found]
    except ImportError:
        _emit("engine_unavailable", message="yara-python is not installed")
        return 0
    except MemoryError:
        _emit("resource_exceeded", message="worker memory limit was reached while loading YARA")
        return 0
    except Exception:
        _emit("engine_unavailable", message="yara-python could not be loaded")
        return 0

    engine_version = str(getattr(yara, "__version__", "unknown"))[:128]
    try:
        yara.set_config(max_strings_per_rule=maximum_strings_per_rule)
    except Exception:
        _emit(
            "engine_unavailable",
            engine_version=engine_version,
            message="YARA max_strings_per_rule limit could not be applied",
        )
        return 0
    source_path = Path(source_argument)
    try:
        if not source_path.is_file():
            _emit(
                "invalid_input",
                engine_version=engine_version,
                message="rule source file is missing",
            )
            return 0
        rules = yara.compile(filepath=str(source_path), includes=False)
    except yara.SyntaxError as exc:
        message = str(exc)
        line = getattr(exc, "line", None) or getattr(exc, "lineno", None)
        try:
            normalized_line = int(line) if line is not None else None
        except (TypeError, ValueError):
            normalized_line = None
        if normalized_line is None:
            match = re.search(r"\((\d+)\):", message)
            if match is not None:
                normalized_line = max(1, int(match.group(1)))
        elif normalized_line < 1:
            normalized_line = 1
        try:
            source_text = source_path.read_text(encoding="utf-8", errors="strict")
        except (OSError, UnicodeDecodeError):
            source_text = ""
        normalized_column = _syntax_error_column(source_text, normalized_line, message)
        _emit(
            "compile_error",
            engine_version=engine_version,
            message=message,
            line=normalized_line,
            column=normalized_column,
        )
        return 0
    except MemoryError:
        _emit(
            "resource_exceeded",
            engine_version=engine_version,
            message="worker memory limit was reached while compiling the rule",
        )
        return 0
    except Exception as exc:
        _emit("engine_crashed", engine_version=engine_version, message=type(exc).__name__)
        return 0

    if operation == "compile":
        _emit("ok", engine_version=engine_version)
        return 0

    deadline = time.monotonic() + timeout_seconds
    matches: list[str] = []
    for argument in sample_arguments:
        sample_sha256, separator, sample_path_text = argument.partition(":")
        if not separator or len(sample_sha256) != 64:
            _emit(
                "invalid_input",
                engine_version=engine_version,
                matches=tuple(matches),
                message="sample path identity is invalid",
            )
            return 0
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            _emit(
                "timeout",
                engine_version=engine_version,
                matches=tuple(matches),
                message="YARA match budget was exhausted",
            )
            return 0
        sample_path = Path(sample_path_text)
        try:
            found = rules.match(
                filepath=str(sample_path),
                timeout=max(1, math.ceil(remaining)),
                fast=True,
            )
        except yara.TimeoutError:
            _emit(
                "timeout",
                engine_version=engine_version,
                matches=tuple(matches),
                message="YARA exceeded its per-match timeout",
            )
            return 0
        except MemoryError:
            _emit(
                "resource_exceeded",
                engine_version=engine_version,
                matches=tuple(matches),
                message="worker memory limit was reached while testing the corpus",
            )
            return 0
        except Exception as exc:
            _emit(
                "engine_crashed",
                engine_version=engine_version,
                matches=tuple(matches),
                message=type(exc).__name__,
            )
            return 0
        if found:
            matches.append(sample_sha256)

    _emit("ok", engine_version=engine_version, matches=tuple(matches))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
