from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, cast

from cti_app.application.local_analysis import LocalCommandCapability
from cti_app.config import get_settings
from cti_app.domain.local_analysis import (
    LocalCommandId,
    LocalCommandOutputKind,
    LocalCommandResult,
    LocalCommandSpec,
    LocalCommandStatus,
)
from cti_app.infrastructure.seccomp_network import (
    install_network_seccomp_filter as _install_network_seccomp_filter,
)
from cti_app.infrastructure.seccomp_network import (
    network_seccomp_filter_program,
)

_CONTROL_CHARACTERS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_URL_TEXT = re.compile(r"https?://[^\s\"'<>]+", re.IGNORECASE)
_CREDENTIAL_ASSIGNMENT = re.compile(
    r"(?i)[\"']?\b(?:"
    r"api[_ -]?key|access[_ -]?token|auth[_ -]?token|secret|password|passwd|"
    r"authorization|private[_ -]?key)\b[\"']?\s*[:=]\s*[\"']?[^\"'\s,;}]+"
)
_BEARER_TOKEN = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_JWT_TOKEN = re.compile(r"\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\b")
_LONG_TOKEN = re.compile(r"\b[A-Za-z0-9_+/=-]{40,}\b")
_PRIVATE_KEY_BLOCK = re.compile(
    r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----[\s\S]*?-----END [A-Z0-9 ]*PRIVATE KEY-----",
    re.IGNORECASE,
)
_DEFAULT_MAX_SAMPLE_BYTES = 200 * 1024 * 1024
_FAILED_NAMESPACE_LAUNCHERS: set[tuple[str, str]] = set()
_FAILED_NAMESPACE_LAUNCHER_REASONS: dict[tuple[str, str], NamespaceFailureReason] = {}
_NAMESPACE_LAUNCHERS_LOCK = threading.Lock()


class NetworkIsolationStatus(StrEnum):
    BWRAP_NET = "bwrap-net"
    UNSHARE_NET = "unshare-net"
    SECCOMP_ONLY = "seccomp-network-syscall-filter"


class NamespaceFailureReason(StrEnum):
    BWRAP_NOT_INSTALLED = "bwrap_not_installed"
    BWRAP_PROBE_FAILED = "bwrap_probe_failed"
    BWRAP_USERNS_REFUSED_BY_KERNEL = "bwrap_userns_refused_by_kernel"
    BWRAP_PROBE_NETWORK_SOCKET_ALLOWED = "bwrap_probe_network_socket_allowed"
    BWRAP_PROBE_NETWORK_SOCKET_UNEXPECTED_ERROR = "bwrap_probe_network_socket_unexpected_error"
    BWRAP_PROBE_FILESYSTEM_NOT_CONFINED = "bwrap_probe_filesystem_not_confined"
    BWRAP_PROBE_SAMPLE_MOUNT_UNREADABLE = "bwrap_probe_sample_mount_unreadable"
    BWRAP_PREVIOUSLY_FAILED = "bwrap_previously_failed"
    UNSHARE_NOT_INSTALLED = "unshare_not_installed"
    UNSHARE_PROBE_FAILED = "unshare_probe_failed"
    UNSHARE_PREVIOUSLY_FAILED = "unshare_previously_failed"
    NAMESPACE_LAUNCHER_FAILED_DURING_EXECUTION = "namespace_launcher_failed_during_execution"
    NO_NETWORK_NAMESPACE_AVAILABLE = "no_network_namespace_available"


@dataclass(frozen=True, slots=True)
class _NamespaceDetection:
    status: NetworkIsolationStatus
    launcher: tuple[str, str] | None
    reasons: tuple[NamespaceFailureReason, ...] = ()


class LocalCommandRunner:
    """Run only catalogued analysis tools with bounded resources and no network syscalls."""

    def __init__(
        self,
        *,
        network_sandbox_probe_seconds: float = 2.0,
        max_sample_bytes: int = _DEFAULT_MAX_SAMPLE_BYTES,
        require_filesystem_confinement: bool | None = None,
    ) -> None:
        if max_sample_bytes <= 0:
            raise ValueError("max_sample_bytes must be positive")
        self._max_sample_bytes = max_sample_bytes
        self._require_filesystem_confinement = (
            get_settings().local_analysis_require_filesystem_confinement
            if require_filesystem_confinement is None
            else require_filesystem_confinement
        )
        detection = _detect_network_namespace(network_sandbox_probe_seconds)
        if detection is None:
            detection = _NamespaceDetection(
                NetworkIsolationStatus.SECCOMP_ONLY,
                None,
                (NamespaceFailureReason.NO_NETWORK_NAMESPACE_AVAILABLE,),
            )
        self._namespace_detection = detection
        self._namespace_launcher = detection.launcher

    @property
    def network_isolation(self) -> str:
        return self._namespace_detection.status.value

    @property
    def namespace_status(self) -> NetworkIsolationStatus:
        return self._namespace_detection.status

    @property
    def namespace_reasons(self) -> tuple[NamespaceFailureReason, ...]:
        return self._namespace_detection.reasons

    @property
    def namespace_reason(self) -> NamespaceFailureReason | None:
        return self._namespace_detection.reasons[-1] if self._namespace_detection.reasons else None

    @property
    def filesystem_confined(self) -> bool:
        return self._namespace_detection.status is NetworkIsolationStatus.BWRAP_NET

    async def run(
        self,
        capability: LocalCommandCapability,
        *,
        sample_sha256: str,
        sample_bytes: bytes,
        options: Mapping[str, object],
        _allow_namespace_fallback: bool = True,
    ) -> LocalCommandResult:
        spec = capability.spec
        started_at = datetime.now(UTC)
        monotonic_start = time.monotonic()
        if not capability.available or capability.executable_path is None:
            return _make_result(
                spec,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                started_at,
                0,
                "",
                (spec.binary,),
                capability.version,
                "binary_unavailable",
            )
        if spec.requires_filesystem_confinement and not self.filesystem_confined:
            return _make_result(
                spec,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                started_at,
                0,
                "",
                (spec.binary,),
                capability.version,
                "filesystem_confinement_required",
            )
        if spec.id in {
            LocalCommandId.ZIP_MEMBERS,
            LocalCommandId.ARCHIVE_LIST,
            LocalCommandId.ARCHIVE_EXTRACT_HASH,
        }:
            return _make_result(
                spec,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                started_at,
                0,
                "",
                (spec.binary,),
                capability.version,
                "confined_archive_worker_required",
            )
        if self._require_filesystem_confinement and not self.filesystem_confined:
            return _make_result(
                spec,
                LocalCommandStatus.UNAVAILABLE,
                sample_sha256,
                started_at,
                0,
                "",
                (spec.binary,),
                capability.version,
                "filesystem_confinement_unavailable",
            )
        if len(sample_bytes) > self._max_sample_bytes:
            return _make_result(
                spec,
                LocalCommandStatus.POLICY_DENIED,
                sample_sha256,
                started_at,
                0,
                "",
                (spec.binary,),
                capability.version,
                "sample_too_large",
            )
        if hashlib.sha256(sample_bytes).hexdigest() != sample_sha256:
            return _make_result(
                spec,
                LocalCommandStatus.FAILED,
                sample_sha256,
                started_at,
                0,
                "",
                (spec.binary,),
                capability.version,
                "sample_hash_mismatch",
            )

        with tempfile.TemporaryDirectory(prefix="cti-local-analysis-") as temporary:
            directory = Path(temporary)
            sample_path = directory / "sample.bin"
            command_bytes = sample_bytes
            if spec.id is LocalCommandId.SHA256SUM and options.get("offset") is not None:
                start = cast(int, options["offset"])
                command_bytes = sample_bytes[start : start + cast(int, options["length"])]
            sample_path.write_bytes(command_bytes)
            sample_path.chmod(0o400)
            rule_path: Path | None = None
            if spec.id is LocalCommandId.YARA:
                rule_path = directory / "rules.yar"
                rule_path.write_text(_render_yara_rule(options), encoding="ascii")
                rule_path.chmod(0o400)

            argv, logical_argv = _build_argv(
                spec,
                capability.executable_path,
                sample_path,
                rule_path,
                options,
            )
            logical_argv = (*logical_argv, f"<options_sha256:{_options_sha256(options)}>")
            seccomp_filter_file: BinaryIO | None = None
            pass_fds: tuple[int, ...] = ()
            install_filter_before_exec = True
            if self._namespace_launcher is not None:
                if self._namespace_launcher[0] == NetworkIsolationStatus.BWRAP_NET.value:
                    try:
                        seccomp_filter_file = _network_seccomp_filter_file()
                        seccomp_fd = seccomp_filter_file.fileno()
                        argv = self._wrap_network_namespace(
                            argv, sample_path, rule_path, seccomp_fd=seccomp_fd
                        )
                    except OSError:
                        if seccomp_filter_file is not None:
                            seccomp_filter_file.close()
                        return _make_result(
                            spec,
                            LocalCommandStatus.UNAVAILABLE,
                            sample_sha256,
                            started_at,
                            time.monotonic() - monotonic_start,
                            "",
                            logical_argv,
                            capability.version,
                            "seccomp_filter_unavailable",
                        )
                    pass_fds = (seccomp_fd,)
                    install_filter_before_exec = False
                else:
                    argv = self._wrap_network_namespace(argv, sample_path, rule_path)
            environment = {
                "PATH": "/usr/bin:/bin",
                "LANG": "C",
                "LC_ALL": "C",
                "HOME": "/analysis"
                if self.namespace_status is NetworkIsolationStatus.BWRAP_NET
                else str(directory),
                "TMPDIR": "/tmp"
                if self.namespace_status is NetworkIsolationStatus.BWRAP_NET
                else str(directory),
            }

            try:
                launch_argv = _sandbox_bootstrap_argv(
                    argv,
                    spec,
                    install_network_filter_before_exec=install_filter_before_exec,
                )
                process = await asyncio.create_subprocess_exec(
                    *launch_argv,
                    cwd=directory,
                    stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    env=environment,
                    start_new_session=True,
                    pass_fds=pass_fds,
                )
            except (OSError, subprocess.SubprocessError, ValueError):
                return _make_result(
                    spec,
                    LocalCommandStatus.UNAVAILABLE,
                    sample_sha256,
                    started_at,
                    time.monotonic() - monotonic_start,
                    "",
                    logical_argv,
                    capability.version,
                    "isolation_or_process_start_unavailable",
                )
            finally:
                if seccomp_filter_file is not None:
                    seccomp_filter_file.close()

            assert process.stdout is not None
            assert process.stderr is not None
            captured = bytearray()
            captured_stderr = bytearray()
            output_limit_hit = False
            # L4: hash all stdout chunks while keeping the retained prefix bounded.
            raw_stdout_hash = hashlib.sha256() if spec.capture_full_output_hash else None
            raw_stdout_bytes = 0

            async def read_stream(stream: asyncio.StreamReader, *, is_stderr: bool) -> None:
                nonlocal output_limit_hit, raw_stdout_bytes
                while True:
                    chunk = await stream.read(64 * 1024)
                    if not chunk:
                        return
                    if raw_stdout_hash is not None and not is_stderr:
                        raw_stdout_hash.update(chunk)
                        raw_stdout_bytes += len(chunk)
                    if spec.capture_full_output_hash and is_stderr:
                        stderr_remaining = spec.max_output_bytes - len(captured_stderr)
                        if stderr_remaining > 0:
                            captured_stderr.extend(chunk[:stderr_remaining])
                        continue
                    remaining = spec.max_output_bytes - len(captured)
                    if len(chunk) > remaining:
                        if remaining > 0:
                            captured.extend(chunk[:remaining])
                        if is_stderr:
                            stderr_remaining = spec.max_output_bytes - len(captured_stderr)
                            if stderr_remaining > 0:
                                captured_stderr.extend(chunk[:stderr_remaining])
                        output_limit_hit = True
                        if not spec.capture_full_output_hash:
                            _kill_process_group(process.pid)
                            return
                        continue
                    captured.extend(chunk)
                    if is_stderr:
                        stderr_remaining = spec.max_output_bytes - len(captured_stderr)
                        if stderr_remaining > 0:
                            captured_stderr.extend(chunk[:stderr_remaining])

            stdout_task = asyncio.create_task(read_stream(process.stdout, is_stderr=False))
            stderr_task = asyncio.create_task(read_stream(process.stderr, is_stderr=True))
            wait_task = asyncio.create_task(process.wait())
            try:
                try:
                    await asyncio.wait_for(asyncio.shield(wait_task), spec.timeout_seconds)
                    timed_out = False
                except TimeoutError:
                    timed_out = True
                    _kill_process_group(process.pid)
                    await wait_task
                _, pending_readers = await asyncio.wait(
                    (stdout_task, stderr_task), timeout=0.2, return_when=asyncio.ALL_COMPLETED
                )
                if pending_readers:
                    # Do not let a child that inherited a pipe keep this request
                    # alive after the allowlisted command has exited.
                    _kill_process_group(process.pid)
                await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
            except asyncio.CancelledError:
                _kill_process_group(process.pid)
                await asyncio.gather(stdout_task, stderr_task, wait_task, return_exceptions=True)
                raise

            output = _normalize_output(
                bytes(captured),
                spec,
                sample_path,
                rule_path,
                directory,
                max_strings=cast(int, options.get("max_strings", 10_000)),
            )
            return_code = process.returncode
            duration = time.monotonic() - monotonic_start
            if (
                _allow_namespace_fallback
                and self._namespace_launcher is not None
                and return_code != 0
                and not timed_out
                and _is_namespace_launcher_error(
                    self._namespace_launcher[0], bytes(captured_stderr)
                )
            ):
                failed_launcher = self._namespace_launcher
                self._namespace_launcher = None
                failure_reason = NamespaceFailureReason.NAMESPACE_LAUNCHER_FAILED_DURING_EXECUTION
                self._namespace_detection = _NamespaceDetection(
                    NetworkIsolationStatus.SECCOMP_ONLY,
                    None,
                    (*self._namespace_detection.reasons, failure_reason),
                )
                _remember_failed_namespace_launcher(failed_launcher, failure_reason)
                return await self.run(
                    capability,
                    sample_sha256=sample_sha256,
                    sample_bytes=sample_bytes,
                    options=options,
                    _allow_namespace_fallback=False,
                )
            if timed_out:
                status = LocalCommandStatus.TIMEOUT
                detail = "timeout"
            elif return_code == 125:
                status = LocalCommandStatus.UNAVAILABLE
                detail = "isolation_unavailable"
            elif output_limit_hit or output.truncated:
                status = LocalCommandStatus.OUTPUT_TRUNCATED
                detail = "output_limit"
            elif return_code != 0:
                status = LocalCommandStatus.FAILED
                detail = "nonzero_exit"
            elif not output.text.strip():
                status = LocalCommandStatus.EMPTY
                detail = "empty_output"
            else:
                status = LocalCommandStatus.SUCCEEDED
                detail = None
            return _make_result(
                spec,
                status,
                sample_sha256,
                started_at,
                duration,
                "" if return_code == 125 else output.text,
                logical_argv,
                capability.version,
                detail,
                return_code,
                raw_output_sha256=(
                    raw_stdout_hash.hexdigest() if raw_stdout_hash is not None else None
                ),
                raw_output_bytes=raw_stdout_bytes,
                raw_output_complete=(
                    raw_stdout_hash is not None and not timed_out and return_code != 125
                ),
                raw_output_prefix=(bytes(captured) if raw_stdout_hash is not None else None),
            )

    def _wrap_network_namespace(
        self,
        argv: list[str],
        sample_path: Path,
        rule_path: Path | None,
        *,
        seccomp_fd: int | None = None,
    ) -> list[str]:
        assert self._namespace_launcher is not None
        launcher, executable = self._namespace_launcher
        if launcher == NetworkIsolationStatus.UNSHARE_NET.value:
            return [executable, "--net", "--", *argv]
        if seccomp_fd is None:
            raise ValueError("bwrap requires a seccomp filter file descriptor")
        return _bwrap_command(executable, argv, sample_path, rule_path, seccomp_fd=seccomp_fd)


@dataclass(frozen=True, slots=True)
class _NormalizedOutput:
    text: str
    truncated: bool


def _build_argv(
    spec: LocalCommandSpec,
    executable: str,
    sample_path: Path,
    rule_path: Path | None,
    options: Mapping[str, object],
) -> tuple[list[str], tuple[str, ...]]:
    path = str(sample_path)
    if spec.binary == "tshark" and options.get("field_catalog") is True:
        argv = [executable, "-G", "fields", "tls.handshake"]
    elif spec.binary == "tshark":
        fields: tuple[str, ...] = (
            "frame.number",
            "frame.time_epoch",
            "frame.len",
            "_ws.col.Protocol",
            "frame.protocols",
            "ip.src",
            "ip.dst",
            "ipv6.src",
            "ipv6.dst",
            "tcp.srcport",
            "tcp.dstport",
            "udp.srcport",
            "udp.dstport",
            "tcp.stream",
            "udp.stream",
            "dns.qry.name",
            "dns.flags.response",
            "http.host",
            "http.request.method",
            "http.request.uri",
            "tls.handshake.extensions_server_name",
        )
        if options.get("include_ja3") is True:
            fields = (*fields, "tls.handshake.ja3")
        argv = [
            executable,
            "-n",
            "-r",
            path,
            "-T",
            "fields",
            "-E",
            "quote=d",
            "-E",
            "occurrence=a",
        ]
        for field_name in fields:
            argv.extend(("-e", field_name))
    elif spec.binary == "capinfos":
        argv = [executable, "-M", "-c", "-a", "-e", "-t", "-u", path]
    elif spec.id is LocalCommandId.FILE:
        flag = "--mime-type" if options.get("mime_type") else "-b"
        argv = [executable, flag, path]
    elif spec.id is LocalCommandId.STRINGS:
        argv = [executable, "-a", "-n", str(options["min_length"]), path]
    elif spec.id is LocalCommandId.XXD:
        argv = [
            executable,
            "-g",
            "1",
            "-s",
            str(options["offset"]),
            "-l",
            str(options["length"]),
            path,
        ]
    elif spec.id is LocalCommandId.SHA256SUM:
        argv = [executable, path]
    elif spec.id is LocalCommandId.READELF:
        flag = {
            "header": "-h",
            "program_headers": "-l",
            "sections": "-S",
            "dynamic": "-d",
            "symbols": "-s",
        }[cast(str, options["mode"])]
        argv = [executable, flag, path]
    elif spec.id is LocalCommandId.OBJDUMP:
        flag = {
            "file_header": "-f",
            "sections": "-h",
            "private_headers": "-p",
            "disassemble": "-d",
        }[cast(str, options["mode"])]
        argv = [executable, flag, path]
    elif spec.id is LocalCommandId.YARA:
        if rule_path is None:
            raise ValueError("YARA rules were not staged")
        argv = [executable, str(rule_path), path]
    # L4: arguments for the optional extractors are fixed; only parser_id comes
    # from an application allowlist and is passed as one argv element.
    elif spec.id is LocalCommandId.FLOSS:
        argv = [executable, "--json", path]
    elif spec.id is LocalCommandId.MWCP:
        parser_id = options.get("parser_id")
        if not isinstance(parser_id, str) or not parser_id:
            raise ValueError("MWCP parser identifier was not validated")
        argv = [executable, "parse", parser_id, "--json", path]
    else:
        raise ValueError("Uncatalogued local command")
    logical = tuple(
        "<rules>"
        if rule_path is not None and argument == str(rule_path)
        else "<sample>"
        if argument == path
        else spec.binary
        if argument == executable
        else argument
        for argument in argv
    )
    return argv, logical


def _render_yara_rule(options: Mapping[str, object]) -> str:
    patterns = cast(tuple[str, ...], options["patterns"])
    strings = []
    for index, pattern in enumerate(patterns):
        encoded = " ".join(f"{byte:02X}" for byte in pattern.encode("utf-8"))
        strings.append(f"    $s{index} = {{ {encoded} }}")
    operator = "all" if options["match_mode"] == "all" else "any"
    return (
        "rule local_analysis_f1 {\n strings:\n"
        + "\n".join(strings)
        + f"\n condition:\n  {operator} of them\n}}\n"
    )


def _normalize_output(
    raw: bytes,
    spec: LocalCommandSpec,
    sample_path: Path,
    rule_path: Path | None,
    directory: Path,
    *,
    max_strings: int,
) -> _NormalizedOutput:
    text = raw.decode("utf-8", errors="replace")
    text = text.replace(str(sample_path), "<sample>")
    text = text.replace("/analysis/sample.bin", "<sample>")
    if rule_path is not None:
        text = text.replace(str(rule_path), "<rules>")
        text = text.replace("/analysis/rules.yar", "<rules>")
    text = text.replace(str(directory), "<analysis>")
    text = _CONTROL_CHARACTERS.sub("", text).replace("\r\n", "\n").replace("\r", "\n")
    text = _URL_TEXT.sub("<url-redacted>", text)
    text = _CREDENTIAL_ASSIGNMENT.sub("<credential-redacted>", text)
    text = _BEARER_TOKEN.sub("Bearer <credential-redacted>", text)
    text = _JWT_TOKEN.sub("<token-redacted>", text)
    text = _LONG_TOKEN.sub("<token-redacted>", text)
    text = _PRIVATE_KEY_BLOCK.sub("<private-key-redacted>", text)
    truncated = False
    if spec.output_kind in {LocalCommandOutputKind.LINES, LocalCommandOutputKind.MATCHES}:
        lines = text.splitlines()
        if len(lines) > max_strings:
            lines = lines[:max_strings]
            truncated = True
        text = "\n".join(lines)
    max_chars = spec.max_output_bytes
    if len(text.encode("utf-8")) > max_chars:
        encoded = text.encode("utf-8")[:max_chars]
        text = encoded.decode("utf-8", errors="ignore")
        truncated = True
    return _NormalizedOutput(text.strip(), truncated)


def _make_result(
    spec: LocalCommandSpec,
    status: LocalCommandStatus,
    sample_sha256: str,
    started_at: datetime,
    duration: float,
    output: str,
    normalized_command: tuple[str, ...],
    executable_version: str | None,
    detail_code: str | None,
    exit_code: int | None = None,
    *,
    raw_output_sha256: str | None = None,
    raw_output_bytes: int = 0,
    raw_output_complete: bool = False,
    raw_output_prefix: bytes | None = None,
) -> LocalCommandResult:
    return LocalCommandResult(
        command_id=spec.id,
        status=status,
        sample_sha256=sample_sha256,
        output=output,
        normalized_command=normalized_command,
        executable_version=executable_version,
        exit_code=exit_code,
        duration_seconds=max(0, duration),
        started_at=started_at,
        detail_code=detail_code,
        raw_output_sha256=raw_output_sha256,
        raw_output_bytes=raw_output_bytes,
        raw_output_complete=raw_output_complete,
        raw_output_prefix=raw_output_prefix,
    )


def _options_sha256(options: Mapping[str, object]) -> str:
    serialized = json.dumps(options, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(serialized.encode("ascii")).hexdigest()


@lru_cache(maxsize=4)
def _detect_network_namespace(timeout_seconds: float) -> _NamespaceDetection:
    environment = {"PATH": "/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"}
    bwrap = shutil.which("bwrap")
    reasons: list[NamespaceFailureReason] = []
    if bwrap:
        launcher = (NetworkIsolationStatus.BWRAP_NET.value, bwrap)
        if _namespace_launcher_failed(launcher):
            reasons.append(_namespace_launcher_failure_reason(launcher))
        else:
            probe_failure = _probe_bwrap_namespace(bwrap, environment, timeout_seconds)
            if probe_failure is None:
                return _NamespaceDetection(NetworkIsolationStatus.BWRAP_NET, launcher)
            reasons.append(probe_failure)
    else:
        reasons.append(NamespaceFailureReason.BWRAP_NOT_INSTALLED)

    unshare = shutil.which("unshare")
    if unshare:
        launcher = (NetworkIsolationStatus.UNSHARE_NET.value, unshare)
        if _namespace_launcher_failed(launcher):
            reasons.append(_namespace_launcher_failure_reason(launcher))
        elif _probe_namespace(
            [unshare, "--net", "--", "/usr/bin/true"], environment, timeout_seconds
        ):
            return _NamespaceDetection(NetworkIsolationStatus.UNSHARE_NET, launcher, tuple(reasons))
        else:
            reasons.append(NamespaceFailureReason.UNSHARE_PROBE_FAILED)
    else:
        reasons.append(NamespaceFailureReason.UNSHARE_NOT_INSTALLED)
    if not reasons:
        reasons.append(NamespaceFailureReason.NO_NETWORK_NAMESPACE_AVAILABLE)
    return _NamespaceDetection(NetworkIsolationStatus.SECCOMP_ONLY, None, tuple(reasons))


def _probe_bwrap_namespace(
    executable: str, environment: dict[str, str], timeout_seconds: float
) -> NamespaceFailureReason | None:
    """Measure network-syscall denial and an unmounted host path with runner mounts."""
    probe_program = "\n".join(
        (
            "import errno, socket",
            "try:",
            "    socket.socket()",
            "except OSError as error:",
            "    if error.errno not in (errno.EPERM, errno.EACCES): raise SystemExit(11)",
            "else:",
            "    raise SystemExit(10)",
            "try:",
            "    open('/etc/passwd', 'rb').close()",
            "except OSError:",
            "    pass",
            "else:",
            "    raise SystemExit(12)",
            "try:",
            "    if open('/analysis/sample.bin', 'rb').read(1) != b'p': raise SystemExit(13)",
            "except OSError:",
            "    raise SystemExit(13)",
        )
    )
    try:
        with tempfile.TemporaryDirectory(prefix="cti-bwrap-probe-") as temporary:
            directory = Path(temporary)
            sample_path = directory / "sample.bin"
            sample_path.write_bytes(b"probe")
            sample_path.chmod(0o400)
            rule_path = directory / "rules.yar"
            rule_path.write_text("rule probe { condition: true }\n", encoding="ascii")
            rule_path.chmod(0o400)
            with _network_seccomp_filter_file() as filter_file:
                seccomp_fd = filter_file.fileno()
                command = _bwrap_command(
                    executable,
                    [getattr(sys, "_base_executable", None) or sys.executable, "-c", probe_program],
                    sample_path,
                    rule_path,
                    seccomp_fd=seccomp_fd,
                )
                result = subprocess.run(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.PIPE,
                    timeout=timeout_seconds,
                    env=environment,
                    shell=False,
                    check=False,
                    pass_fds=(seccomp_fd,),
                )
    except (OSError, subprocess.SubprocessError, ValueError):
        return NamespaceFailureReason.BWRAP_PROBE_FAILED
    stderr = getattr(result, "stderr", b"")
    if isinstance(stderr, bytes):
        diagnostic = stderr[:4096].decode("utf-8", "replace").casefold()
    else:
        diagnostic = str(stderr)[:4096].casefold()
    if "namespace" in diagnostic and (
        "no permissions" in diagnostic
        or "operation not permitted" in diagnostic
        or ("user namespace" in diagnostic and "not allowed" in diagnostic)
    ):
        return NamespaceFailureReason.BWRAP_USERNS_REFUSED_BY_KERNEL
    return {
        0: None,
        10: NamespaceFailureReason.BWRAP_PROBE_NETWORK_SOCKET_ALLOWED,
        11: NamespaceFailureReason.BWRAP_PROBE_NETWORK_SOCKET_UNEXPECTED_ERROR,
        12: NamespaceFailureReason.BWRAP_PROBE_FILESYSTEM_NOT_CONFINED,
        13: NamespaceFailureReason.BWRAP_PROBE_SAMPLE_MOUNT_UNREADABLE,
    }.get(result.returncode, NamespaceFailureReason.BWRAP_PROBE_FAILED)


def _namespace_launcher_failed(launcher: tuple[str, str] | None) -> bool:
    if launcher is None:
        return False
    with _NAMESPACE_LAUNCHERS_LOCK:
        return launcher in _FAILED_NAMESPACE_LAUNCHERS


def _namespace_launcher_failure_reason(launcher: tuple[str, str]) -> NamespaceFailureReason:
    with _NAMESPACE_LAUNCHERS_LOCK:
        return _FAILED_NAMESPACE_LAUNCHER_REASONS.get(
            launcher,
            NamespaceFailureReason.BWRAP_PREVIOUSLY_FAILED
            if launcher[0] == NetworkIsolationStatus.BWRAP_NET.value
            else NamespaceFailureReason.UNSHARE_PREVIOUSLY_FAILED,
        )


def _remember_failed_namespace_launcher(
    launcher: tuple[str, str],
    reason: NamespaceFailureReason = (
        NamespaceFailureReason.NAMESPACE_LAUNCHER_FAILED_DURING_EXECUTION
    ),
) -> None:
    with _NAMESPACE_LAUNCHERS_LOCK:
        _FAILED_NAMESPACE_LAUNCHERS.add(launcher)
        _FAILED_NAMESPACE_LAUNCHER_REASONS[launcher] = reason
    _detect_network_namespace.cache_clear()


def _is_namespace_launcher_error(launcher: str, stderr: bytes) -> bool:
    prefix = {"bwrap-net": b"bwrap:", "unshare-net": b"unshare:"}.get(launcher)
    return prefix is not None and stderr.lstrip().startswith(prefix)


def _probe_namespace(
    command: list[str],
    env: dict[str, str],
    timeout_seconds: float,
    *,
    pass_fds: tuple[int, ...] = (),
) -> bool:
    try:
        result = subprocess.run(
            command,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            env=env,
            shell=False,
            check=False,
            pass_fds=pass_fds,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def _bwrap_command(
    executable: str,
    argv: list[str],
    sample_path: Path,
    rule_path: Path | None,
    *,
    seccomp_fd: int,
) -> list[str]:
    command = [
        executable,
        "--unshare-net",
        "--unshare-pid",
        "--unshare-ipc",
        "--unshare-uts",
        "--die-with-parent",
        "--seccomp",
        str(seccomp_fd),
        "--ro-bind",
        "/usr",
        "/usr",
    ]
    for system_path in ("/lib", "/lib64"):
        if Path(system_path).exists():
            command.extend(("--ro-bind", system_path, system_path))
    command.extend(("--dev", "/dev", "--proc", "/proc", "--tmpfs", "/tmp"))
    command.extend(("--dir", "/analysis", "--ro-bind", str(sample_path), "/analysis/sample.bin"))
    if rule_path is not None:
        command.extend(("--ro-bind", str(rule_path), "/analysis/rules.yar"))
        argv = ["/analysis/rules.yar" if item == str(rule_path) else item for item in argv]
    argv = ["/analysis/sample.bin" if item == str(sample_path) else item for item in argv]
    command.extend(("--chdir", "/analysis", "--", *argv))
    return command


def _network_seccomp_filter_file() -> BinaryIO:
    filter_file = tempfile.TemporaryFile(mode="w+b", prefix="cti-network-seccomp-")
    try:
        filter_file.write(network_seccomp_filter_program())
        filter_file.flush()
        filter_file.seek(0)
    except BaseException:
        filter_file.close()
        raise
    return filter_file


def _kill_process_group(process_id: int) -> None:
    try:
        os.killpg(process_id, signal.SIGKILL)
    except ProcessLookupError:
        pass


def _sandbox_bootstrap_argv(
    command: list[str],
    spec: LocalCommandSpec,
    *,
    install_network_filter_before_exec: bool = True,
) -> list[str]:
    filter_install = (
        "module.install_network_seccomp_filter()\n" if install_network_filter_before_exec else ""
    )
    bootstrap = (
        "import importlib.util, os, resource, sys\n"
        "cpu, memory, file_size = map(int, sys.argv[1:4])\n"
        "module_path = sys.argv[4]\n"
        "spec = importlib.util.spec_from_file_location('_cti_seccomp_network', module_path)\n"
        "if spec is None or spec.loader is None: raise SystemExit(125)\n"
        "module = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(module)\n"
        "command = sys.argv[6:]\n"
        "if sys.argv[5] != '--' or not command or "
        "not os.path.isabs(command[0]): raise SystemExit(125)\n"
        "resource.setrlimit(resource.RLIMIT_CPU, (cpu, cpu + 1))\n"
        "resource.setrlimit(resource.RLIMIT_AS, (memory, memory))\n"
        "resource.setrlimit(resource.RLIMIT_FSIZE, (file_size, file_size))\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))\n"
        "resource.setrlimit(resource.RLIMIT_CORE, (0, 0))\n"
        + filter_install
        + "environment = os.environ.copy()\n"
        "environment.pop('PYTHONPATH', None)\n"
        "os.execve(command[0], command, environment)\n"
    )
    return [
        sys.executable,
        "-c",
        bootstrap,
        str(spec.cpu_limit_seconds),
        str(spec.memory_limit_bytes),
        str(spec.file_size_limit_bytes),
        str(Path(__file__).with_name("seccomp_network.py")),
        "--",
        *command,
    ]


def _sandbox_exec(arguments: list[str], *, install_network_filter: bool = True) -> int:
    if len(arguments) < 5 or arguments[3] != "--":
        return 125
    try:
        cpu_limit = int(arguments[0])
        memory_limit = int(arguments[1])
        file_size_limit = int(arguments[2])
        command = arguments[4:]
        if not command or not Path(command[0]).is_absolute():
            return 125
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_limit, cpu_limit + 1))
        resource.setrlimit(resource.RLIMIT_AS, (memory_limit, memory_limit))
        resource.setrlimit(resource.RLIMIT_FSIZE, (file_size_limit, file_size_limit))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        if install_network_filter:
            _install_network_seccomp_filter()
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        os.execve(command[0], command, environment)
    except (OSError, ValueError, OverflowError):
        return 125
    return 125


if __name__ == "__main__" and len(sys.argv) > 1 and sys.argv[1] == "--cti-sandbox-exec":
    raise SystemExit(_sandbox_exec(sys.argv[2:]))
