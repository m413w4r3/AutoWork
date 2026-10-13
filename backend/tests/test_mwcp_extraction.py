from __future__ import annotations

import hashlib
import json
import sys
from collections.abc import Mapping
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

import cti_app.infrastructure.local_command_runner as local_runner_module
from cti_app.application.investigation_extraction_tools import (
    MWCP_TOOL,
    _configuration_value_json,
    extraction_tool_capabilities,
    register_extraction_tools,
)
from cti_app.application.investigation_tools import InMemoryToolRegistry
from cti_app.application.local_analysis import (
    LocalCommandCatalog,
    LocalCommandService,
    extraction_command_specs,
)
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.local_analysis import LocalCommandSpec
from cti_app.infrastructure.local_command_runner import LocalCommandRunner
from cti_app.infrastructure.mwcp_adapter import MwcpAdapter, _normalize_configuration


class _Samples:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    async def get_sample_bytes(self, sample_sha256: str, *, max_bytes: int) -> bytes:
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


def _fake_mwcp(
    path: Path,
    output: str,
    marker: Path,
    *,
    version: str = "fake-mwcp 2.0.0",
) -> Path:
    path.write_text(
        "#!/bin/sh\n"
        f"if [ \"$1\" = \"--version\" ]; then printf '%s\\n' '{version}'; exit 0; fi\n"
        f": > '{marker}'\n"
        f"printf '%s\\n' '{output}'\n",
        encoding="utf-8",
    )
    path.chmod(0o755)
    return path


def _context(sample_sha256: str) -> SimpleNamespace:
    return SimpleNamespace(
        investigation_id=uuid4(),
        idempotency_key="mwcp-test-invocation",
        available_capabilities=frozenset({MWCP_TOOL}),
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


@pytest.mark.asyncio
async def test_mwcp_runs_only_allowlisted_parser_and_normalizes_typed_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"harmless fixture"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    marker = tmp_path / "parser-executed"
    raw_output = json.dumps(
        {
            "metadata": {"parser": "demo.config", "version": "1.4"},
            "configuration": {
                "c2": "HTTPS://C2.Example.COM/path?access_token=hidden",
                "callback": "https://[2001:db8::1]/poll?token=hidden-ip-token",
                "malformed_endpoint": "https://[malformed",
                "port": 443,
                "enabled": True,
                "token": "do-not-persist",
                "/home/analyst/private": "safe value",
            },
        },
        separators=(",", ":"),
    )
    binary = _fake_mwcp(
        tmp_path / "mwcp",
        raw_output,
        marker,
        version="fake-mwcp 2.0 /home/analyst TOKEN=version-secret",
    )
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    monkeypatch.setattr(local_runner_module, "_sandbox_bootstrap_argv", _test_bootstrap_argv)
    parser_versions = {"demo.config": "1.4"}
    catalog = LocalCommandCatalog(
        specs=extraction_command_specs(mwcp_parser_ids=tuple(parser_versions)),
        which=lambda _name: str(binary),
    )
    service = LocalCommandService(
        _Samples(sample),  # type: ignore[arg-type]
        _TestConfinedRunner(require_filesystem_confinement=False),
        catalog,
        enabled=True,
    )
    port = MwcpAdapter(service, catalog, parser_versions=parser_versions, enabled=True)
    recorder = _Recorder()
    registry = InMemoryToolRegistry()
    register_extraction_tools(
        registry,
        sample_associations=_Associations(sample_sha256),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        configuration=port,
    )
    assert MWCP_TOOL in extraction_tool_capabilities(
        floss=None, configuration=port, report_import_enabled=False
    )
    definition, adapter = registry.get(MWCP_TOOL, "1")  # type: ignore[misc]

    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "parser_id": "demo.config"},
        _context(sample_sha256),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert marker.exists()
    assert definition.schema_version == "1"
    assert result.output["parser_id"] == "demo.config"
    assert result.output["parser_version"] == "1.4"
    indicators = result.output["network_indicators"]
    assert indicators[0]["normalized_value"] == "https c2.example.com/path"
    assert indicators[0]["scheme"] == "https"
    assert indicators[0]["host"] == "c2.example.com"
    assert indicators[1]["host"] == "2001:db8::1"
    assert indicators[1]["path"] == "/poll"
    assert len(indicators) == 2
    serialized = repr(result.output)
    assert "hidden" not in serialized
    assert "hidden-ip-token" not in serialized
    assert "do-not-persist" not in serialized
    assert "version-secret" not in serialized
    assert "/home/analyst" not in serialized
    assert (
        recorder.payloads[0]["raw_output_sha256"]
        == hashlib.sha256((raw_output + "\n").encode()).hexdigest()
    )
    assert (
        "An absent extraction does not establish the absence of configuration."
        in result.output["limitations"]
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("parser_id", ["unauthorized.parser", 17, None])
async def test_mwcp_rejects_parser_outside_operator_allowlist_before_execution(
    parser_id: str | int | None, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"safe"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    marker = tmp_path / "parser-executed"
    binary = _fake_mwcp(tmp_path / "mwcp", '{"configuration":{}}', marker)
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    monkeypatch.setattr(local_runner_module, "_sandbox_bootstrap_argv", _test_bootstrap_argv)
    allowed = {"only.this.parser": "1.0"}
    catalog = LocalCommandCatalog(
        specs=extraction_command_specs(mwcp_parser_ids=tuple(allowed)),
        which=lambda _name: str(binary),
    )
    service = LocalCommandService(
        _Samples(sample),  # type: ignore[arg-type]
        _TestConfinedRunner(require_filesystem_confinement=False),
        catalog,
        enabled=True,
    )
    port = MwcpAdapter(service, catalog, parser_versions=allowed, enabled=True)
    registry = InMemoryToolRegistry()
    recorder = _Recorder()
    register_extraction_tools(
        registry,
        sample_associations=_Associations(sample_sha256),  # type: ignore[arg-type]
        recorder=recorder,  # type: ignore[arg-type]
        configuration=port,
    )
    _definition, adapter = registry.get(MWCP_TOOL, "1")  # type: ignore[misc]

    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "parser_id": parser_id},
        _context(sample_sha256),
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.output["reason"] == "parser_not_allowlisted"
    assert not marker.exists()
    assert recorder.payloads == []


@pytest.mark.asyncio
async def test_mwcp_empty_extraction_states_configuration_absence_is_unproven(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"safe"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    marker = tmp_path / "parser-executed"
    binary = _fake_mwcp(tmp_path / "mwcp", '{"configuration":{}}', marker)
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    monkeypatch.setattr(local_runner_module, "_sandbox_bootstrap_argv", _test_bootstrap_argv)
    allowed = {"empty.parser": "2"}
    catalog = LocalCommandCatalog(
        specs=extraction_command_specs(mwcp_parser_ids=tuple(allowed)),
        which=lambda _name: str(binary),
    )
    service = LocalCommandService(
        _Samples(sample),  # type: ignore[arg-type]
        _TestConfinedRunner(require_filesystem_confinement=False),
        catalog,
        enabled=True,
    )
    port = MwcpAdapter(service, catalog, parser_versions=allowed, enabled=True)
    registry = InMemoryToolRegistry()
    register_extraction_tools(
        registry,
        sample_associations=_Associations(sample_sha256),  # type: ignore[arg-type]
        recorder=_Recorder(),  # type: ignore[arg-type]
        configuration=port,
    )
    _definition, adapter = registry.get(MWCP_TOOL, "1")  # type: ignore[misc]

    result = await adapter.execute(
        {"sample_sha256": sample_sha256, "parser_id": "empty.parser"},
        _context(sample_sha256),
    )

    assert result.status is ToolInvocationStatus.EMPTY
    assert (
        "An absent extraction does not establish the absence of configuration."
        in result.output["limitations"]
    )


def test_mwcp_field_truncation_is_marked_and_counted() -> None:
    values, indicators, truncated = _normalize_configuration(
        {"configuration": {"payload": "x" * 5_000, "server": "c" * 2_200}}
    )

    assert truncated is True
    assert indicators == ()
    payload_value = next(item for item in values if item.key == "payload")
    server_value = next(item for item in values if item.key == "server")
    assert payload_value.truncated_fields == ("value",)
    assert server_value.truncated_fields == ("network_indicator",)
    serialized = _configuration_value_json(payload_value)
    assert serialized["truncated_fields"] == ["value"]
    assert serialized["truncated"] is True
    assert sum(len(item.truncated_fields) for item in values) == 2


@pytest.mark.asyncio
async def test_mwcp_binary_absence_is_capability_unavailable_not_failure() -> None:
    sample = b"safe"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    parser_versions = {"allowed.parser": "1.0"}
    catalog = LocalCommandCatalog(
        specs=extraction_command_specs(mwcp_parser_ids=tuple(parser_versions)),
        which=lambda _name: None,
    )
    service = LocalCommandService(
        _Samples(sample),  # type: ignore[arg-type]
        _TestConfinedRunner(require_filesystem_confinement=False),
        catalog,
        enabled=True,
    )
    port = MwcpAdapter(service, catalog, parser_versions=parser_versions, enabled=True)

    assert not port.available
    assert MWCP_TOOL not in extraction_tool_capabilities(
        floss=None, configuration=port, report_import_enabled=False
    )
    result = await port.extract(sample_sha256, parser_id="allowed.parser")
    assert result.status.value == "unavailable"


@pytest.mark.asyncio
async def test_mwcp_is_unavailable_without_filesystem_confinement(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    sample = b"safe fixture"
    sample_sha256 = hashlib.sha256(sample).hexdigest()
    marker = tmp_path / "parser-executed"
    binary = _fake_mwcp(tmp_path / "mwcp", '{"configuration":{}}', marker)
    monkeypatch.setattr(local_runner_module, "_detect_network_namespace", lambda *_args: None)
    parser_versions = {"allowed.parser": "1.0"}
    catalog = LocalCommandCatalog(
        specs=extraction_command_specs(mwcp_parser_ids=tuple(parser_versions)),
        which=lambda _name: str(binary),
    )
    service = LocalCommandService(
        _Samples(sample),
        LocalCommandRunner(require_filesystem_confinement=False),
        catalog,
        enabled=True,
    )
    port = MwcpAdapter(service, catalog, parser_versions=parser_versions, enabled=True)

    assert not port.available
    assert port.unavailable_reason == "filesystem_confinement_required"
    assert MWCP_TOOL not in extraction_tool_capabilities(
        floss=None, configuration=port, report_import_enabled=False
    )
    result = await port.extract(sample_sha256, parser_id="allowed.parser")

    assert result.status.value == "unavailable"
    assert result.detail_code == "filesystem_confinement_required"
    assert not marker.exists()
