from __future__ import annotations

import hashlib
import struct
from dataclasses import replace
from pathlib import Path

import pytest

import cti_app.infrastructure.local_command_runner as local_runner_module
from cti_app.application.local_analysis import (
    LocalCommandCatalog,
    LocalCommandService,
    SampleBytesUnavailableError,
    default_local_command_specs,
)
from cti_app.domain.local_analysis import (
    LocalAnalysisFileType,
    LocalCommandId,
    LocalCommandStatus,
)
from cti_app.infrastructure.local_command_runner import LocalCommandRunner


class _Samples:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0

    async def get_sample_bytes(self, sample_sha256: str, *, max_bytes: int) -> bytes:
        self.reads += 1
        if not self.payload:
            raise SampleBytesUnavailableError
        assert len(self.payload) <= max_bytes
        return self.payload


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _elf_fixture() -> bytes:
    ident = b"\x7fELF" + bytes((2, 1, 1, 0)) + bytes(8)
    header = struct.pack(
        "<HHIQQQIHHHHHH",
        2,
        62,
        1,
        0,
        0,
        0,
        0,
        64,
        0,
        0,
        0,
        0,
        0,
    )
    return ident + header + b"\0SAFE_LOCAL_SAMPLE_MARKER\0"


def _pe_fixture() -> bytes:
    dos_header = bytearray(64)
    dos_header[:2] = b"MZ"
    dos_header[0x3C:0x40] = (64).to_bytes(4, "little")
    coff_header = struct.pack("<HHIIIHH", 0x14C, 0, 0, 0, 0, 0, 0x0102)
    return bytes(dos_header) + b"PE\0\0" + coff_header


def test_bubblewrap_is_preferred_and_reports_filesystem_confinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    probed: list[str] = []
    executables = {"bwrap": "/test/bwrap", "unshare": "/test/unshare"}
    monkeypatch.setattr(local_runner_module.shutil, "which", executables.get)

    def probe_bwrap(*_args: object) -> bool:
        probed.append("bwrap")
        return True

    def probe_namespace(command: list[str], *_args: object) -> bool:
        probed.append("unshare" if command[0] == "/test/unshare" else "other")
        return True

    monkeypatch.setattr(local_runner_module, "_probe_bwrap_namespace", probe_bwrap)
    monkeypatch.setattr(local_runner_module, "_probe_namespace", probe_namespace)
    local_runner_module._detect_network_namespace.cache_clear()
    try:
        runner = LocalCommandRunner(network_sandbox_probe_seconds=0.37)

        assert runner.network_isolation == "bwrap-net"
        assert runner.filesystem_confined is True
        assert probed == ["bwrap"]

        probed.clear()

        def unavailable_bwrap(*_args: object) -> bool:
            probed.append("bwrap")
            return False

        monkeypatch.setattr(local_runner_module, "_probe_bwrap_namespace", unavailable_bwrap)
        fallback = LocalCommandRunner(network_sandbox_probe_seconds=0.38)
        assert fallback.network_isolation == "unshare-net"
        assert fallback.filesystem_confined is False
        assert probed == ["bwrap", "unshare"]
    finally:
        local_runner_module._detect_network_namespace.cache_clear()


@pytest.mark.asyncio
async def test_required_filesystem_confinement_refuses_execution_when_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "tool-executed"
    tool = tmp_path / "file"
    tool.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then printf \'fake-file 1.0\\n\'; exit 0; fi\n'
        f"touch '{marker}'\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    monkeypatch.setattr(local_runner_module.shutil, "which", lambda _: None)
    local_runner_module._detect_network_namespace.cache_clear()
    try:
        payload = b"safe"
        samples = _Samples(payload)
        catalog = LocalCommandCatalog(
            specs=(default_local_command_specs()[0],), which=lambda _: str(tool)
        )
        runner = LocalCommandRunner(
            network_sandbox_probe_seconds=0.2,
            require_filesystem_confinement=True,
        )
        service = LocalCommandService(samples, runner, catalog, enabled=True)

        result = await service.run(LocalCommandId.FILE, _sha256(payload))

        assert result.status is LocalCommandStatus.UNAVAILABLE
        assert result.detail_code == "filesystem_confinement_unavailable"
        assert runner.filesystem_confined is False
        assert service.filesystem_confined is False
        assert samples.reads == 1
        assert not marker.exists()
    finally:
        local_runner_module._detect_network_namespace.cache_clear()


@pytest.mark.asyncio
async def test_catalogued_commands_read_synthetic_elf_with_bounded_output() -> None:
    payload = _elf_fixture()
    samples = _Samples(payload)
    runner = LocalCommandRunner(network_sandbox_probe_seconds=0.2)
    catalog = LocalCommandCatalog(
        timeout_seconds=5,
        max_output_bytes=32_768,
        cpu_limit_seconds=10,
        memory_limit_bytes=512 * 1024 * 1024,
    )
    service = LocalCommandService(samples, runner, catalog, enabled=True)
    sample_hash = _sha256(payload)

    file_result = await service.run(LocalCommandId.FILE, sample_hash)
    strings_result = await service.run(LocalCommandId.STRINGS, sample_hash)
    readelf_result = await service.run(LocalCommandId.READELF, sample_hash)
    objdump_result = await service.run(LocalCommandId.OBJDUMP, sample_hash)
    yara_result = await service.run(
        LocalCommandId.YARA,
        sample_hash,
        {"patterns": ["SAFE_LOCAL_SAMPLE_MARKER"], "match_mode": "all"},
    )

    for command_id, result in (
        (LocalCommandId.FILE, file_result),
        (LocalCommandId.STRINGS, strings_result),
        (LocalCommandId.READELF, readelf_result),
        (LocalCommandId.OBJDUMP, objdump_result),
        (LocalCommandId.YARA, yara_result),
    ):
        capability = catalog.get(command_id)
        if capability is None or not capability.available:
            assert result.status is LocalCommandStatus.UNAVAILABLE
        else:
            assert result.status in {
                LocalCommandStatus.SUCCEEDED,
                LocalCommandStatus.EMPTY,
                LocalCommandStatus.TIMEOUT,
                LocalCommandStatus.OUTPUT_TRUNCATED,
            }, (
                command_id,
                repr(result.archive_record()),
            )
            assert len(result.output.encode()) <= capability.spec.max_output_bytes
            assert result.duration_seconds <= capability.spec.timeout_seconds + 1
            assert "cti-local-analysis-" not in result.output
    if file_result.status is LocalCommandStatus.SUCCEEDED:
        assert "ELF" in file_result.output
    if strings_result.status is LocalCommandStatus.SUCCEEDED:
        assert "SAFE_LOCAL_SAMPLE_MARKER" in strings_result.output
    if readelf_result.status is LocalCommandStatus.SUCCEEDED:
        assert "ELF Header" in readelf_result.output
    if yara_result.status is LocalCommandStatus.SUCCEEDED:
        assert "local_analysis_f1" in yara_result.output
    # Optional CLI binaries differ across development machines and CI runners.
    # Only tools present in the runtime catalogue read the sample bytes.
    reading_commands = (
        LocalCommandId.FILE,
        LocalCommandId.STRINGS,
        LocalCommandId.READELF,
        LocalCommandId.OBJDUMP,
        LocalCommandId.YARA,
    )
    available_readers = sum(
        1
        for command_id in reading_commands
        if (capability := catalog.get(command_id)) is not None and capability.available
    )
    assert samples.reads == available_readers
    assert service.estimate_budget(LocalCommandId.READELF).tool_seconds > 0


@pytest.mark.asyncio
async def test_pe_header_is_recognized_and_elf_tools_refuse_other_types() -> None:
    payload = _pe_fixture()
    samples = _Samples(payload)
    catalog = LocalCommandCatalog()
    service = LocalCommandService(
        samples,
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        catalog,
        enabled=True,
    )

    result = await service.run(LocalCommandId.READELF, _sha256(payload))

    assert result.status is LocalCommandStatus.POLICY_DENIED
    assert result.detail_code == "unsupported_file_type"
    assert LocalAnalysisFileType.PE in catalog.get(LocalCommandId.OBJDUMP).spec.accepted_file_types  # type: ignore[union-attr]


@pytest.mark.asyncio
async def test_unknown_command_and_options_are_policy_denied_before_sample_read() -> None:
    samples = _Samples(_elf_fixture())
    service = LocalCommandService(
        samples,
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        LocalCommandCatalog(),
        enabled=True,
    )
    sample_hash = _sha256(samples.payload)

    free_command = await service.run("bash -c 'touch /tmp/nope'", sample_hash)
    free_option = await service.run(
        LocalCommandId.FILE,
        sample_hash,
        {"filename": "; rm -rf /", "args": ["$(touch /tmp/nope)"]},
    )

    assert free_command.status is LocalCommandStatus.POLICY_DENIED
    assert free_option.status is LocalCommandStatus.POLICY_DENIED
    assert samples.reads == 0


@pytest.mark.asyncio
async def test_sample_hash_mismatch_and_missing_binary_are_local_results() -> None:
    payload = _elf_fixture()
    samples = _Samples(payload)
    spec = default_local_command_specs()[0]
    missing_catalog = LocalCommandCatalog(specs=(spec,), which=lambda _: None)
    service = LocalCommandService(
        samples,
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        missing_catalog,
        enabled=True,
    )
    available_service = LocalCommandService(
        samples,
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        LocalCommandCatalog(),
        enabled=True,
    )

    unavailable = await service.run(LocalCommandId.FILE, _sha256(payload))
    mismatched = await available_service.run(LocalCommandId.FILE, "0" * 64)

    assert unavailable.status is LocalCommandStatus.UNAVAILABLE
    assert unavailable.detail_code == "binary_unavailable"
    assert mismatched.status is LocalCommandStatus.FAILED
    assert mismatched.detail_code == "sample_hash_mismatch"


@pytest.mark.asyncio
async def test_metacharacters_in_sample_strings_are_inert_and_filenames_are_fixed(
    tmp_path: Path,
) -> None:
    marker = tmp_path / "unexpected-file"
    payload = (
        f"NORMAL ; touch {marker} $(touch {marker}) ../trap-name; rm -rf / "
        "https://example.invalid/file?sig=signed-secret api_key="
        "1234567890123456789012345678901234567890"
    ).encode()
    catalog = LocalCommandCatalog(max_output_bytes=4096)
    service = LocalCommandService(
        _Samples(payload),
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        catalog,
        enabled=True,
    )

    result = await service.run(LocalCommandId.STRINGS, _sha256(payload))

    if result.status is LocalCommandStatus.SUCCEEDED:
        assert "; touch" in result.output
        assert "https://" not in result.output
        assert "signed-secret" not in result.output
        assert "1234567890123456789012345678901234567890" not in result.output
    assert not marker.exists()
    assert "<sample>" in result.normalized_command
    assert str(marker) not in result.normalized_command


@pytest.mark.asyncio
async def test_output_limit_is_reported_as_truncated(tmp_path: Path) -> None:
    script = tmp_path / "file"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then printf \'fake-file 1.0\\n\'; exit 0; fi\n'
        "printf '%10000s' X\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    spec = replace(
        default_local_command_specs()[0],
        timeout_seconds=30,
        max_output_bytes=128,
        cpu_limit_seconds=5,
    )
    catalog = LocalCommandCatalog(specs=(spec,), which=lambda _: str(script))
    payload = b"not an executable"
    service = LocalCommandService(
        _Samples(payload),
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        catalog,
        enabled=True,
    )

    result = await service.run(LocalCommandId.FILE, _sha256(payload))

    assert result.status is LocalCommandStatus.OUTPUT_TRUNCATED
    assert len(result.output.encode()) <= spec.max_output_bytes
    assert result.archive_record()["sample_sha256"] == _sha256(payload)


@pytest.mark.asyncio
async def test_timeout_is_bounded(tmp_path: Path) -> None:
    script = tmp_path / "file"
    script.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then printf \'fake-file 1.0\\n\'; exit 0; fi\n'
        "sleep 5\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    spec = replace(default_local_command_specs()[0], timeout_seconds=0.2, cpu_limit_seconds=5)
    catalog = LocalCommandCatalog(specs=(spec,), which=lambda _: str(script))
    payload = b"safe"
    service = LocalCommandService(
        _Samples(payload),
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        catalog,
        enabled=True,
    )

    result = await service.run(LocalCommandId.FILE, _sha256(payload))

    assert result.status is LocalCommandStatus.TIMEOUT
    assert result.duration_seconds < 2


@pytest.mark.asyncio
async def test_process_has_no_proxy_or_key_environment_and_socket_calls_are_blocked(
    tmp_path: Path,
) -> None:
    script = tmp_path / "file"
    script.write_text(
        "#!/usr/bin/python3\n"
        "import os, socket, sys\n"
        "if '--version' in sys.argv: print('fake-file 1.0'); raise SystemExit(0)\n"
        "bad = [k for k in os.environ if 'proxy' in k.lower()]\n"
        "keys = ('key', 'token', 'secret')\n"
        "bad += [k for k in os.environ if any(item in k.lower() for item in keys)]\n"
        "print('env-clean' if not bad else 'env-leak')\n"
        "try: socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        "except OSError as error: print(f'network-blocked:{error.errno}')\n"
        "else: print('network-available')\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    spec = default_local_command_specs()[0]
    catalog = LocalCommandCatalog(specs=(spec,), which=lambda _: str(script))
    payload = b"safe"
    service = LocalCommandService(
        _Samples(payload),
        LocalCommandRunner(network_sandbox_probe_seconds=0.2),
        catalog,
        enabled=True,
    )

    result = await service.run(LocalCommandId.FILE, _sha256(payload))

    assert result.status is LocalCommandStatus.SUCCEEDED
    assert "env-clean" in result.output
    assert "network-blocked:1" in result.output
    assert "network-available" not in result.output


@pytest.mark.asyncio
async def test_namespace_launcher_failure_retries_once_with_seccomp(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launcher = tmp_path / "bwrap"
    invocations = tmp_path / "bwrap-invocations"
    launcher.write_text(
        "#!/bin/sh\n"
        f"printf '%s\\n' \"$*\" >> '{invocations}'\n"
        'case "$*" in *"/usr/bin/true"*) exit 0 ;; esac\n'
        "printf '%s\\n' 'bwrap: loopback: Failed to look up lo: No such file or directory' >&2\n"
        "exit 1\n",
        encoding="utf-8",
    )
    launcher.chmod(0o755)
    tool = tmp_path / "file"
    tool.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then printf \'fake-file 1.0\\n\'; exit 0; fi\n'
        "printf 'isolated-result\\n'\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)

    monkeypatch.setattr(
        local_runner_module.shutil,
        "which",
        lambda name: str(launcher) if name == "bwrap" else None,
    )
    local_runner_module._detect_network_namespace.cache_clear()
    catalog = LocalCommandCatalog(
        specs=(default_local_command_specs()[0],), which=lambda _: str(tool)
    )
    runner = LocalCommandRunner(network_sandbox_probe_seconds=1.5)
    payload = b"safe"
    service = LocalCommandService(_Samples(payload), runner, catalog, enabled=True)

    result = await service.run(LocalCommandId.FILE, _sha256(payload))

    assert result.status is LocalCommandStatus.SUCCEEDED
    assert "isolated-result" in result.output
    assert runner.network_isolation == "seccomp-network-syscall-filter"
    calls = invocations.read_text(encoding="utf-8").splitlines()
    assert len(calls) == 2
    assert "--unshare-net" in calls[0]
    assert "--dir /analysis" in calls[0]
    assert "--chdir /analysis" in calls[0]
    assert "sample.bin" in calls[0]
    assert "rules.yar" in calls[0]

    # A runtime failure is remembered for later runner instances in this process.
    assert LocalCommandRunner(network_sandbox_probe_seconds=1.5).network_isolation == (
        "seccomp-network-syscall-filter"
    )
    local_runner_module._detect_network_namespace.cache_clear()


@pytest.mark.asyncio
async def test_unavailable_isolation_never_executes_the_tool(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    marker = tmp_path / "tool-executed"
    tool = tmp_path / "file"
    tool.write_text(
        "#!/bin/sh\n"
        'if [ "$1" = "--version" ]; then printf \'fake-file 1.0\\n\'; exit 0; fi\n'
        f"touch '{marker}'\n",
        encoding="utf-8",
    )
    tool.chmod(0o755)
    fake_python = tmp_path / "python-without-seccomp"
    fake_python.write_text("#!/bin/sh\nexit 125\n", encoding="utf-8")
    fake_python.chmod(0o755)

    monkeypatch.setattr(local_runner_module.shutil, "which", lambda _: None)
    local_runner_module._detect_network_namespace.cache_clear()
    catalog = LocalCommandCatalog(
        specs=(default_local_command_specs()[0],), which=lambda _: str(tool)
    )
    runner = LocalCommandRunner(network_sandbox_probe_seconds=0.2)
    monkeypatch.setattr(local_runner_module.sys, "executable", str(fake_python))
    payload = b"safe"
    service = LocalCommandService(_Samples(payload), runner, catalog, enabled=True)

    result = await service.run(LocalCommandId.FILE, _sha256(payload))

    assert result.status is LocalCommandStatus.UNAVAILABLE
    assert result.detail_code == "isolation_unavailable"
    assert not marker.exists()
    local_runner_module._detect_network_namespace.cache_clear()
