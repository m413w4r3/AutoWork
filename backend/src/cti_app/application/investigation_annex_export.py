"""Deterministic annex package projection for one canonical investigation snapshot."""

from __future__ import annotations

import csv
import ctypes
import errno
import fcntl
import hashlib
import io
import json
import os
import re
import shutil
import tempfile
from collections import defaultdict
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any

ANNEX_MANIFEST_SCHEMA = "autowork.investigation-annex-manifest.v1"
_SHA256 = re.compile(r"^[0-9a-f]{64}$", re.ASCII)
_SAFE_LEAF = re.compile(r"[^A-Za-z0-9._-]+", re.ASCII)
_AVAILABILITY = frozenset({"present", "unavailable", "restricted", "absent"})
_RESTRICTED_TYPES = frozenset(
    {"sample", "network_capture", "protocol_note", "technical_figure", "technical_note"}
)
_MANIFEST_METADATA_KEYS = frozenset(
    {
        "author",
        "author_status",
        "candidate_id",
        "candidate_status",
        "candidate_version",
        "detection_empirically_validated",
        "detection_result",
        "engine_status",
        "external_llm_allowed",
        "fact_refs",
        "figure_kind",
        "ioc_type",
        "license_restriction_present",
        "media_kind",
        "compiler_name",
        "compiler_version",
        "policy_version",
        "origin_kind",
        "origin",
        "origin_tool",
        "origin_invocation_id",
        "source_observation_ids",
        "parent_version",
        "pivot_criteria",
        "qualification",
        "relation",
        "related_to",
        "replay_status",
        "rule_kind",
        "rule_syntax_validation",
        "sample_state",
        "source_service",
        "source_object_id",
        "acquisition_events",
        "status",
        "test_refs",
        "test_report_ids",
        "tlp",
        "value",
    }
)


@dataclass(frozen=True, slots=True)
class AnnexAsset:
    """One canonical record or content-addressed object to represent in the package."""

    canonical_id: str
    type: str
    investigation_id: str
    source: str
    origin: str
    version: str | int | None = None
    sha256: str | None = None
    references: tuple[str, ...] = ()
    availability: str = "present"
    restrictions: tuple[str, ...] = ()
    content: bytes | None = None
    filename: str | None = None
    metadata: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class InvestigationAnnexSnapshot:
    """Only canonical PostgreSQL/MinIO data selected by the read adapter."""

    investigation_id: str
    assets: Sequence[AnnexAsset] = ()


@dataclass(frozen=True, slots=True)
class AnnexExportResult:
    output_dir: Path
    manifest_sha256: str
    object_count: int
    file_count: int


def _canonical_json(value: Any) -> bytes:
    return (
        json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        + b"\n"
    )


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _clean_text(value: object, *, limit: int = 512) -> str:
    text = str(value)
    return "".join(character for character in text if character >= " " and character != "\x7f")[
        :limit
    ]


def _safe_leaf(value: str | None, fallback: str) -> str:
    """Reduce an untrusted filename to one portable path component."""
    candidate = _SAFE_LEAF.sub("_", value or "").strip(" ._")
    if candidate in {"", ".", ".."}:
        candidate = fallback
    if len(candidate) > 120:
        suffix = hashlib.sha256(candidate.encode("utf-8")).hexdigest()[:12]
        candidate = f"{candidate[:100]}-{suffix}"
    return candidate


def _relative_path(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("Annex output path is unsafe")
    if "\\" in value or "\x00" in value:
        raise ValueError("Annex output path is unsafe")
    return path.as_posix()


def _safe_metadata(metadata: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in sorted(_MANIFEST_METADATA_KEYS):
        if key not in metadata:
            continue
        value = metadata[key]
        if value is None or isinstance(value, bool | int | float):
            result[key] = value
        elif isinstance(value, str):
            result[key] = _clean_text(value)
        elif isinstance(value, (tuple, list)):
            result[key] = sorted(_clean_text(item) for item in value)
    return result


def _asset_identity(asset: AnnexAsset) -> tuple[str, str, str]:
    return (asset.type, asset.canonical_id, "" if asset.version is None else str(asset.version))


def _validate_snapshot(snapshot: InvestigationAnnexSnapshot) -> None:
    if not snapshot.investigation_id.strip():
        raise ValueError("Investigation id is required")
    identities: set[tuple[str, str, str]] = set()
    for asset in snapshot.assets:
        if asset.investigation_id != snapshot.investigation_id:
            raise ValueError("Annex asset belongs to another investigation")
        if not asset.canonical_id.strip() or not asset.type.strip():
            raise ValueError("Annex assets require canonical ids and types")
        if asset.availability not in _AVAILABILITY:
            raise ValueError("Annex asset has an unsupported availability state")
        if asset.sha256 is not None and _SHA256.fullmatch(asset.sha256) is None:
            raise ValueError("Annex asset SHA-256 is invalid")
        if asset.content is not None:
            content_sha256 = _sha256(asset.content)
            if asset.sha256 is not None and asset.sha256 != content_sha256:
                raise ValueError("Canonical content SHA-256 does not match its manifest value")
        identity = _asset_identity(asset)
        if identity in identities:
            raise ValueError("Duplicate canonical annex object identity")
        identities.add(identity)


def _rule_key(asset: AnnexAsset) -> str:
    if asset.type == "yara_rule":
        candidate_id = asset.metadata.get("candidate_id")
        if isinstance(candidate_id, str) and candidate_id:
            return candidate_id
    return asset.canonical_id


def _rule_numbers(assets: Sequence[AnnexAsset], kind: str) -> dict[str, int]:
    identities = sorted({_rule_key(asset) for asset in assets if asset.type == kind})
    return {identity: index for index, identity in enumerate(identities, 1)}


def _asset_path(
    asset: AnnexAsset,
    *,
    yara_numbers: Mapping[str, int],
    suricata_numbers: Mapping[str, int],
    report_number: int,
) -> str | None:
    if asset.type == "yara_rule":
        number = yara_numbers[_rule_key(asset)]
        version = _safe_leaf(str(asset.version or "unknown"), "unknown")
        return f"yara/candidate-{number:03d}-v{version}.yar"
    if asset.type == "suricata_rule":
        number = suricata_numbers[_rule_key(asset)]
        version = (
            f"-v{_safe_leaf(str(asset.version), 'unknown')}" if asset.version is not None else ""
        )
        return f"suricata/candidate-{number:03d}{version}.rules"
    if asset.type == "yara_test_report":
        return f"yara/tests/report-{report_number:03d}.json"
    if asset.type == "suricata_test_report":
        return f"suricata/tests/report-{report_number:03d}.json"
    if asset.type == "protocol_note":
        leaf = _safe_leaf(asset.filename, f"note-{report_number:03d}.json")
        return f"network/protocol-notes/restricted/{leaf}"
    if asset.type == "technical_note":
        leaf = _safe_leaf(asset.filename, f"note-{report_number:03d}.txt")
        return f"technical/notes/restricted/{leaf}"
    if asset.type == "technical_figure":
        leaf = _safe_leaf(asset.filename, f"figure-{report_number:03d}.svg")
        return f"technical/figures/restricted/{leaf}"
    if asset.type == "sample":
        digest = asset.sha256 or hashlib.sha256(asset.canonical_id.encode()).hexdigest()
        return f"samples/restricted/{digest}.bin"
    if asset.type == "network_capture":
        digest = asset.sha256 or hashlib.sha256(asset.canonical_id.encode()).hexdigest()
        return f"network/restricted/captures/{digest}.pcap"
    return None


def _csv_bytes(header: Sequence[str], rows: Sequence[Sequence[object]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n", quoting=csv.QUOTE_MINIMAL)
    writer.writerow(header)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def _sorted_refs(asset: AnnexAsset) -> list[str]:
    return sorted({_clean_text(item) for item in asset.references if str(item).strip()})


def _project_assets(
    snapshot: InvestigationAnnexSnapshot, *, include_restricted_archive: bool
) -> tuple[list[dict[str, Any]], dict[str, bytes]]:
    assets = sorted(snapshot.assets, key=_asset_identity)
    yara_numbers = _rule_numbers(assets, "yara_rule")
    suricata_numbers = _rule_numbers(assets, "suricata_rule")
    seen_paths: set[str] = set()
    object_rows: list[dict[str, Any]] = []
    files: dict[str, bytes] = {}
    type_ordinals: defaultdict[str, int] = defaultdict(int)

    for asset in assets:
        type_ordinals[asset.type] += 1
        path = _asset_path(
            asset,
            yara_numbers=yara_numbers,
            suricata_numbers=suricata_numbers,
            report_number=type_ordinals[asset.type],
        )
        availability = asset.availability
        restrictions = sorted({_clean_text(value) for value in asset.restrictions if value.strip()})
        content = asset.content

        if asset.type in _RESTRICTED_TYPES and availability != "absent":
            if not include_restricted_archive:
                availability = "restricted"
                restrictions = sorted(
                    set(restrictions) | {"explicit_restricted_archive_flag_required"}
                )
                content = None
            elif not bool(asset.metadata.get("archive_export_allowed", False)):
                availability = "restricted"
                restrictions = sorted(set(restrictions) | {"canonical_policy_denied"})
                content = None
            elif content is None and availability == "present":
                availability = "unavailable"

        if content is not None and availability == "present":
            actual_sha256 = _sha256(content)
            if asset.sha256 is not None and actual_sha256 != asset.sha256:
                raise ValueError("Canonical annex content failed SHA-256 verification")
            if path is not None:
                path = _relative_path(path)
                if path in seen_paths:
                    relative = PurePosixPath(path)
                    suffix = "".join(relative.suffixes)
                    stem = relative.name[: -len(suffix)] if suffix else relative.name
                    identity_digest = hashlib.sha256(
                        asset.canonical_id.encode("utf-8")
                    ).hexdigest()[:12]
                    path = _relative_path(
                        str(relative.with_name(f"{stem}-{identity_digest}{suffix}"))
                    )
                    if path in seen_paths:
                        raise ValueError("Annex output path collision")
                seen_paths.add(path)
                files[path] = content
        else:
            path = None

        if availability not in _AVAILABILITY:
            raise ValueError("Annex asset has an unsupported availability state")
        row = {
            "canonical_id": _clean_text(asset.canonical_id),
            "type": _clean_text(asset.type),
            "version": asset.version,
            "sha256": asset.sha256
            or (_sha256(asset.content) if asset.content is not None else None),
            "source": _clean_text(asset.source),
            "origin": _clean_text(asset.origin),
            "investigation_id": snapshot.investigation_id,
            "references": _sorted_refs(asset),
            "filename": path,
            "availability": availability,
            "restrictions": restrictions,
            "metadata": _safe_metadata(asset.metadata),
        }
        object_rows.append(row)

    _add_absent_collections(object_rows, snapshot.investigation_id)
    _add_projection_files(files, assets)
    return sorted(
        object_rows,
        key=lambda item: (item["type"], item["canonical_id"], str(item["version"] or "")),
    ), files


def _add_absent_collections(objects: list[dict[str, Any]], investigation_id: str) -> None:
    represented = {item["type"] for item in objects}
    for object_type in (
        "yara_rule",
        "yara_test_report",
        "suricata_rule",
        "suricata_test_report",
        "observed_ioc",
        "discovered_sample",
        "sample",
        "network_capture",
        "protocol_note",
        "technical_figure",
        "technical_note",
    ):
        if object_type in represented:
            continue
        objects.append(
            {
                "canonical_id": f"absent:{investigation_id}:{object_type}",
                "type": object_type,
                "version": None,
                "sha256": None,
                "source": "canonical_snapshot_query",
                "origin": "PostgreSQL+MinIO",
                "investigation_id": investigation_id,
                "references": [],
                "filename": None,
                "availability": "absent",
                "restrictions": [],
                "metadata": {"status": "no_canonical_records"},
            }
        )


def _add_projection_files(files: dict[str, bytes], assets: Sequence[AnnexAsset]) -> None:
    ioc_rows = []
    for asset in assets:
        if asset.type != "observed_ioc":
            continue
        metadata = asset.metadata
        value = metadata.get("value")
        ioc_type = metadata.get("ioc_type")
        if isinstance(value, str) and isinstance(ioc_type, str):
            ioc_rows.append(
                (
                    ioc_type,
                    value,
                    asset.canonical_id,
                    asset.source,
                    asset.sha256 or "",
                    str(metadata.get("relation") or "observed"),
                )
            )
    files["iocs/observed-iocs.csv"] = _csv_bytes(
        ("type", "value", "observation_id", "source", "observation_sha256", "relation"),
        sorted(set(ioc_rows), key=lambda row: tuple(str(item) for item in row)),
    )

    discovered_rows = []
    for asset in assets:
        if asset.type != "discovered_sample":
            continue
        metadata = asset.metadata
        discovered_rows.append(
            (
                metadata.get("value") or asset.sha256 or "",
                metadata.get("status") or "unknown",
                metadata.get("origin_kind") or asset.origin,
                metadata.get("qualification") or "unknown",
                metadata.get("related_to") or "",
                metadata.get("relation") or "observed_relationship_not_family_proof",
                metadata.get("pivot_criteria") or "canonical_relationship_observation",
            )
        )
    files["iocs/discovered-samples.csv"] = _csv_bytes(
        (
            "sha256",
            "acquisition_status",
            "origin",
            "qualification",
            "related_to",
            "relation",
            "pivot_criteria",
        ),
        sorted(set(discovered_rows), key=lambda row: tuple(str(item) for item in row)),
    )

    inventory_rows = []
    for asset in assets:
        if asset.type not in {"sample", "network_capture"}:
            continue
        inventory_rows.append(
            (
                asset.sha256 or "",
                asset.type,
                asset.metadata.get("sample_state") or "unknown",
                asset.metadata.get("qualification") or "unknown",
                asset.metadata.get("origin_kind") or asset.origin,
                asset.metadata.get("tlp") or "unknown",
                "yes" if asset.metadata.get("license_restriction_present") else "no",
                "yes" if asset.metadata.get("external_llm_allowed") else "no",
                asset.metadata.get("source_service") or "unknown",
                asset.metadata.get("source_object_id") or "",
            )
        )
    files["samples/sample-inventory.csv"] = _csv_bytes(
        (
            "sha256",
            "object_type",
            "sample_state",
            "qualification",
            "origin",
            "tlp",
            "license_restricted",
            "external_llm_allowed",
            "source_service",
            "source_object_id",
        ),
        sorted(set(inventory_rows), key=lambda row: tuple(str(item) for item in row)),
    )
    for rule_type, report_type, directory in (
        ("yara_rule", "yara_test_report", "yara/tests"),
        ("suricata_rule", "suricata_test_report", "suricata/tests"),
    ):
        reports_by_ref: defaultdict[str, list[str]] = defaultdict(list)
        for report in assets:
            if report.type == report_type and report.availability != "absent":
                for reference in report.references:
                    reports_by_ref[reference].append(report.canonical_id)
        index_rows = []
        for rule in assets:
            if rule.type != rule_type:
                continue
            version_ref = (
                f"{rule.canonical_id}:v{rule.version}"
                if rule.version is not None
                else rule.canonical_id
            )
            index_rows.append(
                {
                    "canonical_id": rule.canonical_id,
                    "version": rule.version,
                    "sha256": rule.sha256
                    or (_sha256(rule.content) if rule.content is not None else None),
                    "test_refs": sorted(str(value) for value in rule.metadata.get("test_refs", ())),
                    "test_report_ids": sorted(
                        set(reports_by_ref.get(rule.canonical_id, ()))
                        | set(reports_by_ref.get(version_ref, ()))
                    ),
                }
            )
        files[f"{directory}/index.json"] = _canonical_json(index_rows)
    files["technical/notes/README.txt"] = (
        b"Technical notes and protocol facts are represented by the canonical object references "
        b"in manifest.json. Missing or policy-restricted material is listed there explicitly.\n"
    )


@contextmanager
def _export_lock(destination: Path) -> Iterator[None]:
    lock_path = destination.parent / f".{destination.name}.annex-export.lock"
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _write_file(root: Path, relative_path: str, content: bytes) -> dict[str, Any]:
    relative_path = _relative_path(relative_path)
    path = root.joinpath(*PurePosixPath(relative_path).parts)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    with path.open("xb") as stream:
        stream.write(content)
        stream.flush()
        os.fsync(stream.fileno())
    mode = (
        0o400
        if relative_path.startswith(("samples/restricted/", "network/restricted/captures/"))
        else 0o600
    )
    os.chmod(path, mode)
    return {"path": relative_path, "sha256": _sha256(content), "size_bytes": len(content)}


def _exchange_directories(first: Path, second: Path) -> bool:
    """Atomically swap two directory names where Linux renameat2 is available."""
    renameat2 = getattr(ctypes.CDLL(None, use_errno=True), "renameat2", None)
    if renameat2 is None:
        return False
    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(-100, os.fsencode(first), -100, os.fsencode(second), 2)
    if result == 0:
        return True
    error_number = ctypes.get_errno()
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP}:
        return False
    raise OSError(error_number, os.strerror(error_number), str(second))


def _commit_directory(stage: Path, destination: Path, *, overwrite: bool) -> None:
    backup: Path | None = None
    if destination.exists() or destination.is_symlink():
        if destination.is_symlink() or not destination.is_dir():
            raise FileExistsError("Annex destination exists and is not a regular directory")
        if not overwrite:
            raise FileExistsError("Annex destination already exists; use --overwrite to replace it")
        if _exchange_directories(stage, destination):
            shutil.rmtree(stage)
            return
        backup = destination.parent / f".{destination.name}.backup-{os.getpid()}-{id(stage)}"
        if backup.exists():
            raise FileExistsError("Annex replacement is already in progress")
        os.replace(destination, backup)
    try:
        os.replace(stage, destination)
    except BaseException:
        if backup is not None:
            os.replace(backup, destination)
        raise
    if backup is not None:
        shutil.rmtree(backup)


def write_investigation_annex(
    snapshot: InvestigationAnnexSnapshot,
    output_dir: str | Path,
    *,
    include_restricted_archive: bool = False,
    overwrite: bool = False,
) -> AnnexExportResult:
    """Write a deterministic, policy-aware annex directory from canonical snapshot data."""
    _validate_snapshot(snapshot)
    destination = Path(output_dir).expanduser()
    if not destination.name:
        raise ValueError("Annex output directory must name a directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination = destination.absolute()

    with _export_lock(destination):
        if (destination.exists() or destination.is_symlink()) and not overwrite:
            raise FileExistsError("Annex destination already exists; use --overwrite to replace it")
        objects, content_files = _project_assets(
            snapshot, include_restricted_archive=include_restricted_archive
        )
        stage = Path(tempfile.mkdtemp(prefix=f".{destination.name}.stage-", dir=destination.parent))
        os.chmod(stage, 0o700)
        try:
            generated_files = [
                _write_file(stage, path, content) for path, content in sorted(content_files.items())
            ]
            manifest = {
                "schema": ANNEX_MANIFEST_SCHEMA,
                "investigation_id": snapshot.investigation_id,
                "restricted_archive_included": include_restricted_archive,
                "objects": objects,
                # Exclude manifest files to avoid a hash cycle.
                "files": generated_files,
            }
            manifest_bytes = _canonical_json(manifest)
            manifest_sha256 = _sha256(manifest_bytes)
            _write_file(stage, "manifest.json", manifest_bytes)
            _write_file(stage, "manifest.sha256", f"{manifest_sha256}  manifest.json\n".encode())
            _commit_directory(stage, destination, overwrite=overwrite)
        except BaseException:
            if stage.exists():
                shutil.rmtree(stage)
            raise

    return AnnexExportResult(
        output_dir=destination,
        manifest_sha256=manifest_sha256,
        object_count=len(objects),
        file_count=len(generated_files),
    )
