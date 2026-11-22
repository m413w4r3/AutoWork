"""Static deployment contract for the M24 host workspace agent (ticket W7).

These oracles never start systemd, Docker, the agent, a browser or a network
service. They parse the checked-in deployment artefacts and run the installer
in its documented ``--dry-run`` mode under a throwaway ``HOME``.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import cast

import yaml

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
COMPOSE_PATH = REPOSITORY_ROOT / "compose.yaml"
UNIT_PATH = REPOSITORY_ROOT / "infra" / "systemd" / "autowork-workspace-agent.service"
INSTALL_SCRIPT = REPOSITORY_ROOT / "scripts" / "install-workspace-agent.sh"
DEV_SCRIPT = REPOSITORY_ROOT / "scripts" / "workspace-agent-dev.sh"
ENV_EXAMPLE_PATH = REPOSITORY_ROOT / ".env.example"

SOCKET_MOUNT = "${AUTOWORK_WORKSPACE_SOCKET_DIR:-./var/run/workspace-agent}:/run/workspace-agent:ro"
SOCKET_MOUNT_TARGET = "/run/workspace-agent"
BACKEND_ENVIRONMENT = {
    "WORKSPACE_AGENT_SOCKET": "/run/workspace-agent/workspace-agent.sock",
    "WORKSPACE_AGENT_TOKEN_FILE": "/run/workspace-agent/auth-token",
}
REQUIRED_UNIT_LINES = frozenset(
    {
        "Type=simple",
        "RuntimeDirectory=autowork-workspace",
        "RuntimeDirectoryMode=0750",
        "UMask=0007",
        "Restart=on-failure",
        "NoNewPrivileges=yes",
        "ProtectSystem=strict",
        "ProtectHome=read-only",
        "WantedBy=default.target",
    }
)
EXECUTABLE_FRAGMENTS = (
    "/usr/bin/python3.12",
    "-m sidecars.workspace_agent",
    "--root %h/work/autowork",
    "--socket-dir %t/autowork-workspace",
    "--group autowork-workspace",
)
ENV_EXAMPLE_DEFAULTS = {
    "AUTOWORK_WORKSPACE_SOCKET_DIR": "./var/run/workspace-agent",
    "AUTOWORK_WORKSPACE_ROOT": "~/work/autowork",
    "AUTOWORK_WORKSPACE_GID": "autowork-workspace",
}


def _mapping(value: object, label: str) -> dict[str, object]:
    assert isinstance(value, dict), f"{label} must be a mapping"
    return cast("dict[str, object]", value)


def _strings(value: object, label: str) -> list[str]:
    assert isinstance(value, list), f"{label} must be a sequence"
    assert all(isinstance(item, str) for item in value), f"{label} must contain strings"
    return cast("list[str]", value)


def _compose_services() -> dict[str, object]:
    document = yaml.safe_load(COMPOSE_PATH.read_text(encoding="utf-8"))
    return _mapping(_mapping(document, "compose")["services"], "compose.services")


def _service_volumes(name: str) -> list[str]:
    service = _mapping(_compose_services()[name], f"services.{name}")
    return _strings(service.get("volumes", []), f"services.{name}.volumes")


def _unit_lines() -> list[str]:
    return [line.strip() for line in UNIT_PATH.read_text(encoding="utf-8").splitlines()]


def _env_example_values() -> dict[str, str]:
    values: dict[str, str] = {}
    for line in ENV_EXAMPLE_PATH.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, _, value = stripped.partition("=")
        values[key] = value
    return values


def test_backend_mounts_the_runtime_socket_directory_read_only() -> None:
    backend_volumes = _service_volumes("backend")
    socket_mounts = [volume for volume in backend_volumes if SOCKET_MOUNT_TARGET in volume]

    assert socket_mounts == [SOCKET_MOUNT]
    assert all(volume.endswith(":ro") for volume in socket_mounts)

    for name in _compose_services():
        if name == "backend":
            continue
        assert not any(SOCKET_MOUNT_TARGET in volume for volume in _service_volumes(name)), name


def test_no_service_mounts_the_workspace_tree() -> None:
    for name in _compose_services():
        volumes = _service_volumes(name)
        assert not any("AUTOWORK_WORKSPACE_ROOT" in volume for volume in volumes), name
        assert not any("work/autowork" in volume for volume in volumes), name

    assert not any(SOCKET_MOUNT_TARGET in volume for volume in _service_volumes("analysis-worker"))


def test_backend_receives_the_workspace_agent_endpoints() -> None:
    service = _mapping(_compose_services()["backend"], "services.backend")
    environment = _mapping(service["environment"], "services.backend.environment")

    for key, expected in BACKEND_ENVIRONMENT.items():
        assert environment.get(key) == expected


def test_systemd_unit_template_is_hardened() -> None:
    lines = _unit_lines()
    missing = REQUIRED_UNIT_LINES - set(lines)
    assert not missing, f"missing unit directives: {sorted(missing)}"

    exec_start = [line for line in lines if line.startswith("ExecStart=")]
    assert len(exec_start) == 1
    for fragment in EXECUTABLE_FRAGMENTS:
        assert fragment in exec_start[0]

    read_write_paths = [line for line in lines if line.startswith("ReadWritePaths=")]
    assert len(read_write_paths) == 1
    assert "%h/work/autowork" in read_write_paths[0]
    assert "%t/autowork-workspace" in read_write_paths[0]

    pythonpath = [line for line in lines if line.startswith("Environment=PYTHONPATH=")]
    assert len(pythonpath) == 1
    assert "@AUTOWORK_REPOSITORY_ROOT@" in pythonpath[0]


def test_env_example_documents_host_agent_defaults() -> None:
    values = _env_example_values()

    for key, expected in ENV_EXAMPLE_DEFAULTS.items():
        assert values.get(key) == expected, key


def test_installer_dry_run_renders_the_unit_without_side_effects(tmp_path: Path) -> None:
    home = tmp_path / "home"
    home.mkdir()
    environment = {
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "USER": "autowork-test",
    }

    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--dry-run"],
        cwd=REPOSITORY_ROOT,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 0, result.stderr
    assert "[Unit]" in result.stdout
    for fragment in EXECUTABLE_FRAGMENTS:
        assert fragment in result.stdout
    assert f"PYTHONPATH={REPOSITORY_ROOT}" in result.stdout
    assert "@AUTOWORK_REPOSITORY_ROOT@" not in result.stdout
    assert "sudo groupadd --system autowork-workspace" in result.stdout

    assert not (home / ".config").exists()
    assert list(home.iterdir()) == []


def test_dev_script_runs_the_module_on_the_dev_socket_directory() -> None:
    text = DEV_SCRIPT.read_text(encoding="utf-8")

    assert "var/run/workspace-agent" in text
    assert "/usr/bin/python3.12" in text
    assert "-m sidecars.workspace_agent" in text


def test_shell_scripts_are_executable_and_syntactically_valid() -> None:
    for script in (INSTALL_SCRIPT, DEV_SCRIPT):
        assert script.is_file(), script
        assert os.access(script, os.X_OK), f"{script} is not executable"

        result = subprocess.run(
            ["bash", "-n", str(script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stderr


def test_shell_scripts_are_shellcheck_clean_when_available() -> None:
    shellcheck = shutil.which("shellcheck")
    if shellcheck is None:
        return

    for script in (INSTALL_SCRIPT, DEV_SCRIPT):
        result = subprocess.run(
            [shellcheck, "-x", str(script)],
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )
        assert result.returncode == 0, result.stdout + result.stderr
