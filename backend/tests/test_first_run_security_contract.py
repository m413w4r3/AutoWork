from __future__ import annotations

import os
import tomllib
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from shutil import which
from typing import Any, cast
from uuid import uuid4

import httpx
import pytest
from pydantic import SecretStr

import cti_app.infrastructure.local_command_runner as local_command_runner_module
from cti_app.api import main as api_main
from cti_app.application.capabilities import CAPA_ANALYSIS_JOB_KIND
from cti_app.application.confined_analysis import CONFINED_ANALYSIS_JOB_KIND
from cti_app.application.identity import (
    LocalIdentityProvider,
    StaticTokenIdentityProvider,
    is_human_actor_id,
    is_loopback_bind_host,
    validate_local_identity_bind,
)
from cti_app.application.investigation_local_tools import (
    LOCAL_RUN_COMMAND,
    local_tool_capabilities,
)
from cti_app.application.local_analysis import (
    LocalCommandCatalog,
    LocalCommandId,
    LocalCommandService,
    default_local_command_specs,
)
from cti_app.application.virustotal import VirusTotalCapabilities
from cti_app.config import Settings
from cti_app.domain.local_analysis import LocalCommandStatus
from cti_app.infrastructure.investigation_runtime_ports import (
    _allow_unconfined_analysis_processes,
    _local_command_catalog,
    build_investigation_runtime_ports,
)
from cti_app.infrastructure.local_command_runner import (
    LocalCommandRunner,
    NamespaceFailureReason,
    NetworkIsolationStatus,
    _build_argv,
    _bwrap_command,
    _NamespaceDetection,
)
from cti_app.infrastructure.tshark_adapter import network_local_command_specs
from cti_app.workers import analysis_tasks


class _SampleBytes:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload
        self.reads = 0

    async def get_sample_bytes(self, sample_sha256: str, *, max_bytes: int) -> bytes:
        del sample_sha256
        assert len(self.payload) <= max_bytes
        self.reads += 1
        return self.payload


class _RecordingLocalCommandRunner(LocalCommandRunner):
    def __init__(self) -> None:
        with local_command_runner_module._NAMESPACE_LAUNCHERS_LOCK:
            local_command_runner_module._FAILED_NAMESPACE_LAUNCHERS.clear()
            local_command_runner_module._FAILED_NAMESPACE_LAUNCHER_REASONS.clear()
        local_command_runner_module._detect_network_namespace.cache_clear()
        super().__init__(network_sandbox_probe_seconds=2.0, require_filesystem_confinement=True)
        self.last_bwrap_argv: list[str] | None = None

    def _wrap_network_namespace(
        self,
        argv: list[str],
        sample_path: Path,
        rule_path: Path | None,
        *,
        seccomp_fd: int | None = None,
    ) -> list[str]:
        wrapped = super()._wrap_network_namespace(
            argv, sample_path, rule_path, seccomp_fd=seccomp_fd
        )
        self.last_bwrap_argv = wrapped
        return wrapped


def test_strict_mode_hides_execution_tools_without_measured_filesystem_confinement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cti_app.infrastructure.local_command_runner._detect_network_namespace",
        lambda _timeout: _NamespaceDetection(
            NetworkIsolationStatus.SECCOMP_ONLY,
            None,
            (NamespaceFailureReason.NO_NETWORK_NAMESPACE_AVAILABLE,),
        ),
    )
    monkeypatch.setenv("LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT", "1")
    settings = Settings(
        _env_file=None,
        local_analysis_enabled=True,
        rule_workbench_enabled=True,
        mwcp_parser_versions={"safe.parser": "1.0"},
    )
    assert settings.local_analysis_require_filesystem_confinement is True
    catalog = _local_command_catalog(settings, filesystem_confined=False)

    assert catalog.available_commands() == ()
    assert LOCAL_RUN_COMMAND not in local_tool_capabilities(catalog, ghidra_enabled=False)
    assert not _allow_unconfined_analysis_processes(settings)


def test_strict_confined_catalog_never_runs_unconfined_version_probes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LOCAL_ANALYSIS_REQUIRE_FILESYSTEM_CONFINEMENT", "1")
    settings = Settings(_env_file=None, local_analysis_enabled=True)

    catalog = _local_command_catalog(settings, filesystem_confined=True)
    file_capability = catalog.get(LocalCommandId.FILE)

    assert file_capability is not None and file_capability.available
    assert file_capability.spec.requires_filesystem_confinement
    assert file_capability.version == "unverified"


def test_strict_runtime_disables_every_unconfined_local_executor(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "cti_app.infrastructure.local_command_runner._detect_network_namespace",
        lambda _timeout: _NamespaceDetection(
            NetworkIsolationStatus.SECCOMP_ONLY,
            None,
            (NamespaceFailureReason.NO_NETWORK_NAMESPACE_AVAILABLE,),
        ),
    )
    settings = Settings(
        local_analysis_enabled=True,
        local_analysis_require_filesystem_confinement=True,
        rule_workbench_enabled=True,
        mwcp_parser_versions={"safe.parser": "1.0"},
    )
    runtime = build_investigation_runtime_ports(
        settings,
        uow_factory=cast(Any, lambda: None),
        blob_store=cast(Any, object()),
        blob_catalog=cast(Any, object()),
        vt_adapter=None,
        vt_capabilities=VirusTotalCapabilities(),
        vt_observations=None,
        shodan_adapter=None,
        ghidra_adapter=None,
    )
    statuses = {status.family: status for status in runtime.statuses}
    ports = runtime.optional_tool_ports

    assert statuses["local_command_execution"].reason == "no_network_namespace_available"
    assert statuses["rule_workbench"].reason == "analysis_worker_disabled_by_configuration"
    assert statuses["rule_workbench"].execution_mode == "unavailable"
    assert statuses["local_filesystem_confinement"].reason == "no_network_namespace_available"
    assert statuses["capa_analysis"].state.value == "unavailable"
    assert statuses["smda_analysis"].state.value == "unavailable"
    assert statuses["floss_extraction"].reason == "analysis_worker_disabled_by_configuration"
    assert (
        statuses["configuration_extraction"].reason == "analysis_worker_disabled_by_configuration"
    )
    assert statuses["local_analysis"].state.value == "unavailable"
    assert ports.local_commands is not None
    assert ports.local_command_catalog is not None
    assert ports.local_command_catalog.available_commands() == ()
    assert ports.yara_engine is None
    assert ports.sample_capabilities_analysis is None
    assert ports.sample_code_features is None
    assert ports.floss_extraction is None
    assert ports.configuration_extraction is None


def test_tshark_catalog_is_offline_read_only_and_has_no_arbitrary_lua_option() -> None:
    specs = network_local_command_specs()
    by_binary = {spec.binary: spec for spec in specs}

    assert set(by_binary) == {"tshark", "capinfos"}
    assert all(spec.requires_filesystem_confinement for spec in specs)
    assert tuple(option.name for option in by_binary["tshark"].allowed_options) == (
        "field_catalog",
    )
    assert by_binary["capinfos"].allowed_options == ()

    sample_path = Path("/analysis/sample.bin")
    tshark_argv, _logical_argv = _build_argv(
        by_binary["tshark"],
        "/usr/bin/tshark",
        sample_path,
        None,
        {"include_ja3": True},
    )
    assert "-r" in tshark_argv
    assert str(sample_path) in tshark_argv
    assert not {"-i", "-X", "--interface", "dumpcap", "lua_script"}.intersection(tshark_argv)


def test_bwrap_command_uses_pid_namespaces_and_keeps_seccomp_fd() -> None:
    command = _bwrap_command(
        "bwrap",
        ["/usr/bin/true"],
        Path("/tmp/sample.bin"),
        None,
        seccomp_fd=17,
    )

    for option in ("--unshare-net", "--unshare-pid", "--unshare-ipc", "--unshare-uts"):
        assert option in command
    assert "--die-with-parent" in command
    seccomp_index = command.index("--seccomp")
    assert command[seccomp_index + 1] == "17"
    assert seccomp_index > command.index("--die-with-parent")


@pytest.mark.asyncio
async def test_analysis_worker_registers_capa_behind_its_container_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = Settings(local_analysis_require_filesystem_confinement=True)

    class _Engine:
        async def dispose(self) -> None:
            return None

    class _Executor:
        registry: object | None = None

        def __init__(self, _factory: object, registry: object, **_kwargs: object) -> None:
            type(self).registry = registry

        async def execute(self, _job_id: object) -> None:
            return None

    engine = _Engine()
    monkeypatch.setattr(analysis_tasks, "get_settings", lambda: settings)
    monkeypatch.setattr(analysis_tasks, "create_postgres_engine", lambda _dsn: engine)
    monkeypatch.setattr(analysis_tasks, "create_session_factory", lambda _engine: object())
    monkeypatch.setattr(analysis_tasks, "Minio", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(analysis_tasks, "MinioBlobStore", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(analysis_tasks, "BlobCatalogService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(analysis_tasks, "StaticAnalysisService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(analysis_tasks, "CapabilitiesService", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(analysis_tasks, "JobExecutor", _Executor)

    await analysis_tasks._execute_analysis_job(uuid4())

    assert _Executor.registry is not None
    assert cast(Any, _Executor.registry).validate(CAPA_ANALYSIS_JOB_KIND, {"sample_id": uuid4()})
    confined_parameters = {
        "investigation_id": uuid4(),
        "association_id": uuid4(),
        "sample_id": uuid4(),
        "artifact_id": uuid4(),
        "artifact_sha256": "a" * 64,
        "operation_key": "confined-analysis:" + "b" * 64,
        "tool": "sample_analysis",
        "timeout_seconds": 30,
        "max_output_bytes": 1024,
    }
    assert cast(Any, _Executor.registry).validate(CONFINED_ANALYSIS_JOB_KIND, confined_parameters)


@pytest.mark.asyncio
async def test_real_bwrap_runs_only_catalogued_readers_on_an_inert_sample() -> None:
    # bwrap --unshare-net gives this run a measured network namespace and its
    # read-only binds expose /usr, libraries, the synthetic input, and no host
    # root. Bubblewrap installs the same network-denying filter inside the new
    # namespace. Seccomp alone does not confine filesystem reads. CPU,
    # address-space, file-size, descriptor, core, wall-clock, and captured-output
    # limits are enforced independently by the bootstrap and stream readers.
    assert which("bwrap") is not None, "bubblewrap is required for this confinement test"
    runner = _RecordingLocalCommandRunner()
    if not runner.filesystem_confined:
        pytest.skip(
            f"host kernel does not permit measured bwrap isolation: {runner.namespace_reason}"
        )
    assert runner.network_isolation == "bwrap-net"

    payload = b"SAFE_INERT_SYNTHETIC_SAMPLE_MARKER\x00" * 8
    sample_hash = sha256(payload).hexdigest()
    samples = _SampleBytes(payload)
    bounded_specs = tuple(
        replace(
            spec,
            requires_filesystem_confinement=True,
            timeout_seconds=5,
            max_output_bytes=1024,
            cpu_limit_seconds=5,
            memory_limit_bytes=512 * 1024 * 1024,
            file_size_limit_bytes=4096,
        )
        for spec in default_local_command_specs()
        if spec.id in {LocalCommandId.FILE, LocalCommandId.STRINGS}
    )
    catalog = LocalCommandCatalog(specs=bounded_specs)
    service = LocalCommandService(samples, runner, catalog, enabled=True)

    file_result = await service.run(LocalCommandId.FILE, sample_hash)
    strings_result = await service.run(LocalCommandId.STRINGS, sample_hash)

    assert file_result.status is LocalCommandStatus.SUCCEEDED
    assert strings_result.status is LocalCommandStatus.SUCCEEDED
    assert "SAFE_INERT_SYNTHETIC_SAMPLE_MARKER" in strings_result.output
    assert len(file_result.output.encode()) <= 1024
    assert len(strings_result.output.encode()) <= 1024
    assert file_result.duration_seconds <= 5
    assert strings_result.duration_seconds <= 5
    assert samples.reads == 2
    assert runner.last_bwrap_argv is not None
    assert "--unshare-net" in runner.last_bwrap_argv
    assert "--unshare-pid" in runner.last_bwrap_argv
    assert "--unshare-ipc" in runner.last_bwrap_argv
    assert "--unshare-uts" in runner.last_bwrap_argv
    assert "--seccomp" in runner.last_bwrap_argv
    read_only_binds = [
        (runner.last_bwrap_argv[index + 1], runner.last_bwrap_argv[index + 2])
        for index, item in enumerate(runner.last_bwrap_argv[:-2])
        if item == "--ro-bind"
    ]
    assert ("/usr", "/usr") in read_only_binds
    assert ("/", "/") not in read_only_binds
    assert any(target == "/analysis/sample.bin" for _source, target in read_only_binds)
    assert all(
        target in {"/usr", "/lib", "/lib64", "/analysis/sample.bin"}
        for _source, target in read_only_binds
    )


@pytest.mark.asyncio
async def test_real_bwrap_hides_host_proc_and_seccomp_denies_socket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert which("bwrap") is not None, "bubblewrap is required for this confinement test"
    runner = _RecordingLocalCommandRunner()
    if not runner.filesystem_confined:
        pytest.skip(
            f"host kernel does not permit measured bwrap isolation: {runner.namespace_reason}"
        )

    monkeypatch.setenv("AUTOWORK_HOST_PROC_SENTINEL", "must-not-reach-sandbox")
    host_worker_pid = os.getpid()
    payload = (
        b"import os, socket\n"
        + f"host_worker_pid = {host_worker_pid}\n".encode()
        + b"proc_entries = set(os.listdir('/proc'))\n"
        b"print(f'namespace-pid:{os.getpid()}')\n"
        b"pid1_exe = os.path.basename(os.readlink('/proc/1/exe'))\n"
        b"print('pid1-is-bwrap' if pid1_exe == 'bwrap' else 'pid1-not-bwrap')\n"
        b"pid1_environ = open('/proc/1/environ', 'rb').read()\n"
        b"pid1_has_host_env = b'AUTOWORK_HOST_PROC_SENTINEL' in pid1_environ\n"
        b"print('pid1-host-env-visible' if pid1_has_host_env else 'pid1-host-env-hidden')\n"
        b"host_pid_visible = str(host_worker_pid) in proc_entries\n"
        b"print('host-pid-visible' if host_pid_visible else 'host-pid-hidden')\n"
        b"try:\n"
        b"    open(f'/proc/{host_worker_pid}/environ', 'rb').close()\n"
        b"except OSError:\n"
        b"    print('host-environ-denied')\n"
        b"else:\n"
        b"    print('host-environ-readable')\n"
        b"try:\n"
        b"    socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
        b"except OSError as error:\n"
        b"    print(f'socket-denied:{error.errno}')\n"
        b"else:\n"
        b"    print('socket-created')\n"
    )
    sample_hash = sha256(payload).hexdigest()
    sample = _SampleBytes(payload)
    spec = replace(
        default_local_command_specs()[0],
        binary="python3",
        requires_filesystem_confinement=True,
        timeout_seconds=5,
        max_output_bytes=1024,
        cpu_limit_seconds=5,
        memory_limit_bytes=512 * 1024 * 1024,
        file_size_limit_bytes=4096,
    )
    catalog = LocalCommandCatalog(specs=(spec,), which=lambda _name: "/usr/bin/python3")
    result = await LocalCommandService(sample, runner, catalog, enabled=True).run(
        LocalCommandId.FILE, sample_hash
    )

    assert result.status is LocalCommandStatus.SUCCEEDED
    assert "namespace-pid:" in result.output
    assert "pid1-is-bwrap" in result.output
    assert "pid1-host-env-hidden" in result.output
    assert "host-pid-hidden" in result.output
    assert "host-environ-denied" in result.output
    assert "socket-denied:1" in result.output
    assert "socket-created" not in result.output
    assert sample.reads == 1
    assert runner.last_bwrap_argv is not None
    assert "--unshare-net" in runner.last_bwrap_argv
    assert "--unshare-pid" in runner.last_bwrap_argv
    assert "--unshare-ipc" in runner.last_bwrap_argv
    assert "--unshare-uts" in runner.last_bwrap_argv
    assert "--seccomp" in runner.last_bwrap_argv


@pytest.mark.parametrize("environment", ["pilot", "prod", "production", "live"])
def test_local_identity_rejects_non_loopback_configuration(environment: str) -> None:
    for host in ("0.0.0.0", "::"):
        with pytest.raises(ValueError, match="cannot bind outside loopback"):
            validate_local_identity_bind(app_env=environment, bind_host=host)

    validate_local_identity_bind(app_env=environment, bind_host="127.0.0.1")
    validate_local_identity_bind(app_env=environment, bind_host="::1")
    assert is_loopback_bind_host("[::1]")
    assert not is_loopback_bind_host("localhost")
    assert not is_loopback_bind_host("example.test")


@pytest.mark.parametrize("token", ["short", "é" * 40, "token-with internal-space-123456789"])
def test_operator_token_requires_a_long_ascii_value(token: str) -> None:
    with pytest.raises(ValueError, match="at least 32 ASCII non-whitespace characters"):
        Settings(operator_api_token=SecretStr(token))


def test_operator_token_loads_from_environment_as_a_secret(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = "environment-operator-token-value-123456"
    monkeypatch.setenv("OPERATOR_API_TOKEN", token)

    settings = Settings(_env_file=None)

    assert settings.operator_api_token is not None
    assert settings.operator_api_token.get_secret_value() == token
    assert token not in repr(settings)


@pytest.mark.asyncio
async def test_api_status_marks_local_identity_unverified_and_system_actor_cannot_approve(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(app_env="development", api_bind_host="127.0.0.1"),
    )
    application = api_main.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://127.0.0.1",
    ) as client:
        status_response = await client.get("/api/identity/status")
        assert status_response.status_code == 200
        status_payload = status_response.json()
        assert status_payload["mode"] == "local"
        assert status_payload["verified"] is False
        assert status_payload["identity_scope"] == "local-isolated"
        assert status_payload["human_identity"] is True
        assert "local isolé" in status_payload["warning"].casefold()
        assert "actor_id" not in status_payload

        application.state.identity_provider = LocalIdentityProvider("system:investigation")
        approval_response = await client.post(
            "/api/investigations/00000000-0000-0000-0000-000000000001/envelope/approve",
            json={},
        )

    assert approval_response.status_code == 403
    assert approval_response.json()["detail"]["code"] == "human_operator_identity_required"
    assert not is_human_actor_id("system:investigation")


@pytest.mark.asyncio
@pytest.mark.parametrize("environment", ["production", "live"])
async def test_api_refuses_requests_when_runtime_bind_is_exposed_in_production(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(app_env=environment, api_bind_host="127.0.0.1"),
    )
    application = api_main.create_app()
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        status_response = await client.get("/api/identity/status")
        approval_response = await client.post(
            "/api/investigations/00000000-0000-0000-0000-000000000001/envelope/approve",
            json={},
        )

    assert status_response.json()["bind_scope"] == "exposed"
    assert status_response.json()["approvals_allowed"] is False
    assert approval_response.status_code == 503
    assert approval_response.json()["detail"]["code"] == "local_identity_requires_loopback_bind"


@pytest.mark.parametrize("environment", ["pilot", "prod", "production", "live"])
def test_local_identity_cannot_start_on_an_exposed_protected_bind(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
) -> None:
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(app_env=environment, api_bind_host="0.0.0.0"),
    )

    with pytest.raises(ValueError, match="cannot bind outside loopback"):
        api_main.create_app()


@pytest.mark.asyncio
async def test_static_operator_token_authenticates_without_becoming_identity_or_leaking(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    token = "operator-token-for-supervised-pilot-2026"
    monkeypatch.setattr(
        api_main,
        "get_settings",
        lambda: Settings(
            app_env="pilot",
            api_bind_host="0.0.0.0",
            operator_api_token=SecretStr(token),
        ),
    )
    application = api_main.create_app()

    class Readiness:
        async def check(self) -> dict[str, object]:
            return {}

    application.state.readiness = Readiness()

    @application.get("/api/security-probe")
    async def security_probe() -> dict[str, str]:
        identity = await application.state.identity_provider.current()
        return {"actor_id": identity.actor_id}

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        status_response = await client.get("/api/identity/status")
        live_response = await client.get("/api/health/live")
        ready_response = await client.get("/api/health/ready")
        denied_response = await client.get("/api/security-probe")
        business_denied_response = await client.get("/api/subjects")
        other_health_denied_response = await client.get("/api/health/models")
        accepted_response = await client.get(
            "/api/security-probe",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert status_response.json()["mode"] == "authenticated"
    assert status_response.json()["verified"] is True
    assert status_response.json()["approvals_allowed"] is True
    assert token not in status_response.text
    assert live_response.status_code == 200
    assert ready_response.status_code == 200
    assert denied_response.status_code == 401
    assert business_denied_response.status_code == 401
    assert other_health_denied_response.status_code == 401
    assert accepted_response.status_code == 200
    assert accepted_response.json() == {"actor_id": "operator:static-token"}
    assert token not in repr(application.state.identity_provider)
    assert token not in caplog.text
    provider = StaticTokenIdentityProvider(token)
    assert provider.verify_authorization(f"Bearer {token}")
    assert not provider.verify_authorization("Bearer wrong-token")


def test_backend_image_has_only_the_investigation_runtime_analysis_dependencies() -> None:
    project_root = Path(__file__).resolve().parents[2]
    dockerfile = (project_root / "infra" / "backend.Dockerfile").read_text(encoding="utf-8")
    project = tomllib.loads(
        (project_root / "backend" / "pyproject.toml").read_text(encoding="utf-8")
    )
    dependencies = project["project"]["dependencies"]
    groups = project["dependency-groups"]

    assert "DEBIAN_FRONTEND=noninteractive" in dockerfile
    assert "bubblewrap" in dockerfile
    assert "tshark" in dockerfile
    assert (
        "if ! uv sync --frozen --no-dev --no-group analysis --group analysis-runtime; then"
        in dockerfile
    )
    assert "RUN uv sync --frozen --no-dev --no-group analysis \\" in dockerfile
    assert "uv sync --frozen --no-dev --no-group analysis --no-group analysis-runtime" in dockerfile
    assert groups["analysis-runtime"] == ["yara-python==4.5.0"]
    assert "pefile==2024.8.26" in dependencies
    assert "pyelftools==0.33" in dependencies
