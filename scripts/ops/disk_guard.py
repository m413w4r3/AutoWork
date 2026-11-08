"""Read Docker/MinIO disk headroom and run narrowly scoped Docker GC."""

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

GIGABYTE = 1_000_000_000
DEFAULT_MIN_FREE_GB = 25
DEFAULT_WARN_FREE_GB = 50
DEFAULT_KEEP_STORAGE_GB = 15
DEFAULT_GC_UNTIL = "168h"


class DiskGuardError(RuntimeError):
    """A disk check or safe-GC operation could not be completed."""


@dataclass(frozen=True, slots=True)
class DiskThresholds:
    minimum_bytes: int
    warning_bytes: int


@dataclass(frozen=True, slots=True)
class DiskUsage:
    name: str
    path: str
    free_bytes: int


def parse_thresholds(minimum_gb: str | int, warning_gb: str | int) -> DiskThresholds:
    """Parse positive decimal GB thresholds and require warning >= minimum."""
    minimum = _positive_integer(minimum_gb, "DISK_MIN_FREE_GB")
    warning = _positive_integer(warning_gb, "DISK_WARN_FREE_GB")
    if warning < minimum:
        raise ValueError(
            "DISK_WARN_FREE_GB must be greater than or equal to DISK_MIN_FREE_GB"
        )
    return DiskThresholds(minimum * GIGABYTE, warning * GIGABYTE)


def capacity_status(free_bytes: int, thresholds: DiskThresholds) -> str:
    if free_bytes < thresholds.minimum_bytes:
        return "failing"
    if free_bytes < thresholds.warning_bytes:
        return "degraded"
    return "ok"


def parse_volume_names(output: str) -> tuple[str, ...]:
    """Parse Docker's one-name-per-line volume list without accepting blanks."""
    return tuple(
        dict.fromkeys(line.strip() for line in output.splitlines() if line.strip())
    )


def parse_single_path(output: str, label: str) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    if len(lines) != 1:
        raise ValueError(f"Docker returned no unique {label} path")
    return lines[0]


def validate_gc_settings(keep_storage_gb: str | int, until: str) -> tuple[int, str]:
    keep_storage = _positive_integer(keep_storage_gb, "DOCKER_GC_KEEP_STORAGE_GB")
    if not until or not until[:-1].isdigit() or until[-1:] not in {"s", "m", "h"}:
        raise ValueError(
            "DOCKER_GC_UNTIL must be a positive duration such as 24h or 30m"
        )
    if int(until[:-1]) < 1:
        raise ValueError("DOCKER_GC_UNTIL must be a positive duration")
    return keep_storage, until


def docker_gc_commands(
    docker: Sequence[str],
    keep_storage_gb: str | int = DEFAULT_KEEP_STORAGE_GB,
    until: str = DEFAULT_GC_UNTIL,
    *,
    include_dangling_images: bool = True,
) -> tuple[tuple[str, ...], ...]:
    """Build the only permitted GC commands: bounded cache and dangling images."""
    keep_storage, age = validate_gc_settings(keep_storage_gb, until)
    commands: list[tuple[str, ...]] = [
        (
            *docker,
            "builder",
            "prune",
            "--force",
            "--keep-storage",
            f"{keep_storage}GB",
            "--filter",
            f"until={age}",
        )
    ]
    if include_dangling_images:
        commands.append((*docker, "image", "prune", "--force"))
    return tuple(commands)


def run_gc(
    docker_prefix: Sequence[str],
    *,
    keep_storage_gb: str | int,
    until: str,
    dry_run: bool,
    include_dangling_images: bool,
    output: TextIO = sys.stdout,
) -> None:
    commands = docker_gc_commands(
        docker_prefix,
        keep_storage_gb,
        until,
        include_dangling_images=include_dangling_images,
    )
    if dry_run:
        for command in commands:
            print(f"DRY_RUN=1: would run: {shlex.join(command)}", file=output)
        return

    docker = _resolve_docker(docker_prefix)
    for command in docker_gc_commands(
        docker,
        keep_storage_gb,
        until,
        include_dangling_images=include_dangling_images,
    ):
        print(f"Running: {shlex.join(command)}", file=output)
        _run(command)


def collect_disk_usage(
    docker_prefix: Sequence[str],
    *,
    compose_project: str,
    minio_volume: str | None = None,
) -> tuple[tuple[DiskUsage, ...], bool]:
    docker = _resolve_docker(docker_prefix)
    docker_root = parse_single_path(
        _capture((*docker, "info", "--format", "{{.DockerRootDir}}")), "Docker root"
    )
    usages = [DiskUsage("Docker root", docker_root, _free_bytes(docker_root))]

    if minio_volume:
        volumes = (minio_volume,)
    else:
        volumes = parse_volume_names(
            _capture(
                (
                    *docker,
                    "volume",
                    "ls",
                    "--filter",
                    f"label=com.docker.compose.project={compose_project}",
                    "--filter",
                    "label=com.docker.compose.volume=minio_data",
                    "--format",
                    "{{.Name}}",
                )
            )
        )
    if not volumes:
        return tuple(usages), True
    if len(volumes) > 1:
        raise DiskGuardError(
            f"multiple MinIO data volumes match Compose project {compose_project!r}; "
            "set MINIO_VOLUME_NAME to the intended volume"
        )

    volume = volumes[0]
    mountpoint = parse_single_path(
        _capture((*docker, "volume", "inspect", "--format", "{{.Mountpoint}}", volume)),
        "MinIO volume mount",
    )
    usages.append(DiskUsage("MinIO data volume", mountpoint, _free_bytes(mountpoint)))
    return tuple(usages), False


def format_report(usages: Sequence[DiskUsage], thresholds: DiskThresholds) -> str:
    name_width = max(len("Path"), *(len(usage.name) for usage in usages))
    path_width = max(len("Location"), *(len(usage.path) for usage in usages))
    lines = [
        f"{'Path':<{name_width}}  {'Free GB':>10}  {'Status':<9}  {'Location':<{path_width}}",
        f"{'-' * name_width}  {'-' * 10}  {'-' * 9}  {'-' * path_width}",
    ]
    for usage in usages:
        free_gb = usage.free_bytes / GIGABYTE
        lines.append(
            f"{usage.name:<{name_width}}  {free_gb:>10.1f}  "
            f"{capacity_status(usage.free_bytes, thresholds):<9}  {usage.path}"
        )
    return "\n".join(lines)


def check_disk(
    docker_prefix: Sequence[str],
    *,
    compose_project: str,
    thresholds: DiskThresholds,
    minio_volume: str | None = None,
    output: TextIO = sys.stdout,
) -> tuple[DiskUsage, ...]:
    usages, volume_missing = collect_disk_usage(
        docker_prefix, compose_project=compose_project, minio_volume=minio_volume
    )
    print(format_report(usages, thresholds), file=output)
    if volume_missing:
        print(
            "MinIO data volume: not created yet; its default location is under Docker root.",
            file=output,
        )
    if any(
        capacity_status(usage.free_bytes, thresholds) == "degraded" for usage in usages
    ):
        print("WARN: disk free space is below DISK_WARN_FREE_GB.", file=output)
    failing = [
        usage
        for usage in usages
        if capacity_status(usage.free_bytes, thresholds) == "failing"
    ]
    if failing:
        names = ", ".join(usage.name for usage in failing)
        print(
            f"ERREUR: {names} is below DISK_MIN_FREE_GB; refusing work that may build or store data. "
            "Run `make docker-gc`, recover space, then re-run `make disk-check`.",
            file=sys.stderr,
        )
        raise DiskGuardError("free space is below the hard threshold")
    return usages


def preflight(
    docker_prefix: Sequence[str],
    *,
    compose_project: str,
    thresholds: DiskThresholds,
    keep_storage_gb: str | int,
    until: str,
    auto_gc: bool,
    minio_volume: str | None = None,
    output: TextIO = sys.stdout,
) -> None:
    usages = check_disk(
        docker_prefix,
        compose_project=compose_project,
        thresholds=thresholds,
        minio_volume=minio_volume,
        output=output,
    )
    degraded = any(
        capacity_status(usage.free_bytes, thresholds) == "degraded" for usage in usages
    )
    if not degraded:
        return
    if not auto_gc:
        print(
            "AUTO_GC=0: continuing without automatic BuildKit cache cleanup.",
            file=output,
        )
        return

    print(
        "Low disk warning: pruning only aged BuildKit cache, then checking free space again.",
        file=output,
    )
    run_gc(
        docker_prefix,
        keep_storage_gb=keep_storage_gb,
        until=until,
        dry_run=False,
        include_dangling_images=False,
        output=output,
    )
    check_disk(
        docker_prefix,
        compose_project=compose_project,
        thresholds=thresholds,
        minio_volume=minio_volume,
        output=output,
    )


def _positive_integer(value: str | int, label: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a positive integer number of GB") from exc
    if parsed < 1:
        raise ValueError(f"{label} must be a positive integer number of GB")
    return parsed


def _resolve_docker(docker_prefix: Sequence[str]) -> tuple[str, ...]:
    if not docker_prefix:
        raise DiskGuardError(
            "Docker is required; install the Docker CLI or set DOCKER to its path"
        )
    executable = shutil.which(docker_prefix[0])
    if executable is None:
        raise DiskGuardError(
            f"Docker command not found: {docker_prefix[0]!r}; install the Docker CLI "
            "or set DOCKER to its path"
        )
    return (executable, *docker_prefix[1:])


def _capture(command: Sequence[str]) -> str:
    result = subprocess.run(command, capture_output=True, check=False, text=True)
    if result.returncode:
        detail = result.stderr.strip()[:400]
        suffix = f": {detail}" if detail else ""
        raise DiskGuardError(f"command failed with exit {result.returncode}{suffix}")
    return result.stdout


def _run(command: Sequence[str]) -> None:
    result = subprocess.run(command, check=False)
    if result.returncode:
        raise DiskGuardError(
            f"Docker garbage collection failed with exit {result.returncode}"
        )


def _free_bytes(path: str) -> int:
    """Free bytes of ``path``'s filesystem.

    Docker data roots are often root-only (``PermissionError``): fall back to the
    nearest readable parent, which lives on the same filesystem in practice.
    """
    candidate = Path(path)
    last_error: OSError | None = None
    while True:
        try:
            return shutil.disk_usage(candidate).free
        except OSError as exc:
            last_error = exc
            if candidate.parent == candidate:
                break
            candidate = candidate.parent
    raise DiskGuardError(
        f"cannot read free space for {path!r}: {type(last_error).__name__}"
    ) from last_error


def _thresholds_from_environment() -> DiskThresholds:
    return parse_thresholds(
        os.environ.get("DISK_MIN_FREE_GB", str(DEFAULT_MIN_FREE_GB)),
        os.environ.get("DISK_WARN_FREE_GB", str(DEFAULT_WARN_FREE_GB)),
    )


def _gc_from_environment() -> tuple[int, str]:
    return validate_gc_settings(
        os.environ.get("DOCKER_GC_KEEP_STORAGE_GB", str(DEFAULT_KEEP_STORAGE_GB)),
        os.environ.get("DOCKER_GC_UNTIL", DEFAULT_GC_UNTIL),
    )


def _docker_prefix(value: str) -> tuple[str, ...]:
    try:
        prefix = tuple(shlex.split(value))
    except ValueError as exc:
        raise ValueError("DOCKER must be an executable command") from exc
    if not prefix:
        raise ValueError("DOCKER must be an executable command")
    return prefix


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="action", required=True)
    for action in ("check", "preflight", "gc"):
        command = subparsers.add_parser(action)
        command.add_argument("--docker", default=os.environ.get("DOCKER", "docker"))
    for action in ("check", "preflight"):
        command = subparsers.choices[action]
        command.add_argument(
            "--compose-project",
            default=os.environ.get("COMPOSE_PROJECT_NAME", "cti-bulletin"),
        )
        command.add_argument(
            "--minio-volume", default=os.environ.get("MINIO_VOLUME_NAME")
        )
    preflight_parser = subparsers.choices["preflight"]
    preflight_parser.add_argument(
        "--auto-gc", choices=("0", "1"), default=os.environ.get("AUTO_GC", "1")
    )
    gc_parser = subparsers.choices["gc"]
    gc_parser.add_argument(
        "--dry-run", choices=("0", "1"), default=os.environ.get("DRY_RUN", "0")
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        docker = _docker_prefix(args.docker)
        if args.action == "gc":
            keep_storage, until = _gc_from_environment()
            run_gc(
                docker,
                keep_storage_gb=keep_storage,
                until=until,
                dry_run=args.dry_run == "1",
                include_dangling_images=True,
            )
        elif args.action == "check":
            thresholds = _thresholds_from_environment()
            check_disk(
                docker,
                compose_project=args.compose_project,
                thresholds=thresholds,
                minio_volume=args.minio_volume,
            )
        else:
            thresholds = _thresholds_from_environment()
            keep_storage, until = _gc_from_environment()
            preflight(
                docker,
                compose_project=args.compose_project,
                thresholds=thresholds,
                keep_storage_gb=keep_storage,
                until=until,
                auto_gc=args.auto_gc == "1",
                minio_volume=args.minio_volume,
            )
    except (DiskGuardError, ValueError) as exc:
        print(f"ERREUR: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
