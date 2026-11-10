from __future__ import annotations

import json
from pathlib import Path

from cti_app.application.apk_analysis import (
    JADX_JRE_VERSION,
    JADX_LICENSE,
    JADX_PROJECT_URL,
    JADX_URL,
    JADX_VERSION,
    JADX_ZIP_SHA256,
)
from cti_app.infrastructure import jadx


def test_jadx_provenance_requires_pinned_manifest_cli_hash_and_jre(
    monkeypatch, tmp_path: Path
) -> None:
    monkeypatch.delenv("AUTOWORK_JADX_BINARY", raising=False)
    manifest = tmp_path / "PROVENANCE.json"
    manifest.write_text(
        json.dumps(
            {
                "package": "jadx",
                "version": JADX_VERSION,
                "zip_sha256": JADX_ZIP_SHA256,
                "url": JADX_URL,
                "project_url": JADX_PROJECT_URL,
                "license": JADX_LICENSE,
                "jre_version": JADX_JRE_VERSION,
            }
        ),
        encoding="utf-8",
    )
    binary = tmp_path / "jadx"
    binary.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    binary.chmod(0o755)
    monkeypatch.setattr(jadx, "JADX_MANIFEST", manifest)
    monkeypatch.setattr(jadx, "JADX_BINARY", binary)
    monkeypatch.setattr(jadx, "jadx_version", lambda _binary: JADX_VERSION)
    monkeypatch.setattr(jadx, "java_runtime_version", lambda: "17.0.14")

    provenance = jadx.jadx_provenance()
    assert provenance == {
        "jadx": JADX_VERSION,
        "jadx_zip_sha256": JADX_ZIP_SHA256,
        "jadx_jre_version": "17.0.14",
    }
    assert jadx.jadx_provenance(address_space_limit_bytes=4 * 1024**3) is None

    monkeypatch.setattr(jadx, "java_runtime_version", lambda: "11.0.22")
    assert jadx.jadx_provenance() is None

    monkeypatch.setattr(jadx, "java_runtime_version", lambda: "21.0.12.1")
    manifest.write_text(
        json.dumps(
            {
                "package": "jadx",
                "version": JADX_VERSION,
                "zip_sha256": "0" * 64,
                "url": JADX_URL,
                "project_url": JADX_PROJECT_URL,
                "license": JADX_LICENSE,
                "jre_version": JADX_JRE_VERSION,
            }
        ),
        encoding="utf-8",
    )
    assert jadx.jadx_provenance() is None
