"""Static platform contract for confined agent scripts in the analysis-worker.

These checks do not start Docker.  They fail if a later compose or Dockerfile
change weakens the container-level backstops that complete the per-execution
subreaper, Landlock and rlimit stack (M24.1 F2-A).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, cast

import yaml

from cti_app.application.confined_analysis import CONFINED_SCRIPT_MAX_PROCESSES_RANGE

_REPOSITORY_ROOT = Path(__file__).resolve().parents[2]


def _compose_document() -> dict[str, Any]:
    with (_REPOSITORY_ROOT / "compose.yaml").open(encoding="utf-8") as handle:
        return cast("dict[str, Any]", yaml.safe_load(handle))


def test_analysis_worker_keeps_the_script_platform_boundaries() -> None:
    document = _compose_document()
    worker = document["services"]["analysis-worker"]
    assert worker["user"] == "10001:10001"
    assert worker["read_only"] is True
    assert worker["cap_drop"] == ["ALL"]
    assert "no-new-privileges:true" in worker["security_opt"]
    assert worker["pids_limit"] == 128
    assert worker["mem_limit"] == "2g"
    assert worker["cpus"] == 2.0
    assert worker["ulimits"]["core"] == 0
    assert any(entry.startswith("/tmp:rw,noexec,nosuid,nodev") for entry in worker["tmpfs"])
    assert "network_mode" not in worker
    assert "ports" not in worker


def test_analysis_worker_has_no_external_network() -> None:
    document = _compose_document()
    worker = document["services"]["analysis-worker"]
    assert worker["networks"] == ["analysis-internal"]
    assert document["networks"]["analysis-internal"]["internal"] is True


def test_script_process_quota_stays_inside_the_container_pid_budget() -> None:
    document = _compose_document()
    worker = document["services"]["analysis-worker"]
    maximum_processes = CONFINED_SCRIPT_MAX_PROCESSES_RANGE[1]
    assert maximum_processes <= worker["pids_limit"]


def test_worker_image_still_runs_as_the_unprivileged_uid() -> None:
    dockerfile = (_REPOSITORY_ROOT / "infra" / "analysis-worker.Dockerfile").read_text(
        encoding="utf-8"
    )
    assert "useradd --system --uid 10001 analysis" in dockerfile
    assert "USER analysis" in dockerfile
    assert "USER root" not in dockerfile


def test_container_verification_script_keeps_the_production_worker_options() -> None:
    """The on-demand battery must run under the same container backstops (F2-B).

    It runs ``docker run --rm`` only, so this is a static contract: the script
    is allowed to exist for operators but CI never starts Docker.
    """
    script = _REPOSITORY_ROOT / "scripts" / "ops" / "verify_run_script_confinement_container.sh"
    assert script.is_file()
    assert os.access(script, os.X_OK)
    text = script.read_text(encoding="utf-8")
    assert text.startswith("#!/usr/bin/env bash")
    assert "set -euo pipefail" in text
    for option in (
        "timeout 120 docker run --rm",
        "--network none",
        "--user 10001:10001",
        "--read-only",
        "--tmpfs /tmp:rw,noexec,nosuid,nodev,size=512m",
        "--cap-drop ALL",
        "--security-opt no-new-privileges:true",
        "--pids-limit 128",
        "--memory 2g",
        "--cpus 2",
        "--ulimit core=0",
        "cti-bulletin-analysis-worker:latest",
        "probe_confined_script_execution",
        "probe_script_sandbox",
        "final_proc_oracle",
    ):
        assert option in text, option
