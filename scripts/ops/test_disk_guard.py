from __future__ import annotations

import io
import shutil

import disk_guard
import pytest


def test_thresholds_use_decimal_gb_and_distinguish_warn_from_hard() -> None:
    thresholds = disk_guard.parse_thresholds("25", "50")

    assert thresholds.minimum_bytes == 25_000_000_000
    assert disk_guard.capacity_status(24_999_999_999, thresholds) == "failing"
    assert disk_guard.capacity_status(25_000_000_000, thresholds) == "degraded"
    assert disk_guard.capacity_status(50_000_000_000, thresholds) == "ok"


@pytest.mark.parametrize(
    ("minimum", "warning"),
    [("0", "50"), ("25", "0"), ("not-a-number", "50"), ("50", "25")],
)
def test_thresholds_reject_invalid_or_inverted_values(
    minimum: str, warning: str
) -> None:
    with pytest.raises(ValueError):
        disk_guard.parse_thresholds(minimum, warning)


def test_docker_volume_and_path_parsers_are_strict() -> None:
    assert disk_guard.parse_volume_names(
        "\ncti-bulletin_minio_data\ncti-bulletin_minio_data\n"
    ) == ("cti-bulletin_minio_data",)
    assert (
        disk_guard.parse_single_path("/var/lib/docker\n", "Docker root")
        == "/var/lib/docker"
    )
    with pytest.raises(ValueError, match="no unique"):
        disk_guard.parse_single_path("", "Docker root")


def test_gc_is_limited_to_capped_old_cache_and_dangling_images() -> None:
    commands = disk_guard.docker_gc_commands(("docker",), 15, "168h")

    assert commands == (
        (
            "docker",
            "builder",
            "prune",
            "--force",
            "--keep-storage",
            "15GB",
            "--filter",
            "until=168h",
        ),
        ("docker", "image", "prune", "--force"),
    )
    flattened = " ".join(" ".join(command) for command in commands)
    assert "volume" not in flattened
    assert "system" not in flattened
    assert "prune -a" not in flattened


def test_gc_rejects_unbounded_or_invalid_options() -> None:
    with pytest.raises(ValueError, match="positive integer"):
        disk_guard.docker_gc_commands(("docker",), 0, "168h")
    with pytest.raises(ValueError, match="positive duration"):
        disk_guard.docker_gc_commands(("docker",), 15, "forever")


def test_gc_dry_run_prints_commands_without_requiring_docker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    output = io.StringIO()

    disk_guard.run_gc(
        ("docker-not-installed",),
        keep_storage_gb=15,
        until="168h",
        dry_run=True,
        include_dangling_images=True,
        output=output,
    )

    rendered = output.getvalue()
    assert "would run: docker-not-installed builder prune" in rendered
    assert "--keep-storage 15GB --filter until=168h" in rendered
    assert "would run: docker-not-installed image prune --force" in rendered


def test_disk_check_reports_a_clear_error_when_docker_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)

    with pytest.raises(disk_guard.DiskGuardError, match="Docker command not found"):
        disk_guard.collect_disk_usage(("docker",), compose_project="cti-bulletin")


def test_report_marks_each_path_with_its_own_threshold_status() -> None:
    thresholds = disk_guard.parse_thresholds(25, 50)
    report = disk_guard.format_report(
        (
            disk_guard.DiskUsage("Docker root", "/var/lib/docker", 70_000_000_000),
            disk_guard.DiskUsage(
                "MinIO data volume", "/var/lib/docker/volumes/minio", 30_000_000_000
            ),
        ),
        thresholds,
    )

    assert "Docker root" in report and "ok" in report
    assert "MinIO data volume" in report and "degraded" in report


def test_preflight_refuses_below_hard_floor_before_garbage_collection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thresholds = disk_guard.parse_thresholds(25, 50)
    monkeypatch.setattr(
        disk_guard,
        "collect_disk_usage",
        lambda *_args, **_kwargs: (
            (disk_guard.DiskUsage("Docker root", "/docker", 10_000_000_000),),
            True,
        ),
    )
    gc_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        disk_guard, "run_gc", lambda *_args, **kwargs: gc_calls.append(kwargs)
    )

    with pytest.raises(disk_guard.DiskGuardError, match="hard threshold"):
        disk_guard.preflight(
            ("docker",),
            compose_project="cti-bulletin",
            thresholds=thresholds,
            keep_storage_gb=15,
            until="168h",
            auto_gc=True,
            output=io.StringIO(),
        )

    assert gc_calls == []


def test_preflight_uses_cache_only_gc_between_thresholds_and_rechecks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    thresholds = disk_guard.parse_thresholds(25, 50)
    snapshots = iter(
        (
            ((disk_guard.DiskUsage("Docker root", "/docker", 30_000_000_000),), True),
            ((disk_guard.DiskUsage("Docker root", "/docker", 60_000_000_000),), True),
        )
    )
    monkeypatch.setattr(
        disk_guard, "collect_disk_usage", lambda *_args, **_kwargs: next(snapshots)
    )
    gc_calls: list[dict[str, object]] = []
    monkeypatch.setattr(
        disk_guard, "run_gc", lambda *_args, **kwargs: gc_calls.append(kwargs)
    )

    disk_guard.preflight(
        ("docker",),
        compose_project="cti-bulletin",
        thresholds=thresholds,
        keep_storage_gb=15,
        until="168h",
        auto_gc=True,
        output=io.StringIO(),
    )

    assert len(gc_calls) == 1
    assert gc_calls[0]["include_dangling_images"] is False
    assert gc_calls[0]["dry_run"] is False
