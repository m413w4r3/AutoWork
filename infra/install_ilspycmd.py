"""Fetch and verify the locked ILSpy .NET tool at image-build time only."""

from __future__ import annotations

import hashlib
import io
import json
import os
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path, PurePosixPath

from cti_app.application.dotnet_analysis import (
    ILSPYCMD_LICENSE,
    ILSPYCMD_PACKAGE_SHA512,
    ILSPYCMD_PROJECT_URL,
    ILSPYCMD_SOURCE_COMMIT,
    ILSPYCMD_VERSION,
)

PACKAGE_URL = (
    "https://api.nuget.org/v3-flatcontainer/ilspycmd/"
    f"{ILSPYCMD_VERSION}/ilspycmd.{ILSPYCMD_VERSION}.nupkg"
)
INSTALL_ROOT = Path("/opt/ilspycmd")
_NUSPEC_NAMESPACE = {"n": "http://schemas.microsoft.com/packaging/2012/06/nuspec.xsd"}


def main() -> None:
    with urllib.request.urlopen(PACKAGE_URL, timeout=60) as response:
        package = response.read(32 * 1024 * 1024 + 1)
    if len(package) > 32 * 1024 * 1024:
        raise SystemExit("ILSpy package exceeds the build-time size limit")
    package_sha512 = hashlib.sha512(package).hexdigest()
    if package_sha512 != ILSPYCMD_PACKAGE_SHA512:
        raise SystemExit("ILSpy NuGet package SHA-512 does not match the source lock")

    with zipfile.ZipFile(io.BytesIO(package)) as archive:
        nuspec_name = next(
            (name for name in archive.namelist() if name.lower().endswith(".nuspec")),
            None,
        )
        if nuspec_name is None:
            raise SystemExit("ILSpy package has no NuGet specification")
        _verify_nuspec(archive.read(nuspec_name))
        prefix = "tools/net8.0/any/"
        members = [
            item for item in archive.infolist() if item.filename.startswith(prefix)
        ]
        if not members:
            raise SystemExit(
                "ILSpy package does not contain the locked .NET 8 tool assets"
            )
        INSTALL_ROOT.mkdir(parents=True, exist_ok=True)
        for item in members:
            relative = PurePosixPath(item.filename)
            if relative.is_absolute() or ".." in relative.parts:
                raise SystemExit("ILSpy package contains an unsafe path")
            destination = INSTALL_ROOT.joinpath(*relative.parts)
            if item.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
            else:
                destination.parent.mkdir(parents=True, exist_ok=True)
                destination.write_bytes(archive.read(item))

    wrapper = Path("/usr/local/bin/ilspycmd")
    wrapper.write_text(
        '#!/bin/sh\nexec dotnet /opt/ilspycmd/tools/net8.0/any/ilspycmd.dll "$@"\n',
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    provenance = {
        "package": "ilspycmd",
        "version": ILSPYCMD_VERSION,
        "package_sha512": package_sha512,
        "source_commit": ILSPYCMD_SOURCE_COMMIT,
        "project_url": ILSPYCMD_PROJECT_URL,
        "license": ILSPYCMD_LICENSE,
        "feed": "https://api.nuget.org/v3-flatcontainer",
        "runtime": "Microsoft.NETCore.App 8.0",
    }
    (INSTALL_ROOT / "PROVENANCE.json").write_text(
        json.dumps(provenance, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    os.chmod(INSTALL_ROOT, 0o555)
    for path in INSTALL_ROOT.rglob("*"):
        os.chmod(path, 0o555 if path.is_dir() else 0o444)


def _verify_nuspec(payload: bytes) -> None:
    root = ET.fromstring(payload)
    metadata = root.find("n:metadata", _NUSPEC_NAMESPACE)
    if metadata is None:
        raise SystemExit("ILSpy NuGet metadata is missing")
    package_id = metadata.findtext("n:id", namespaces=_NUSPEC_NAMESPACE)
    version = metadata.findtext("n:version", namespaces=_NUSPEC_NAMESPACE)
    project_url = metadata.findtext("n:projectUrl", namespaces=_NUSPEC_NAMESPACE)
    license_node = metadata.find("n:license", _NUSPEC_NAMESPACE)
    repository = metadata.find("n:repository", _NUSPEC_NAMESPACE)
    if (
        package_id != "ilspycmd"
        or version != ILSPYCMD_VERSION
        or project_url != ILSPYCMD_PROJECT_URL
        or license_node is None
        or license_node.attrib != {"type": "expression"}
        or (license_node.text or "").strip() != ILSPYCMD_LICENSE
        or repository is None
        or repository.attrib.get("url") != ILSPYCMD_PROJECT_URL
        or repository.attrib.get("commit") != ILSPYCMD_SOURCE_COMMIT
    ):
        raise SystemExit(
            "ILSpy package provenance or MIT license does not match the source lock"
        )


if __name__ == "__main__":
    main()
