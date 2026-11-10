from __future__ import annotations

import hashlib
import importlib.util
import json
import stat
import zipfile
from io import BytesIO
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parents[2]
_INSTALLER_PATH = _REPO_ROOT / "infra/install_jadx.py"
_SPEC = importlib.util.spec_from_file_location("autowork_install_jadx", _INSTALLER_PATH)
assert _SPEC is not None and _SPEC.loader is not None
install_jadx = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(install_jadx)


@pytest.fixture(autouse=True)
def _restore_tree_permissions(tmp_path: Path):
    """The installer makes its tree read-only; pytest and `rm` must still clean it up."""
    yield
    for path in [tmp_path, *tmp_path.rglob("*")]:
        if path.is_dir() and not path.is_symlink():
            path.chmod(0o700)


def _zip_package(entries: list[tuple[str, bytes]]) -> bytes:
    stream = BytesIO()
    with zipfile.ZipFile(stream, "w") as archive:
        for name, content in entries:
            info = zipfile.ZipInfo(name)
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            archive.writestr(info, content)
    return stream.getvalue()


def test_jadx_installer_extracts_only_locked_safe_regular_files(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = _zip_package(
        [
            ("bin/jadx", b"#!/bin/sh\nexit 0\n"),
            ("lib/jadx-1.5.6-all.jar", b"fixture jar marker"),
        ]
    )
    monkeypatch.setattr(install_jadx, "JADX_ZIP_SHA256", hashlib.sha256(package).hexdigest())
    destination = tmp_path / "opt/jadx"

    install_jadx.install_archive(package, destination)

    assert (destination / "bin/jadx").read_bytes().startswith(b"#!/bin/sh")
    assert (destination / "bin/jadx").stat().st_mode & 0o111
    assert (destination / "lib/jadx-1.5.6-all.jar").read_bytes() == b"fixture jar marker"
    provenance = json.loads((destination / "PROVENANCE.json").read_text(encoding="utf-8"))
    assert provenance["zip_sha256"] == hashlib.sha256(package).hexdigest()


def test_jadx_installer_refuses_zip_slip_and_checks_hash_before_extraction(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    package = _zip_package(
        [
            ("../escape.txt", b"no"),
            ("bin/jadx", b"cli"),
            ("lib/jadx-1.5.6-all.jar", b"jar"),
        ]
    )
    destination = tmp_path / "opt/jadx"
    monkeypatch.setattr(install_jadx, "JADX_ZIP_SHA256", "0" * 64)
    with pytest.raises(SystemExit, match="SHA-256"):
        install_jadx.install_archive(package, destination)
    assert not (tmp_path / "escape.txt").exists()
    assert not destination.exists()

    monkeypatch.setattr(install_jadx, "JADX_ZIP_SHA256", hashlib.sha256(package).hexdigest())
    with pytest.raises(SystemExit, match="unsafe path"):
        install_jadx.install_archive(package, destination)
    assert not (tmp_path / "escape.txt").exists()
    assert not destination.exists()
