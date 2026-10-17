from __future__ import annotations

import importlib.util
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

import pytest


def _load_reset_module() -> Any:
    source = Path(__file__).resolve().parents[2] / "scripts/ops/safe_local_reset.py"
    spec = importlib.util.spec_from_file_location("safe_local_reset", source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


reset: Any = _load_reset_module()


def _compose_config(root: Path) -> dict[str, object]:
    return {
        "name": "cti-bulletin",
        "services": {
            "postgres": {
                "environment": {"POSTGRES_DB": "cti_app", "POSTGRES_USER": "cti_app"},
                "volumes": [
                    {
                        "type": "volume",
                        "source": "cti-bulletin_postgres_data",
                        "target": "/var/lib/postgresql/data",
                    }
                ],
            },
            "redis": {
                "volumes": [
                    {
                        "type": "volume",
                        "source": "cti-bulletin_redis_data",
                        "target": "/data",
                    }
                ]
            },
            "minio": {
                "volumes": [
                    {
                        "type": "volume",
                        "source": "cti-bulletin_minio_data",
                        "target": "/data",
                    }
                ]
            },
            "backend": {
                "environment": {
                    "POSTGRES_DSN": (
                        "postgresql+asyncpg://cti_app:placeholder@postgres:5432/cti_app"
                    )
                },
                "volumes": [
                    {
                        "type": "bind",
                        "source": str(root / "var/workspaces/subjects"),
                        "target": "/work/subjects",
                    },
                    {
                        "type": "bind",
                        "source": str(root / "var/workspaces/editions"),
                        "target": "/work/editions",
                    },
                ],
            },
        },
        "volumes": {
            name: {"name": f"cti-bulletin_{name}"}
            for name in ("postgres_data", "redis_data", "minio_data")
        },
    }


def _owned_volumes() -> dict[str, Mapping[str, object]]:
    return {
        f"cti-bulletin_{logical}": {
            "Labels": {
                "com.docker.compose.project": "cti-bulletin",
                "com.docker.compose.volume": logical,
            }
        }
        for logical in ("postgres_data", "redis_data", "minio_data")
    }


def test_reset_refuses_remote_or_unknown_project_targets(tmp_path: Path) -> None:
    config = _compose_config(tmp_path)
    with pytest.raises(reset.ResetRefusedError, match="DSN PostgreSQL"):
        config["services"]["backend"]["environment"]["POSTGRES_DSN"] = (
            "postgresql+asyncpg://cti_app:secret@db.example.test:5432/cti_app"
        )
        reset.validate_reset_configuration(config, root=tmp_path, project_name="cti-bulletin")

    with pytest.raises(reset.ResetRefusedError, match="projet Compose"):
        reset.validate_reset_configuration(
            _compose_config(tmp_path), root=tmp_path, project_name="some-other-project"
        )

    with pytest.raises(reset.ResetRefusedError, match="DSN PostgreSQL"):
        config = _compose_config(tmp_path)
        config["services"]["backend"]["environment"]["POSTGRES_DSN"] = (
            "postgresql+asyncpg://cti_app:placeholder@127.0.0.1:5432/cti_app"
        )
        reset.validate_reset_configuration(config, root=tmp_path, project_name="cti-bulletin")


def test_reset_refuses_remote_docker_contexts() -> None:
    reset.validate_local_docker_endpoint("unix:///var/run/docker.sock")
    with pytest.raises(reset.ResetRefusedError, match="socket Docker Unix local"):
        reset.validate_local_docker_endpoint("ssh://builder.example.test")
    with pytest.raises(reset.ResetRefusedError, match="socket Docker Unix local"):
        reset.validate_local_docker_endpoint("tcp://192.0.2.10:2376")


def test_reset_refuses_volumes_without_compose_ownership(tmp_path: Path) -> None:
    plan = reset.validate_reset_configuration(
        _compose_config(tmp_path), root=tmp_path, project_name="cti-bulletin"
    )
    volumes = _owned_volumes()
    volumes["cti-bulletin_postgres_data"] = {
        "Labels": {
            "com.docker.compose.project": "other-project",
            "com.docker.compose.volume": "postgres_data",
        }
    }

    with pytest.raises(reset.ResetRefusedError, match="n'appartient pas"):
        reset.validate_volume_ownership(plan, volumes)


def test_reset_refuses_workspace_bind_paths_outside_var(tmp_path: Path) -> None:
    config = _compose_config(tmp_path)
    config["services"]["backend"]["volumes"][0]["source"] = str(tmp_path / "data")

    with pytest.raises(reset.ResetRefusedError, match="workspace"):
        reset.validate_reset_configuration(config, root=tmp_path, project_name="cti-bulletin")


def test_real_run_marker_blocks_confirmed_reset_before_any_deletion(tmp_path: Path) -> None:
    config = _compose_config(tmp_path)
    plan = reset.validate_reset_configuration(config, root=tmp_path, project_name="cti-bulletin")
    present = reset.validate_volume_ownership(plan, _owned_volumes())
    workspace = tmp_path / "var/workspaces/subjects"
    workspace.mkdir(parents=True)
    marker = workspace / "preserve.txt"
    marker.write_text("run evidence", encoding="utf-8")
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], _root: Path) -> reset.CommandResult:
        commands.append(command)
        return reset.CommandResult(0)

    with pytest.raises(reset.ResetRefusedError, match="premier run supervisé a démarré"):
        reset.safe_local_reset(
            plan=plan,
            present_volumes=present,
            compose_prefix=("docker", "compose", "-p", "cti-bulletin"),
            root=tmp_path,
            database_status_probe=lambda: reset.DatabaseResetStatus(run_started=True),
            dry_run=False,
            confirm_initial_reset=True,
            runner=runner,
        )

    assert commands == []
    assert marker.read_text(encoding="utf-8") == "run evidence"


def test_dry_run_checks_fake_db_and_leaves_volumes_and_paths_untouched(tmp_path: Path) -> None:
    plan = reset.validate_reset_configuration(
        _compose_config(tmp_path), root=tmp_path, project_name="cti-bulletin"
    )
    present = reset.validate_volume_ownership(plan, _owned_volumes())
    workspace = tmp_path / "var/workspaces/subjects"
    workspace.mkdir(parents=True)
    marker = workspace / "preserve.txt"
    marker.write_text("fixture", encoding="utf-8")
    probes: list[str] = []
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], _root: Path) -> reset.CommandResult:
        commands.append(command)
        return reset.CommandResult(0)

    def probe_empty_database() -> reset.DatabaseResetStatus:
        probes.append("checked")
        return reset.DatabaseResetStatus()

    reported: list[str] = []

    messages = reset.safe_local_reset(
        plan=plan,
        present_volumes=present,
        compose_prefix=("docker", "compose", "-p", "cti-bulletin"),
        root=tmp_path,
        database_status_probe=probe_empty_database,
        dry_run=True,
        reporter=reported.append,
        runner=runner,
    )

    assert probes == ["checked"]
    assert commands == []
    assert any("cti-bulletin_postgres_data" in line for line in reported)
    assert any("production_runs=0" in line for line in reported)
    assert any("analyst_investigations=0" in line for line in reported)
    assert any("subjects=0" in line for line in reported)
    assert any("editions=0" in line for line in reported)
    assert "Dry-run validé : aucune donnée n'a été supprimée." in messages
    assert marker.read_text(encoding="utf-8") == "fixture"


def test_initial_data_reset_requires_confirmation_and_displays_counts(tmp_path: Path) -> None:
    plan = reset.validate_reset_configuration(
        _compose_config(tmp_path), root=tmp_path, project_name="cti-bulletin"
    )
    present = reset.validate_volume_ownership(plan, _owned_volumes())
    status = reset.DatabaseResetStatus(
        production_runs=2,
        analyst_investigations=1,
        subjects=4,
        editions=2,
    )
    reported: list[str] = []

    with pytest.raises(reset.ResetRefusedError, match="make up-clean CONFIRM_INITIAL_RESET=yes"):
        reset.safe_local_reset(
            plan=plan,
            present_volumes=present,
            compose_prefix=("docker", "compose", "-p", "cti-bulletin"),
            root=tmp_path,
            database_status_probe=lambda: status,
            dry_run=True,
            reporter=reported.append,
            runner=lambda _command, _root: reset.CommandResult(0),
        )

    assert any(
        "production_runs=2, analyst_investigations=1, subjects=4, editions=2" in line
        for line in reported
    )
    assert any("volumes à retirer" in line for line in reported)
    assert any("chemins locaux à nettoyer" in line for line in reported)


def test_initial_data_reset_accepts_explicit_confirmation(tmp_path: Path) -> None:
    plan = reset.validate_reset_configuration(
        _compose_config(tmp_path), root=tmp_path, project_name="cti-bulletin"
    )
    present = reset.validate_volume_ownership(plan, _owned_volumes())
    workspace = tmp_path / "var/workspaces/subjects"
    workspace.mkdir(parents=True)
    (workspace / "legacy-test.txt").write_text("old test data", encoding="utf-8")
    status = reset.DatabaseResetStatus(
        production_runs=2,
        analyst_investigations=1,
        subjects=4,
        editions=2,
    )
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], _root: Path) -> reset.CommandResult:
        commands.append(command)
        return reset.CommandResult(0)

    outcome = reset.safe_local_reset(
        plan=plan,
        present_volumes=present,
        compose_prefix=("docker", "compose", "-p", "cti-bulletin"),
        root=tmp_path,
        database_status_probe=lambda: status,
        dry_run=False,
        confirm_initial_reset=True,
        runner=runner,
    )

    assert outcome == ("Données applicatives locales effacées.",)
    assert not workspace.exists()
    assert commands[0][-1] == "down"
    assert [command[-1] for command in commands[1:]] == list(present)


def test_reset_rechecks_run_marker_after_stopping_stack(tmp_path: Path) -> None:
    plan = reset.validate_reset_configuration(
        _compose_config(tmp_path), root=tmp_path, project_name="cti-bulletin"
    )
    present = reset.validate_volume_ownership(plan, _owned_volumes())
    workspace = tmp_path / "var/workspaces/subjects"
    workspace.mkdir(parents=True)
    marker = workspace / "preserve.txt"
    marker.write_text("run evidence", encoding="utf-8")
    statuses = [
        reset.DatabaseResetStatus(),
        reset.DatabaseResetStatus(run_started=True),
    ]
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], _root: Path) -> reset.CommandResult:
        commands.append(command)
        return reset.CommandResult(0)

    with pytest.raises(reset.ResetRefusedError, match="apparu pendant l'arrêt"):
        reset.safe_local_reset(
            plan=plan,
            present_volumes=present,
            compose_prefix=("docker", "compose", "-p", "cti-bulletin"),
            root=tmp_path,
            database_status_probe=lambda: statuses.pop(0),
            dry_run=False,
            confirm_initial_reset=True,
            runner=runner,
        )

    assert len(statuses) == 0
    assert commands == [["docker", "compose", "-p", "cti-bulletin", "down"]]
    assert marker.read_text(encoding="utf-8") == "run evidence"


def test_database_probe_removes_temporary_postgres_container(tmp_path: Path) -> None:
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], _root: Path) -> reset.CommandResult:
        commands.append(command)
        if command[-4:] == ["ps", "--status", "running", "--services"]:
            return reset.CommandResult(0, stdout="")
        if command[-2:] == ["exec", "postgres"]:
            pytest.fail("unexpected shortened exec command")
        if "exec" in command:
            return reset.CommandResult(
                0,
                stderr="NOTICE: AUTOWORK_RESET_STATUS=0|0|0|0|f\n",
            )
        return reset.CommandResult(0)

    probe = reset._database_status_probe(
        root=tmp_path,
        compose_prefix=("docker", "compose", "-p", "cti-bulletin"),
        runner=runner,
        postgres_user="cti_app",
        postgres_database="cti_app",
        dry_run=False,
    )

    assert probe() == reset.DatabaseResetStatus()
    assert commands[1][-3:] == ["up", "-d", "postgres"]
    assert commands[-2][-2:] == ["stop", "postgres"]
    assert commands[-1][-3:] == ["rm", "-f", "postgres"]


def test_root_owned_workspace_is_reclaimed_through_local_docker(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    workspace = tmp_path / "var/workspaces/editions"
    workspace.mkdir(parents=True)
    (workspace / "fixture.txt").write_text("remove", encoding="utf-8")
    original_rmtree = reset.shutil.rmtree
    attempts = 0
    commands: list[Sequence[str]] = []

    def rmtree(path: Path) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise PermissionError(path)
        original_rmtree(path)

    def runner(command: Sequence[str], _root: Path) -> reset.CommandResult:
        commands.append(command)
        return reset.CommandResult(0)

    monkeypatch.setattr(reset.shutil, "rmtree", rmtree)

    reset._remove_local_directory(
        path=workspace,
        root=tmp_path,
        docker_command=("docker",),
        runner=runner,
    )

    assert workspace.exists()
    assert list(workspace.iterdir()) == []
    assert commands == [
        [
            "docker",
            "run",
            "--rm",
            "--network",
            "none",
            "--mount",
            f"type=bind,src={workspace},dst=/target",
            reset.LOCAL_CLEANUP_IMAGE,
            "chown",
            "-R",
            f"{reset.os.getuid()}:{reset.os.getgid()}",
            "/target",
        ]
    ]


def test_clean_reset_only_removes_prevalidated_targets_when_db_is_empty(tmp_path: Path) -> None:
    plan = reset.validate_reset_configuration(
        _compose_config(tmp_path), root=tmp_path, project_name="cti-bulletin"
    )
    present = reset.validate_volume_ownership(plan, _owned_volumes())
    workspace = tmp_path / "var/workspaces/subjects"
    workspace.mkdir(parents=True)
    (workspace / "fixture.txt").write_text("remove", encoding="utf-8")
    commands: list[Sequence[str]] = []

    def runner(command: Sequence[str], _root: Path) -> reset.CommandResult:
        commands.append(command)
        return reset.CommandResult(0)

    reset.safe_local_reset(
        plan=plan,
        present_volumes=present,
        compose_prefix=("docker", "compose", "-p", "cti-bulletin"),
        root=tmp_path,
        database_status_probe=lambda: reset.DatabaseResetStatus(),
        dry_run=False,
        runner=runner,
    )

    assert not workspace.exists()
    assert commands[0][-1] == "down"
    assert [command[-1] for command in commands[1:]] == list(present)
