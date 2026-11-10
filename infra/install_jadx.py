"""Fetch and verify the locked jadx distribution at analysis-worker build time only."""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import stat
import tempfile
import urllib.request
import zipfile
from pathlib import Path, PurePosixPath

from cti_app.application.apk_analysis import (
    JADX_JRE_VERSION,
    JADX_LICENSE,
    JADX_PROJECT_URL,
    JADX_URL,
    JADX_VERSION,
    JADX_ZIP_SHA256,
)

INSTALL_ROOT = Path("/opt/jadx")
MAX_PACKAGE_BYTES = 96 * 1024 * 1024
MAX_UNPACKED_BYTES = 128 * 1024 * 1024
MAX_FILES = 256


def main() -> None:
    with urllib.request.urlopen(JADX_URL, timeout=60) as response:
        package = response.read(MAX_PACKAGE_BYTES + 1)
    if len(package) > MAX_PACKAGE_BYTES:
        raise SystemExit("jadx package exceeds the build-time size limit")
    install_archive(package, INSTALL_ROOT)
    launcher = Path("/usr/local/bin/jadx")
    launcher.write_text("#!/bin/sh\nexec /opt/jadx/bin/jadx \"$@\"\n", encoding="utf-8")
    launcher.chmod(0o755)


def install_archive(package: bytes, install_root: Path) -> None:
    """Verify the complete archive before extracting any member to a staging tree."""
    if len(package) > MAX_PACKAGE_BYTES:
        raise SystemExit("jadx package exceeds the build-time size limit")
    if hashlib.sha256(package).hexdigest() != JADX_ZIP_SHA256:
        raise SystemExit("jadx ZIP SHA-256 does not match the source lock")
    if install_root.exists() or install_root.is_symlink():
        raise SystemExit("jadx install root already exists")

    parent = install_root.parent
    parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".jadx-install-", dir=parent) as temporary:
        staging_root = Path(temporary)
        with zipfile.ZipFile(io.BytesIO(package)) as archive:
            members = archive.infolist()
            if len(members) > MAX_FILES:
                raise SystemExit("jadx archive has too many files")
            if sum(item.file_size for item in members) > MAX_UNPACKED_BYTES:
                raise SystemExit("jadx archive exceeds the unpacked size limit")
            if not members:
                raise SystemExit("jadx archive is empty")
            for item in members:
                relative = _safe_relative_path(item)
                mode = item.external_attr >> 16
                if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                    raise SystemExit("jadx archive contains a non-regular file")
                if item.is_dir():
                    (staging_root / Path(*relative.parts)).mkdir(parents=True, exist_ok=True)
                    continue
                destination = staging_root / Path(*relative.parts)
                destination.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(item, "r") as source, destination.open("xb") as target:
                    shutil.copyfileobj(source, target, length=1024 * 1024)
                if item.external_attr >> 16 & 0o111:
                    destination.chmod(0o755)
                else:
                    destination.chmod(0o644)

        cli = staging_root / "bin" / "jadx"
        jar = staging_root / "lib" / f"jadx-{JADX_VERSION}-all.jar"
        if not cli.is_file() or not jar.is_file():
            raise SystemExit("jadx archive does not contain the expected CLI and JAR")
        (staging_root / "PROVENANCE.json").write_text(
            json.dumps(
                {
                    "package": "jadx",
                    "version": JADX_VERSION,
                    "zip_sha256": JADX_ZIP_SHA256,
                    "url": JADX_URL,
                    "project_url": JADX_PROJECT_URL,
                    "license": JADX_LICENSE,
                    "jre_version": JADX_JRE_VERSION,
                    "runtime": "OpenJDK 17 JRE headless",
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n",
            encoding="utf-8",
        )
        (staging_root / "PROVENANCE.json").chmod(0o444)
        for directory in sorted(
            (path for path in staging_root.rglob("*") if path.is_dir()),
            key=lambda path: len(path.parts),
            reverse=True,
        ):
            directory.chmod(0o555)
        staging_root.chmod(0o555)
        os.replace(staging_root, install_root)


def _safe_relative_path(item: zipfile.ZipInfo) -> PurePosixPath:
    name = item.filename
    raw_name = name[:-1] if item.is_dir() and name.endswith("/") else name
    path = PurePosixPath(raw_name)
    mode = item.external_attr >> 16
    if (
        not name
        or "\\" in name
        or ":" in name
        or raw_name.startswith("/")
        or any(part in {"", ".", ".."} for part in raw_name.split("/"))
        or path.is_absolute()
        or stat.S_ISLNK(mode)
    ):
        raise SystemExit("jadx archive contains an unsafe path")
    return path


if __name__ == "__main__":
    main()
