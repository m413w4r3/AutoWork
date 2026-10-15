#!/usr/bin/env python3
"""Validate and reset only the local AutoWork Compose data targets."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

EXPECTED_PROJECT = "cti-bulletin"
VOLUME_KEYS = ("postgres_data", "redis_data", "minio_data")
CLEAN_DIRECTORIES = (
    Path("var/diagnostics/runs"),
    Path("var/workspaces/editions"),
    Path("var/workspaces/subjects"),
)
CLEAN_FILES = (Path("var/diagnostics/events.jsonl"),)
WORKSPACE_MOUNTS = {
    "/work/subjects": Path("var/workspaces/subjects"),
    "/work/editions": Path("var/workspaces/editions"),
}
_MARKER_SQL = """DO $reset_guard$
DECLARE started boolean := false;
BEGIN
  IF to_regclass('public.production_runs') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.production_runs)' INTO started;
  END IF;
  IF NOT started AND to_regclass('public.analyst_investigations') IS NOT NULL THEN
    EXECUTE 'SELECT EXISTS (SELECT 1 FROM public.analyst_investigations)' INTO started;
  END IF;
  RAISE NOTICE 'AUTOWORK_RUN_MARKER=%', started;
END
$reset_guard$;"""


class ResetRefusedError(ValueError):
    """A reset target could not be proved to belong to the local app stack."""


@dataclass(frozen=True, slots=True)
class ResetPlan:
    project_name: str
    postgres_user: str
    postgres_database: str
    volume_names: tuple[str, ...]
    clean_directories: tuple[Path, ...]
    clean_files: tuple[Path, ...]


@dataclass(frozen=True, slots=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


CommandRunner = Callable[[Sequence[str], Path], CommandResult]


def validate_reset_configuration(
    compose_config: Mapping[str, object], *, root: Path, project_name: str
) -> ResetPlan:
    """Validate project identity, local PostgreSQL DSN, volumes, and bind paths."""
    resolved_root = root.resolve()
    if project_name != EXPECTED_PROJECT or compose_config.get("name") != EXPECTED_PROJECT:
        raise ResetRefusedError(
            f"Refus du reset : le projet Compose doit être exactement {EXPECTED_PROJECT}."
        )

    services = compose_config.get("services")
    volumes = compose_config.get("volumes")
    if not isinstance(services, Mapping) or not isinstance(volumes, Mapping):
        raise ResetRefusedError("Refus du reset : configuration Compose incomplète.")
    postgres = services.get("postgres")
    backend = services.get("backend")
    if not isinstance(postgres, Mapping) or not isinstance(backend, Mapping):
        raise ResetRefusedError("Refus du reset : services PostgreSQL/backend inconnus.")

    postgres_environment = postgres.get("environment")
    backend_environment = backend.get("environment")
    if not isinstance(postgres_environment, Mapping) or not isinstance(
        backend_environment, Mapping
    ):
        raise ResetRefusedError("Refus du reset : environnement PostgreSQL incomplet.")
    dsn = backend_environment.get("POSTGRES_DSN")
    database = postgres_environment.get("POSTGRES_DB")
    username = postgres_environment.get("POSTGRES_USER")
    if (
        not isinstance(dsn, str)
        or not database
        or not isinstance(database, str)
        or not username
        or not isinstance(username, str)
    ):
        raise ResetRefusedError("Refus du reset : DSN PostgreSQL Compose inconnu.")
    try:
        parsed_dsn = urlsplit(dsn)
        dsn_host = parsed_dsn.hostname
        dsn_port = parsed_dsn.port
    except ValueError:
        raise ResetRefusedError("Refus du reset : DSN PostgreSQL Compose invalide.") from None
    if (
        parsed_dsn.scheme not in {"postgres", "postgresql", "postgresql+asyncpg"}
        or dsn_host != "postgres"
        or dsn_port != 5432
        or not parsed_dsn.username
        or parsed_dsn.username != username
        or parsed_dsn.path.lstrip("/") != database
        or parsed_dsn.query
        or parsed_dsn.fragment
    ):
        raise ResetRefusedError(
            "Refus du reset : le DSN PostgreSQL ne cible pas la base locale du projet Compose."
        )
    actual_volume_names: list[str] = []
    for volume_key in VOLUME_KEYS:
        declaration = volumes.get(volume_key)
        if not isinstance(declaration, Mapping):
            raise ResetRefusedError(f"Refus du reset : volume Compose {volume_key} absent.")
        expected_name = f"{EXPECTED_PROJECT}_{volume_key}"
        if declaration.get("external") is True or declaration.get("name", expected_name) != expected_name:
            raise ResetRefusedError(
                f"Refus du reset : le volume {volume_key} ne porte pas le nom local attendu."
            )
        actual_volume_names.append(expected_name)

    expected_attachments = {
        "postgres": ("postgres_data", "/var/lib/postgresql/data"),
        "redis": ("redis_data", "/data"),
        "minio": ("minio_data", "/data"),
    }
    for service_name, (volume_key, target) in expected_attachments.items():
        service = services.get(service_name)
        if not isinstance(service, Mapping) or not _has_volume_mount(
            service, volume_key, f"{EXPECTED_PROJECT}_{volume_key}", target
        ):
            raise ResetRefusedError(
                f"Refus du reset : {service_name} n'utilise pas le volume {volume_key} attendu."
            )

    for target, relative_path in WORKSPACE_MOUNTS.items():
        if not _has_safe_workspace_bind(backend, target, relative_path, resolved_root):
            raise ResetRefusedError(
                f"Refus du reset : le workspace monté sur {target} doit rester sous var/."
            )

    directories = tuple(_checked_local_path(resolved_root, item) for item in CLEAN_DIRECTORIES)
    files = tuple(_checked_local_path(resolved_root, item) for item in CLEAN_FILES)
    return ResetPlan(
        project_name=EXPECTED_PROJECT,
        postgres_user=username,
        postgres_database=database,
        volume_names=tuple(actual_volume_names),
        clean_directories=directories,
        clean_files=files,
    )


def validate_volume_ownership(
    plan: ResetPlan, volume_metadata: Mapping[str, Mapping[str, object] | None]
) -> tuple[str, ...]:
    """Return only present volumes whose Compose ownership labels match."""
    present: list[str] = []
    for volume_name in plan.volume_names:
        metadata = volume_metadata.get(volume_name)
        if metadata is None:
            continue
        labels = metadata.get("Labels")
        if not isinstance(labels, Mapping):
            raise ResetRefusedError(
                f"Refus du reset : les labels du volume {volume_name} sont absents."
            )
        logical_name = volume_name.removeprefix(f"{EXPECTED_PROJECT}_")
        if (
            labels.get("com.docker.compose.project") != EXPECTED_PROJECT
            or labels.get("com.docker.compose.volume") != logical_name
        ):
            raise ResetRefusedError(
                f"Refus du reset : le volume {volume_name} n'appartient pas au projet attendu."
            )
        present.append(volume_name)
    return tuple(present)


def safe_local_reset(
    *,
    plan: ResetPlan,
    present_volumes: Sequence[str],
    compose_prefix: Sequence[str],
    root: Path,
    database_marker_probe: Callable[[], bool] | None,
    dry_run: bool,
    runner: CommandRunner | None = None,
) -> tuple[str, ...]:
    """Probe the durable run marker, then remove only validated local targets."""
    if runner is None:
        runner = _run_command
    if not set(present_volumes).issubset(plan.volume_names):
        raise ResetRefusedError("Refus du reset : la liste des volumes à effacer est inconnue.")
    if "postgres_data" in {
        name.removeprefix(f"{EXPECTED_PROJECT}_") for name in present_volumes
    }:
        if database_marker_probe is None:
            raise ResetRefusedError(
                "Refus du reset : impossible de vérifier les marqueurs de run PostgreSQL."
            )
        try:
            run_started = database_marker_probe()
        except Exception:
            raise ResetRefusedError(
                "Refus du reset : impossible de vérifier les marqueurs de run PostgreSQL."
            ) from None
        if run_started:
            raise ResetRefusedError(
                "Refus du reset : des ProductionRun/AnalystInvestigation existent déjà."
            )

    relative_paths = tuple(CLEAN_DIRECTORIES) + tuple(CLEAN_FILES)
    if dry_run:
        return (
            f"projet Compose : {plan.project_name}",
            "volumes : " + (", ".join(present_volumes) if present_volumes else "aucun"),
            "chemins locaux : " + ", ".join(path.as_posix() for path in relative_paths),
        )

    _run_checked(runner, [*compose_prefix, "down"], root, "arrêt de la stack locale")
    for volume_name in present_volumes:
        _run_checked(
            runner,
            [*compose_prefix[:1], "volume", "rm", volume_name],
            root,
            f"suppression du volume {volume_name}",
        )
    for path in plan.clean_directories:
        if path.exists():
            shutil.rmtree(path)
    for path in plan.clean_files:
        if path.exists():
            path.write_text("", encoding="utf-8")
    return ("Données applicatives locales effacées.",)


def _has_volume_mount(
    service: Mapping[str, object], logical: str, actual: str, target: str
) -> bool:
    mounts = service.get("volumes")
    if not isinstance(mounts, list):
        return False
    for mount in mounts:
        if not isinstance(mount, Mapping):
            continue
        if (
            mount.get("type") == "volume"
            and mount.get("target") == target
            and mount.get("source") in {logical, actual}
        ):
            return True
    return False


def _has_safe_workspace_bind(
    backend: Mapping[str, object], target: str, relative_path: Path, root: Path
) -> bool:
    mounts = backend.get("volumes")
    if not isinstance(mounts, list):
        return False
    for mount in mounts:
        if not isinstance(mount, Mapping):
            continue
        if mount.get("type") != "bind" or mount.get("target") != target:
            continue
        source = mount.get("source")
        if not isinstance(source, str):
            return False
        expected = _checked_local_path(root, relative_path)
        source_path = Path(source)
        actual = (source_path if source_path.is_absolute() else root / source_path).resolve()
        return actual == expected and actual.is_relative_to(root / "var")
    return False


def _checked_local_path(root: Path, relative_path: Path) -> Path:
    if relative_path.is_absolute() or ".." in relative_path.parts:
        raise ResetRefusedError("Refus du reset : un chemin de nettoyage est invalide.")
    resolved_root = root.resolve()
    raw_var_root = resolved_root / "var"
    if raw_var_root.is_symlink():
        raise ResetRefusedError("Refus du reset : var/ ne peut pas être un lien symbolique.")
    var_root = raw_var_root.resolve()
    if not var_root.is_relative_to(resolved_root):
        raise ResetRefusedError("Refus du reset : var/ sort de la racine du projet.")
    path = (resolved_root / relative_path).resolve()
    if not path.is_relative_to(var_root) or path == var_root:
        raise ResetRefusedError("Refus du reset : un chemin de nettoyage sort de var/.")
    candidate = resolved_root / relative_path
    if candidate.is_symlink():
        raise ResetRefusedError("Refus du reset : un chemin de nettoyage est un lien symbolique.")
    return path


def _run_command(command: Sequence[str], root: Path) -> CommandResult:
    try:
        result = subprocess.run(
            list(command),
            cwd=root,
            check=False,
            text=True,
            capture_output=True,
        )
    except OSError:
        return CommandResult(returncode=127)
    return CommandResult(result.returncode, result.stdout, result.stderr)


def _run_checked(
    runner: CommandRunner,
    command: Sequence[str],
    root: Path,
    description: str,
) -> CommandResult:
    result = runner(command, root)
    if result.returncode != 0:
        raise ResetRefusedError(f"Refus du reset : échec pendant {description}.")
    return result


def _load_compose_config(
    *, root: Path, compose_prefix: Sequence[str], runner: CommandRunner
) -> Mapping[str, object]:
    result = _run_checked(
        runner,
        [*compose_prefix, "config", "--format", "json"],
        root,
        "lecture de la configuration Compose",
    )
    try:
        config = json.loads(result.stdout)
    except json.JSONDecodeError:
        raise ResetRefusedError(
            "Refus du reset : la configuration Compose n'est pas un JSON valide."
        ) from None
    if not isinstance(config, Mapping):
        raise ResetRefusedError("Refus du reset : la configuration Compose est invalide.")
    return config


def _inspect_project_volumes(
    *,
    root: Path,
    docker_command: Sequence[str],
    plan: ResetPlan,
    runner: CommandRunner,
) -> tuple[dict[str, Mapping[str, object] | None], tuple[str, ...]]:
    listed = _run_checked(
        runner,
        [*docker_command, "volume", "ls", "--format", "{{.Name}}"],
        root,
        "inspection des volumes Docker",
    )
    existing_names = set(listed.stdout.splitlines())
    metadata: dict[str, Mapping[str, object] | None] = {}
    for volume_name in plan.volume_names:
        if volume_name not in existing_names:
            metadata[volume_name] = None
            continue
        inspected = _run_checked(
            runner,
            [*docker_command, "volume", "inspect", volume_name],
            root,
            f"vérification du volume {volume_name}",
        )
        try:
            payload = json.loads(inspected.stdout)
        except json.JSONDecodeError:
            raise ResetRefusedError(
                f"Refus du reset : les métadonnées du volume {volume_name} sont invalides."
            ) from None
        if (
            not isinstance(payload, list)
            or len(payload) != 1
            or not isinstance(payload[0], Mapping)
            or payload[0].get("Name") != volume_name
        ):
            raise ResetRefusedError(
                f"Refus du reset : l'identité du volume {volume_name} est inconnue."
            )
        metadata[volume_name] = payload[0]
    present = validate_volume_ownership(plan, metadata)
    return metadata, present


def _database_marker_probe(
    *,
    root: Path,
    compose_prefix: Sequence[str],
    runner: CommandRunner,
    postgres_user: str,
    postgres_database: str,
    dry_run: bool,
) -> Callable[[], bool]:
    def probe() -> bool:
        running = _run_checked(
            runner,
            [*compose_prefix, "ps", "--status", "running", "--services"],
            root,
            "vérification du service PostgreSQL",
        )
        started_here = False
        if "postgres" not in running.stdout.splitlines():
            if dry_run:
                raise ResetRefusedError(
                    "Refus du dry-run : PostgreSQL doit déjà tourner pour vérifier les runs."
                )
            _run_checked(
                runner,
                [*compose_prefix, "up", "-d", "postgres"],
                root,
                "démarrage temporaire du PostgreSQL local",
            )
            started_here = True
        try:
            dsn_result = _run_checked(
                runner,
                [
                    *compose_prefix,
                    "exec",
                    "-T",
                    "postgres",
                    "psql",
                    "-qAt",
                    "-v",
                    "ON_ERROR_STOP=1",
                    "-U",
                    postgres_user,
                    "-d",
                    postgres_database,
                    "-c",
                    _MARKER_SQL,
                ],
                root,
                "vérification des marqueurs de run PostgreSQL",
            )
        except ResetRefusedError:
            if started_here:
                runner([*compose_prefix, "stop", "postgres"], root)
            raise
        combined = dsn_result.stdout + "\n" + dsn_result.stderr
        if "AUTOWORK_RUN_MARKER=t" in combined:
            if started_here:
                _run_checked(
                    runner,
                    [*compose_prefix, "stop", "postgres"],
                    root,
                    "arrêt du PostgreSQL temporaire",
                )
            return True
        if "AUTOWORK_RUN_MARKER=f" not in combined:
            if started_here:
                runner([*compose_prefix, "stop", "postgres"], root)
            raise ResetRefusedError(
                "Refus du reset : PostgreSQL n'a pas confirmé l'absence de runs démarrés."
            )
        return False

    return probe


def validate_local_docker_endpoint(endpoint: str) -> None:
    """Reject Docker endpoints that can target a remote daemon."""
    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        raise ResetRefusedError("Refus du reset : cible Docker locale inconnue.") from None
    if parsed.scheme != "unix" or not parsed.path.startswith("/") or parsed.netloc:
        raise ResetRefusedError(
            "Refus du reset : le reset exige un socket Docker Unix local."
        )


def _verify_local_docker_context(
    *, root: Path, docker_command: Sequence[str], runner: CommandRunner
) -> None:
    docker_host = os.environ.get("DOCKER_HOST")
    docker_context = os.environ.get("DOCKER_CONTEXT")
    if docker_host:
        if docker_context:
            raise ResetRefusedError(
                "Refus du reset : DOCKER_HOST et DOCKER_CONTEXT sont définis simultanément."
            )
        validate_local_docker_endpoint(docker_host)
        return
    shown = _run_checked(
        runner,
        [*docker_command, "context", "show"],
        root,
        "lecture du contexte Docker local",
    )
    context_name = docker_context or shown.stdout.strip()
    if not context_name:
        raise ResetRefusedError("Refus du reset : contexte Docker local inconnu.")
    inspected = _run_checked(
        runner,
        [*docker_command, "context", "inspect", context_name],
        root,
        "inspection du contexte Docker local",
    )
    try:
        payload = json.loads(inspected.stdout)
        endpoint = payload[0]["Endpoints"]["docker"]["Host"]
    except (IndexError, KeyError, TypeError, json.JSONDecodeError):
        raise ResetRefusedError("Refus du reset : cible Docker locale inconnue.") from None
    if not isinstance(endpoint, str):
        raise ResetRefusedError("Refus du reset : cible Docker locale inconnue.")
    validate_local_docker_endpoint(endpoint)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project-name", default=EXPECTED_PROJECT)
    parser.add_argument("--compose-command", default="docker compose")
    parser.add_argument("--dry-run", action="store_true")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    root = Path(__file__).resolve().parents[2]
    compose_parts = shlex.split(args.compose_command)
    if len(compose_parts) != 2 or compose_parts[1] != "compose":
        print("Refus du reset : commande Compose inconnue.", file=sys.stderr)
        return 2
    compose_prefix = [*compose_parts, "-p", args.project_name, "-f", "compose.yaml"]
    docker_command = compose_parts[:1]
    try:
        _verify_local_docker_context(
            root=root,
            docker_command=docker_command,
            runner=_run_command,
        )
        config = _load_compose_config(
            root=root,
            compose_prefix=compose_prefix,
            runner=_run_command,
        )
        plan = validate_reset_configuration(
            config, root=root, project_name=args.project_name
        )
        _metadata, present_volumes = _inspect_project_volumes(
            root=root,
            docker_command=docker_command,
            plan=plan,
            runner=_run_command,
        )
        probe = (
            _database_marker_probe(
                root=root,
                compose_prefix=compose_prefix,
                runner=_run_command,
                postgres_user=plan.postgres_user,
                postgres_database=plan.postgres_database,
                dry_run=args.dry_run,
            )
            if f"{EXPECTED_PROJECT}_postgres_data" in present_volumes
            else None
        )
        messages = safe_local_reset(
            plan=plan,
            present_volumes=present_volumes,
            compose_prefix=compose_prefix,
            root=root,
            database_marker_probe=probe,
            dry_run=args.dry_run,
            runner=_run_command,
        )
    except ResetRefusedError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    for message in messages:
        print(message)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
