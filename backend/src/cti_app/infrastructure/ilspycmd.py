"""Pinned ILSpy command-line provenance used by the confined analysis worker."""

from __future__ import annotations

import json
import os
import re
import resource
import shutil
import subprocess
from pathlib import Path
from typing import Any

from cti_app.application.dotnet_analysis import (
    ILSPYCMD_LICENSE,
    ILSPYCMD_PACKAGE_SHA512,
    ILSPYCMD_PROJECT_URL,
    ILSPYCMD_SOURCE_COMMIT,
    ILSPYCMD_VERSION,
)
from cti_app.config import (
    DOTNET_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES,
    DOTNET_ANALYSIS_MAX_ADDRESS_SPACE_LIMIT_BYTES,
    DOTNET_ANALYSIS_MIN_ADDRESS_SPACE_LIMIT_BYTES,
    DOTNET_GC_CONCURRENT,
    DOTNET_GC_HEAP_HARD_LIMIT,
)

ILSPYCMD_MANIFEST = Path("/opt/ilspycmd/PROVENANCE.json")
_SHA512 = re.compile(r"^[0-9a-f]{128}$")


def ilspycmd_provenance(
    *, address_space_limit_bytes: int = DOTNET_ANALYSIS_DEFAULT_ADDRESS_SPACE_LIMIT_BYTES
) -> dict[str, str] | None:
    """Return provenance only when image manifest and installed CLI match the lock."""
    if not (
        DOTNET_ANALYSIS_MIN_ADDRESS_SPACE_LIMIT_BYTES
        <= address_space_limit_bytes
        <= DOTNET_ANALYSIS_MAX_ADDRESS_SPACE_LIMIT_BYTES
    ):
        return None
    executable = shutil.which("ilspycmd")
    try:
        manifest: Any = json.loads(ILSPYCMD_MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(manifest, dict):
        return None
    expected = {
        "package": "ilspycmd",
        "version": ILSPYCMD_VERSION,
        "package_sha512": ILSPYCMD_PACKAGE_SHA512,
        "source_commit": ILSPYCMD_SOURCE_COMMIT,
        "project_url": ILSPYCMD_PROJECT_URL,
        "license": ILSPYCMD_LICENSE,
    }
    if any(manifest.get(key) != value for key, value in expected.items()):
        return None
    package_hash = manifest.get("package_sha512")
    if not isinstance(package_hash, str) or _SHA512.fullmatch(package_hash) is None:
        return None
    if executable is None:
        return None

    def set_address_space_limit() -> None:
        resource.setrlimit(
            resource.RLIMIT_AS,
            (address_space_limit_bytes, address_space_limit_bytes),
        )

    try:
        completed = subprocess.run(
            [executable, "--disable-updatecheck", "--version"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
            env={
                "PATH": "/usr/local/bin:/usr/bin:/bin",
                "DOTNET_ROOT": "/usr/share/dotnet",
                "DOTNET_CLI_TELEMETRY_OPTOUT": "1",
                "DOTNET_NOLOGO": "1",
                "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
                "DOTNET_GCHeapHardLimit": DOTNET_GC_HEAP_HARD_LIMIT,
                "DOTNET_gcConcurrent": DOTNET_GC_CONCURRENT,
            },
            preexec_fn=set_address_space_limit if os.name == "posix" else None,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    lines = (completed.stdout + completed.stderr).splitlines()
    version_line = re.fullmatch(r"ilspycmd:\s*([0-9.]+)", lines[0].strip()) if lines else None
    if (
        completed.returncode != 0
        or version_line is None
        or version_line.group(1) != ILSPYCMD_VERSION
    ):
        return None
    return expected
