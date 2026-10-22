from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path

from cti_app.application.rule_workbench import YaraEnginePort, YaraSampleInput
from cti_app.domain.rule_workbench import YaraEngineResult, YaraExecutionStatus


@dataclass(frozen=True, slots=True, kw_only=True)
class YaraRunnerLimits:
    timeout_seconds: float = 10.0
    cpu_seconds: int = 8
    memory_bytes: int = 512 * 1024 * 1024
    maximum_output_bytes: int = 64 * 1024
    maximum_source_bytes: int = 512 * 1024 * 1024
    maximum_samples: int = 256
    maximum_sample_bytes: int = 64 * 1024 * 1024
    maximum_total_sample_bytes: int = 256 * 1024 * 1024
    maximum_strings_per_rule: int = 10_000

    def __post_init__(self) -> None:
        if self.timeout_seconds <= 0 or self.cpu_seconds <= 0:
            raise ValueError("YARA runner time limits must be positive")
        if (
            min(
                self.memory_bytes,
                self.maximum_output_bytes,
                self.maximum_source_bytes,
                self.maximum_samples,
                self.maximum_sample_bytes,
                self.maximum_total_sample_bytes,
                self.maximum_strings_per_rule,
            )
            <= 0
        ):
            raise ValueError("YARA runner size limits must be positive")
        if self.maximum_strings_per_rule > 10_000:
            raise ValueError("YARA strings-per-rule limit must be <= 10000")


class IsolatedYaraRunner(YaraEnginePort):
    """Compile and match YARA in a restricted Python worker process."""

    def __init__(self, limits: YaraRunnerLimits | None = None) -> None:
        self.limits = limits or YaraRunnerLimits()
        self._worker_path = Path(__file__).with_name("yara_runner_worker.py")

    @cached_property
    def _availability_failure_reason(self) -> str | None:
        """Run one bounded real-worker match to verify compile and match support."""
        if not self._worker_path.is_file():
            return "yara_worker_missing"
        sample_bytes = b"autowork-yara-availability-probe"
        sample = YaraSampleInput(sample_sha256=_sha256(sample_bytes), data=sample_bytes)
        rule = (
            "rule autowork_availability_probe { strings: $marker = "
            '"autowork-yara-availability-probe" condition: $marker }'
        )
        try:
            result = self.match(rule, (sample,))
        except Exception:
            return "yara_probe_failed"
        if result.status is not YaraExecutionStatus.OK:
            return f"yara_probe_{result.status.value.lower()}"
        if sample.sample_sha256 not in result.matches_sha256:
            return "yara_probe_match_missing"
        return None

    @property
    def available(self) -> bool:
        """Whether the isolated worker compiles and matches a small synthetic sample."""
        return self._availability_failure_reason is None

    @property
    def unavailable_reason(self) -> str | None:
        return self._availability_failure_reason

    def compile(self, source: str) -> YaraEngineResult:
        return self._run_worker("compile", source, ())

    def match(self, source: str, samples: Sequence[YaraSampleInput]) -> YaraEngineResult:
        if len(samples) > self.limits.maximum_samples:
            return _failure(
                YaraExecutionStatus.RESOURCE_EXCEEDED,
                "corpus exceeds the configured sample count",
            )
        total_size = 0
        seen: dict[str, bytes] = {}
        normalized: list[YaraSampleInput] = []
        for sample in samples:
            digest = _sha256(sample.data)
            if sample.sample_sha256 != digest:
                return _failure(
                    YaraExecutionStatus.INVALID_INPUT,
                    "sample bytes do not match the declared SHA-256",
                )
            if len(sample.data) > self.limits.maximum_sample_bytes:
                return _failure(
                    YaraExecutionStatus.RESOURCE_EXCEEDED,
                    "sample exceeds the configured byte limit",
                )
            previous = seen.get(sample.sample_sha256)
            if previous is not None:
                if previous != sample.data:
                    return _failure(
                        YaraExecutionStatus.INVALID_INPUT,
                        "duplicate sample SHA-256 has different bytes",
                    )
                continue
            seen[sample.sample_sha256] = sample.data
            normalized.append(sample)
            total_size += len(sample.data)
        if total_size > self.limits.maximum_total_sample_bytes:
            return _failure(
                YaraExecutionStatus.RESOURCE_EXCEEDED,
                "corpus exceeds the configured total byte limit",
            )
        return self._run_worker("match", source, tuple(normalized))

    def _run_worker(
        self,
        operation: str,
        source: str,
        samples: Sequence[YaraSampleInput],
    ) -> YaraEngineResult:
        source_bytes = source.encode("utf-8")
        if len(source_bytes) > self.limits.maximum_source_bytes:
            return _failure(
                YaraExecutionStatus.RESOURCE_EXCEEDED,
                "rule source exceeds the configured byte limit",
            )
        if operation not in {"compile", "match"}:
            return _failure(YaraExecutionStatus.INVALID_INPUT, "unsupported YARA operation")

        try:
            with tempfile.TemporaryDirectory(prefix="autowork-yara-") as directory_name:
                directory = Path(directory_name)
                source_path = directory / f"{_sha256(source_bytes)}.yar"
                source_path.write_bytes(source_bytes)
                sample_arguments: list[str] = []
                for sample in samples:
                    if _sha256(sample.data) != sample.sample_sha256:
                        return _failure(
                            YaraExecutionStatus.INVALID_INPUT,
                            "sample bytes do not match the declared SHA-256",
                        )
                    sample_path = directory / f"{sample.sample_sha256}.bin"
                    sample_path.write_bytes(sample.data)
                    sample_arguments.append(f"{sample.sample_sha256}:{sample_path}")

                stdout_path = directory / "worker.stdout"
                stderr_path = directory / "worker.stderr"
                command = [
                    sys.executable,
                    "-I",
                    str(self._worker_path),
                    operation,
                    str(source_path),
                    str(self.limits.memory_bytes),
                    str(self.limits.cpu_seconds),
                    str(self.limits.maximum_output_bytes),
                    str(self.limits.timeout_seconds),
                    str(self.limits.maximum_strings_per_rule),
                    *sample_arguments,
                ]
                minimal_env = {
                    "HOME": str(directory),
                    "LANG": "C.UTF-8",
                    "LC_ALL": "C.UTF-8",
                    "PATH": "/usr/bin:/bin",
                    "TMPDIR": str(directory),
                }
                with stdout_path.open("wb") as stdout, stderr_path.open("wb") as stderr:
                    try:
                        process = subprocess.Popen(
                            command,
                            stdin=subprocess.DEVNULL,
                            stdout=stdout,
                            stderr=stderr,
                            cwd=directory,
                            env=minimal_env,
                            close_fds=True,
                            start_new_session=True,
                        )
                    except OSError:
                        return _failure(
                            YaraExecutionStatus.ENGINE_UNAVAILABLE,
                            "isolated YARA worker could not be started",
                        )
                    try:
                        return_code = process.wait(timeout=self.limits.timeout_seconds)
                    except subprocess.TimeoutExpired:
                        _kill_process_group(process.pid)
                        process.wait()
                        return _failure(
                            YaraExecutionStatus.TIMEOUT,
                            "isolated YARA worker exceeded its wall clock limit",
                        )

                if (
                    stdout_path.stat().st_size + stderr_path.stat().st_size
                    > self.limits.maximum_output_bytes
                ):
                    return _failure(
                        YaraExecutionStatus.RESOURCE_EXCEEDED,
                        "isolated YARA worker exceeded its output limit",
                    )
                try:
                    response = json.loads(stdout_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeDecodeError, json.JSONDecodeError):
                    if return_code < 0 and -return_code in {
                        signal.SIGKILL,
                        signal.SIGXCPU,
                        signal.SIGXFSZ,
                    }:
                        return _failure(
                            YaraExecutionStatus.RESOURCE_EXCEEDED,
                            "isolated YARA worker exceeded an operating system resource limit",
                        )
                    return _failure(
                        YaraExecutionStatus.ENGINE_CRASHED,
                        "isolated YARA worker returned no valid result",
                    )
                try:
                    status = YaraExecutionStatus(response["status"])
                    result = YaraEngineResult(
                        status=status,
                        engine_version=str(response.get("engine_version") or "unknown"),
                        matches_sha256=tuple(response.get("matches_sha256", ())),
                        message=str(response.get("message", ""))[:1000],
                        line=response.get("line"),
                        column=response.get("column"),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    return _failure(
                        YaraExecutionStatus.ENGINE_CRASHED,
                        f"isolated YARA worker returned a malformed result ({type(exc).__name__})",
                    )
                if return_code < 0 and result.status is YaraExecutionStatus.OK:
                    if -return_code in {signal.SIGKILL, signal.SIGXCPU, signal.SIGXFSZ}:
                        return _failure(
                            YaraExecutionStatus.RESOURCE_EXCEEDED,
                            "isolated YARA worker exceeded an operating system resource limit",
                        )
                    return _failure(
                        YaraExecutionStatus.ENGINE_CRASHED,
                        "isolated YARA worker terminated unexpectedly",
                    )
                return result
        except OSError:
            return _failure(
                YaraExecutionStatus.ENGINE_CRASHED,
                "isolated YARA worker could not prepare its temporary inputs",
            )


def _sha256(payload: bytes) -> str:
    import hashlib

    return hashlib.sha256(payload).hexdigest()


def _kill_process_group(process_id: int) -> None:
    try:
        os.killpg(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _failure(status: YaraExecutionStatus, message: str) -> YaraEngineResult:
    return YaraEngineResult(status=status, engine_version="unknown", message=message[:1000])


__all__ = ["IsolatedYaraRunner", "YaraRunnerLimits"]
