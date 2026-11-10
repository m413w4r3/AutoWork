"""Pinned jadx/JRE provenance checks for the confined APK analysis family."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any

from cti_app.application.apk_analysis import (
    JADX_JRE_VERSION,
    JADX_LICENSE,
    JADX_PROJECT_URL,
    JADX_URL,
    JADX_VERSION,
    JADX_ZIP_SHA256,
)
from cti_app.config import (
    APK_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES,
    APK_ANALYSIS_MAX_ADDRESS_SPACE_LIMIT_BYTES,
    APK_ANALYSIS_MIN_ADDRESS_SPACE_LIMIT_BYTES,
)

JADX_MANIFEST = Path("/opt/jadx/PROVENANCE.json")
JADX_BINARY = Path("/opt/jadx/bin/jadx")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_JADX_VERSION_LINE = re.compile(r"^\s*([0-9]+\.[0-9]+\.[0-9]+)\s*$")
_JAVA_VERSION_LINE = re.compile(r'^(?:openjdk|java) version "([^"]+)"')


def resolve_jadx_binary() -> str | None:
    override = os.environ.get("AUTOWORK_JADX_BINARY")
    if override:
        path = Path(override)
        return str(path) if path.is_file() and os.access(path, os.X_OK) else None
    return shutil.which("jadx")


def jadx_version(binary: str | None = None) -> str | None:
    executable = binary or resolve_jadx_binary()
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    output = (completed.stdout + completed.stderr).strip()
    match = _JADX_VERSION_LINE.fullmatch(output)
    return match.group(1) if completed.returncode == 0 and match is not None else None


def java_runtime_version() -> str | None:
    executable = shutil.which("java")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "-version"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
            env={"PATH": "/usr/local/bin:/usr/bin:/bin", "LANG": "C", "LC_ALL": "C"},
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (completed.stderr + completed.stdout).splitlines()
    match = _JAVA_VERSION_LINE.match(lines[0]) if lines else None
    if completed.returncode != 0 or match is None:
        return None
    return match.group(1)


def supported_jre_version(value: str | None) -> bool:
    """The analysis-worker image contract pins OpenJDK's major version to 17."""
    return _jre_major(value) == int(JADX_JRE_VERSION)


def host_test_jre_compatible(value: str | None) -> bool:
    """Allow newer host Java only for tests using AUTOWORK_JADX_BINARY."""
    major = _jre_major(value)
    return major is not None and major >= int(JADX_JRE_VERSION)


def _jre_major(value: str | None) -> int | None:
    if not value:
        return None
    try:
        return int(value.split(".", maxsplit=1)[0])
    except ValueError:
        return None


def jadx_provenance(
    *, address_space_limit_bytes: int = APK_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES,
) -> dict[str, str] | None:
    """Attest only the installed manifest, CLI version, locked archive and supported JRE."""
    if not (
        APK_ANALYSIS_MIN_ADDRESS_SPACE_LIMIT_BYTES
        <= address_space_limit_bytes
        <= APK_ANALYSIS_MAX_ADDRESS_SPACE_LIMIT_BYTES
    ):
        return None
    if os.environ.get("AUTOWORK_JADX_BINARY"):
        # The override exists only for offline host tests; it cannot attest an image worker.
        return None
    try:
        manifest: Any = json.loads(JADX_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    expected = {
        "package": "jadx",
        "version": JADX_VERSION,
        "zip_sha256": JADX_ZIP_SHA256,
        "url": JADX_URL,
        "project_url": JADX_PROJECT_URL,
        "license": JADX_LICENSE,
        "jre_version": JADX_JRE_VERSION,
    }
    if not isinstance(manifest, dict) or any(
        manifest.get(key) != value for key, value in expected.items()
    ):
        return None
    if not isinstance(manifest.get("zip_sha256"), str) or _SHA256.fullmatch(
        manifest["zip_sha256"]
    ) is None:
        return None
    executable = (
        str(JADX_BINARY)
        if JADX_BINARY.is_file() and os.access(JADX_BINARY, os.X_OK)
        else None
    )
    if executable is None:
        return None
    version = jadx_version(executable)
    runtime_version = java_runtime_version()
    if version != JADX_VERSION or not supported_jre_version(runtime_version):
        return None
    return {
        "jadx": version,
        "jadx_zip_sha256": JADX_ZIP_SHA256,
        "jadx_jre_version": runtime_version or JADX_JRE_VERSION,
    }
