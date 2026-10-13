from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import cti_app.infrastructure.local_command_runner as local_runner_module
from cti_app.application.investigation_extraction_tools import (
    FLOSS_TOOL,
    extraction_tool_capabilities,
    register_extraction_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.application.local_analysis import (
    LocalCommandCatalog,
    LocalCommandService,
    SampleBytesUnavailableError,
    extraction_command_specs,
)
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.local_analysis import LocalCommandSpec
from cti_app.infrastructure.floss_adapter import FlossAdapter
from cti_app.infrastructure.local_command_runner import LocalCommandRunner


class _Samples:
    def __init__(self, payload: bytes | None) -> None:
        self.payload = payload

    async def get_sample_bytes(self, sample_sha256: str, *, max_bytes: int) -> bytes:
        if self.payload is None:
            raise SampleBytesUnavailableError
        return self.payload


class _Associations:
    def __init__(self, sample_sha256: str) -> None:
        self.sample_sha256 = sample_sha256

    async def list_for_investigation(self, investigation_id: object) -> tuple[object, ...]:
        return (SimpleNamespace(sample_sha256=self.sample_sha256),)


class _Recorder:
    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def record_json(self, *, payload: Mapping[str, Any], **kwargs: object) -> SimpleNamespace:
        self.payloads.append(dict(payload))
        return SimpleNamespace(id=uuid4())


class _TestConfinedRunner(LocalCommandRunner):
    """Explicit test-only shim for deterministic fake-binary executions."""

    @property
    def filesystem_confined(self) -> bool:
        return True


def _fake_binary(
    path: Path,
    output: str,
    *,
    delay: str = "",
    version: str = "fake-floss 1.2.3",
) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"--version\" ]; then printf '%s\\n' '{version}'; exit 0; fi\n"
        f"{delay}"
        f"printf '%s\\n' '{output}'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _context(sample_sha256: str) -> SimpleNamespace:
    return SimpleNamespace(
        investigation_id=uuid4(),
        idempotency_key="floss-test-invocation",
        available_capabilities=frozenset({FLOSS_TOOL}),
    )


def _test_bootstrap_argv(command: list[str], spec: LocalCommandSpec) -> list[str]:
    source_root = Path(__file__).resolve().parents[1] / "src"
    bootstrap = (
        "import sys; "
        f"sys.path[:0] = {[str(source_root), *sys.path]!r}; "
        "from cti_app.infrastructure.local_command_runner import _sandbox_exec; "
        "raise SystemExit(_sandbox_exec(sys.argv[1:]))"
    )
    return [
        sys.executable,
        "-c",
        bootstrap,
        str(spec.cpu_limit_seconds),
        str(spec.memory_limit_bytes),
        str(spec.file_size_limit_bytes),
        "--",
        *command,
    ]


def _service(
    binary: Path,
    samples: _Samples,
    *,
    timeout_seconds: float | None = None,
    max_output_bytes: int | None = None,
    runner: LocalCommandRunner | None = None,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[LocalCommandService, LocalCommandCatalog]:
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    monkeypatch.setattr(local_runner_module, "_sandbox_bootstrap_argv", _test_bootstrap_argv)
    catalog = LocalCommandCatalog(
        specs=extraction_command_specs(),
        which=lambda _name: str(binary),
        timeout_seconds=timeout_seconds,
        max_output_bytes=max_output_bytes,
    )
    return (
        LocalCommandService(
            samples,
            runner or _TestConfinedRunner(require_filesystem_confinement=False),
            catalog,
            enabled=True,
            max_sample_bytes=1024 * 1024,
        ),
        catalog,
    )


def _registered(
    service: LocalCommandService,
    catalog: LocalCommandCatalog,
    sample_sha256: str,
) -> tuple[InMemoryToolRegistry, FlossAdapter, _Recorder]:
    port = FlossAdapter(service, catalog, enabled=True)
    recorder = _Recorder()
    registry = InMemoryToolRegistry()
    register_extraction_tools(
        registry,
        sample_associations=_Associations(sample_sha256),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        floss=port,
    )
    return registry, port, recorder


@pytest.mark.asyncio
async def test_floss_is_catalogued_confined_normalized_and_redacts_secrets(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"harmless fixture"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    raw_output = json.dumps(
        {
            "strings": [
                {
                    "string": "decoded text",
                    "type": "decoded",
                    "encoding": "UTF-8",
                    "offset": 32,
                    "address": "0x401020",
                    "function": "decode_config",
                    "context": "called from entry",
                },
                {
                    "string": "TOKEN=do-not-persist /home/analyst/private.bin",
                    "type": "stack",
                },
            ]
        },
        separators=(",", ":"),
    )
    binary = _fake_binary(
        tmp_path / "floss",
        raw_output,
        version="fake-floss 1.2.3 /home/analyst TOKEN=version-secret",
    )
    service, catalog = _service(binary, _Samples(sample), monkeypatch=monkeypatch)
    registry, port, recorder = _registered(service, catalog, sample_sha256)

    assert port.available
    assert port.version == "unverified"
    assert FLOSS_TOOL in extraction_tool_capabilities(
        floss=port, configuration=None, report_import_enabled=False
    )
    definition, adapter = registry.get(FLOSS_TOOL, "1")  # type: ignore[misc]
    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "max_strings": 16}, _context(sample_sha256)
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, {
        key: result.output.get(key)
        for key in (
            "tool_version",
            "raw_output_bytes",
            "raw_output_sha256",
            "exit_code",
            "detail_code",
            "strings",
        )
    }
    strings = result.output["strings"]
    assert isinstance(strings, Sequence)
    assert isinstance(strings[0], Mapping)
    assert strings[0]["type"] == "decoded"
    assert strings[0]["encoding"] == "UTF-8"
    assert strings[0]["offset"] == 32
    assert strings[0]["address"] == "0x401020"
    assert result.output["untrusted_data"] is True
    rendered = repr(result.output)
    assert "do-not-persist" not in rendered
    assert "version-secret" not in rendered
    assert "/home/analyst" not in rendered
    assert definition.schema_version == "1"
    assert recorder.payloads[0]["raw_output_complete"] is True
    assert (
        recorder.payloads[0]["raw_output_sha256"]
        == hashlib.sha256((raw_output + "\n").encode()).hexdigest()
    )


@pytest.mark.asyncio
async def test_floss_is_unavailable_without_filesystem_confinement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"safe fixture"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    binary = _fake_binary(tmp_path / "floss", '{"strings":[]}')
    # The runner probes for bwrap at construction time: neutralise the probe first so the
    # result does not depend on whether the host provides filesystem confinement.
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    service, catalog = _service(
        binary,
        _Samples(sample),
        runner=LocalCommandRunner(require_filesystem_confinement=False),
        monkeypatch=monkeypatch,
    )
    registry, port, recorder = _registered(service, catalog, sample_sha256)

    assert not port.available
    assert port.unavailable_reason == "filesystem_confinement_required"
    assert FLOSS_TOOL not in extraction_tool_capabilities(
        floss=port, configuration=None, report_import_enabled=False
    )
    registration = registry.get(FLOSS_TOOL, "1")
    assert registration is not None
    _definition, adapter = registration
    result = await adapter.execute({"sample_sha256": sample_sha256}, _context(sample_sha256))

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["reason"] == "filesystem_confinement_required"
    assert recorder.payloads == []


@pytest.mark.asyncio
async def test_floss_large_output_is_marked_and_hashes_complete_stdout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"safe"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    raw_output = json.dumps(
        {"strings": [{"string": f"static-{index}", "type": "static"} for index in range(1200)]},
        separators=(",", ":"),
    )
    binary = _fake_binary(tmp_path / "floss", raw_output)
    service, catalog = _service(
        binary,
        _Samples(sample),
        max_output_bytes=512,
        monkeypatch=monkeypatch,
    )
    registry, _port, recorder = _registered(service, catalog, sample_sha256)
    _definition, adapter = registry.get(FLOSS_TOOL, "1")  # type: ignore[misc]

    result = await adapter.execute({"sample_sha256": sample_sha256}, _context(sample_sha256))

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["output_truncated"] is True
    assert "TRUNCATED" in result.output["truncation_marker"]
    assert result.output["raw_output_complete"] is True
    expected = hashlib.sha256((raw_output + "\n").encode()).hexdigest()
    assert result.output["raw_output_sha256"] == expected
    assert recorder.payloads[0]["raw_output_sha256"] == expected


@pytest.mark.asyncio
async def test_floss_field_truncations_are_marked_and_counted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"safe fixture"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    raw_output = json.dumps(
        {"strings": [{"string": "x" * 8_100, "context": "y" * 1_100}]},
        separators=(",", ":"),
    )
    binary = _fake_binary(tmp_path / "floss", raw_output)
    service, catalog = _service(binary, _Samples(sample), monkeypatch=monkeypatch)
    registry, _port, recorder = _registered(service, catalog, sample_sha256)
    registration = registry.get(FLOSS_TOOL, "1")
    assert registration is not None
    _definition, adapter = registration

    result = await adapter.execute({"sample_sha256": sample_sha256}, _context(sample_sha256))

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["truncated_field_count"] == 2
    assert tuple(result.output["strings"][0]["truncated_fields"]) == ("context", "value")
    assert result.output["strings"][0]["truncated"] is True
    assert recorder.payloads[0]["truncated_field_count"] == 2
    assert recorder.payloads[0]["status"] == "partial"


@pytest.mark.asyncio
async def test_floss_timeout_and_missing_binary_are_unavailable_without_secret_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"safe"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    binary = _fake_binary(tmp_path / "floss", '{"strings":[]}', delay="sleep 2\n")
    service, catalog = _service(
        binary,
        _Samples(sample),
        timeout_seconds=0.1,
        monkeypatch=monkeypatch,
    )
    registry, _port, _recorder = _registered(service, catalog, sample_sha256)
    _definition, adapter = registry.get(FLOSS_TOOL, "1")  # type: ignore[misc]

    timeout_result = await adapter.execute(
        {"sample_sha256": sample_sha256}, _context(sample_sha256)
    )
    assert timeout_result.status is ToolInvocationStatus.PARTIAL
    assert timeout_result.output["raw_output_complete"] is False

    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    absent_catalog = LocalCommandCatalog(specs=extraction_command_specs(), which=lambda _name: None)
    absent_service = LocalCommandService(
        _Samples(sample),
        _TestConfinedRunner(require_filesystem_confinement=False),
        absent_catalog,
        enabled=True,
    )
    absent_port = FlossAdapter(absent_service, absent_catalog, enabled=True)
    assert not absent_port.available
    assert FLOSS_TOOL not in extraction_tool_capabilities(
        floss=absent_port, configuration=None, report_import_enabled=False
    )
    unavailable = await absent_port.extract(sample_sha256, max_strings=8)
    assert unavailable.status.value == "unavailable"


@pytest.mark.asyncio
async def test_floss_refuses_absent_or_oversized_sample_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"this is larger than configured sample bound"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    binary = _fake_binary(tmp_path / "floss", '{"strings":[]}')
    _service_instance, catalog = _service(binary, _Samples(sample), monkeypatch=monkeypatch)
    oversized_service = LocalCommandService(
        _Samples(sample),
        _TestConfinedRunner(require_filesystem_confinement=False),
        catalog,
        enabled=True,
        max_sample_bytes=4,
    )
    oversized_port = FlossAdapter(oversized_service, catalog, enabled=True)
    denied = await oversized_port.extract(sample_sha256, max_strings=4)
    assert denied.status.value == "policy_denied"
    assert denied.detail_code == "sample_too_large"

    absent_service = LocalCommandService(
        _Samples(None),
        _TestConfinedRunner(require_filesystem_confinement=False),
        catalog,
        enabled=True,
    )
    absent_port = FlossAdapter(absent_service, catalog, enabled=True)
    unavailable = await absent_port.extract(sample_sha256, max_strings=4)
    assert unavailable.status.value == "unavailable"
