"""Bounded snapshots of the exact inputs used by a Typst render."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
from collections.abc import Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any
from uuid import UUID

from cti_app.application.typst_bundle import hash_bundle_contents
from cti_app.application.typst_rendering import TypstRenderSource, TypstTemplateBundle

SNAPSHOT_SCHEMA = "typst-render-input-snapshot-v1"
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_SNAPSHOT_CONTENT_BYTES = 24 * 1024 * 1024
MAX_SNAPSHOT_FILE_BYTES = 16 * 1024 * 1024
MAX_RENDER_DATA_BYTES = 8 * 1024 * 1024


def preview_inspection_id(preview_input_hash: str, pdf_sha256: str) -> str:
    """Return an opaque stable id for one disposable preview output and its inputs."""
    payload = json.dumps(
        {
            "kind": "edition_preview",
            "preview_input_hash": preview_input_hash,
            "pdf_sha256": pdf_sha256,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def preview_snapshot_cache_key(inspection_id: str) -> str:
    return f"typst-render-inputs:{inspection_id}"


def safe_bundle_path(value: object) -> str | None:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        return None
    posix = PurePosixPath(value)
    windows = PureWindowsPath(value)
    if (
        posix.is_absolute()
        or windows.is_absolute()
        or windows.drive
        or any(part in {"", ".", ".."} for part in value.split("/"))
    ):
        return None
    return value


def build_typst_input_snapshot(
    *,
    template_bundle: TypstTemplateBundle,
    render_source: TypstRenderSource,
    resolved_media: Mapping[UUID, bytes],
    render_id: str | None = None,
    preview_input_hash: str | None = None,
    pdf_sha256: str | None = None,
) -> bytes | None:
    """Serialize verified effective inputs; return None when the bounded limit is exceeded."""
    files: list[dict[str, Any]] = []
    raw_size = 0
    normalized_template: list[tuple[str, bytes]] = []
    seen_template_paths: set[str] = set()
    for file in template_bundle.files:
        relative_path = safe_bundle_path(file.relative_path)
        if (
            relative_path is None
            or relative_path in seen_template_paths
            or len(file.content) > MAX_SNAPSHOT_FILE_BYTES
        ):
            return None
        seen_template_paths.add(relative_path)
        digest = hashlib.sha256(file.content).hexdigest()
        raw_size += len(file.content)
        normalized_template.append((relative_path, file.content))
        files.append(
            {
                "relative_path": relative_path,
                "kind": "template",
                "sha256": digest,
                "byte_size": len(file.content),
                "content_base64": base64.b64encode(file.content).decode("ascii"),
            }
        )

    if hash_bundle_contents(normalized_template) != template_bundle.sha256:
        return None
    entrypoint = next(
        (
            content
            for path, content in normalized_template
            if path == render_source.entrypoint_relative_path
        ),
        None,
    )
    if entrypoint != render_source.source_bytes:
        return None
    source_sha256 = hashlib.sha256(render_source.source_bytes).hexdigest()
    render_data_sha256 = hashlib.sha256(render_source.render_data_bytes).hexdigest()
    if (
        source_sha256 != render_source.source_sha256
        or render_data_sha256 != render_source.render_data_sha256
    ):
        return None

    media: list[dict[str, Any]] = []
    media_refs: list[dict[str, Any]] = []
    for media_ref in render_source.media_refs:
        relative_path = safe_bundle_path(media_ref.media_path)
        content = resolved_media.get(media_ref.asset_id)
        if relative_path is None or (
            content is not None and len(content) > MAX_SNAPSHOT_FILE_BYTES
        ):
            return None
        media_refs.append(
            {
                "asset_id": str(media_ref.asset_id),
                "relative_path": relative_path,
                "expected_sha256": media_ref.expected_sha256,
                "expected_byte_size": media_ref.expected_byte_size,
                "status": "available" if content is not None else "missing",
            }
        )
        if content is None:
            continue
        digest = hashlib.sha256(content).hexdigest()
        raw_size += len(content)
        media.append(
            {
                "asset_id": str(media_ref.asset_id),
                "relative_path": relative_path,
                "expected_sha256": media_ref.expected_sha256,
                "sha256": digest,
                "byte_size": len(content),
                "content_base64": base64.b64encode(content).decode("ascii"),
            }
        )

    if (
        raw_size + len(render_source.render_data_bytes) > MAX_SNAPSHOT_CONTENT_BYTES
        or len(render_source.render_data_bytes) > MAX_RENDER_DATA_BYTES
    ):
        return None
    payload: dict[str, Any] = {
        "schema_version": SNAPSHOT_SCHEMA,
        "render_id": render_id,
        "preview_input_hash": preview_input_hash,
        "pdf_sha256": pdf_sha256,
        "entrypoint_relative_path": render_source.entrypoint_relative_path,
        "render_data_relative_path": render_source.render_data_relative_path,
        "template_version": template_bundle.template_version,
        "template_sha256": template_bundle.sha256,
        "source_sha256": source_sha256,
        "render_data_sha256": render_data_sha256,
        "render_data_byte_size": len(render_source.render_data_bytes),
        "render_data_base64": base64.b64encode(render_source.render_data_bytes).decode("ascii"),
        "files": files,
        "media": media,
        "media_refs": media_refs,
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    if len(encoded) > MAX_SNAPSHOT_BYTES:
        return None
    return encoded


def decode_typst_input_snapshot(content: bytes) -> dict[str, Any] | None:
    """Decode and verify an input snapshot. Raw legacy entrypoint blobs return None."""
    try:
        payload = json.loads(content)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema_version") != SNAPSHOT_SCHEMA:
        return None
    if len(content) > MAX_SNAPSHOT_BYTES:
        raise ValueError("snapshot_size_limit_exceeded")
    entrypoint = safe_bundle_path(payload.get("entrypoint_relative_path"))
    if entrypoint is None:
        raise ValueError("snapshot_entrypoint_invalid")
    files = payload.get("files")
    media = payload.get("media")
    media_refs = payload.get("media_refs", [])
    if (
        not isinstance(files, list)
        or not isinstance(media, list)
        or not isinstance(media_refs, list)
    ):
        raise ValueError("snapshot_inventory_invalid")
    seen_paths: set[str] = set()
    decoded_files: list[dict[str, Any]] = []
    template_contents: list[tuple[str, bytes]] = []
    for item in files:
        decoded = _decode_file(item)
        path = decoded["relative_path"]
        if path in seen_paths:
            raise ValueError("snapshot_duplicate_path")
        seen_paths.add(path)
        decoded_files.append(decoded)
        template_contents.append((path, decoded["content"]))
    if entrypoint not in seen_paths:
        raise ValueError("snapshot_entrypoint_missing")
    expected_template_sha256 = payload.get("template_sha256")
    if (
        not isinstance(expected_template_sha256, str)
        or hash_bundle_contents(template_contents) != expected_template_sha256
    ):
        raise ValueError("snapshot_template_hash_mismatch")
    decoded_media = []
    media_paths: set[str] = set()
    for item in media:
        if not isinstance(item, dict):
            raise ValueError("snapshot_media_invalid")
        decoded = _decode_file(item, path_key="relative_path")
        path = decoded["relative_path"]
        if path in seen_paths or path in media_paths:
            raise ValueError("snapshot_duplicate_path")
        media_paths.add(path)
        decoded_media.append(decoded)
    for item in media_refs:
        if (
            not isinstance(item, dict)
            or safe_bundle_path(item.get("relative_path")) is None
            or item.get("status") not in {"available", "missing"}
        ):
            raise ValueError("snapshot_media_reference_invalid")
        if item["status"] == "available" and item.get("relative_path") not in media_paths:
            raise ValueError("snapshot_media_reference_unavailable")
        if item["status"] == "missing" and item.get("relative_path") in media_paths:
            raise ValueError("snapshot_media_reference_conflict")
        if item["status"] == "available":
            observed = next(
                media_item
                for media_item in decoded_media
                if media_item["relative_path"] == item["relative_path"]
            )
            expected_hash = item.get("expected_sha256")
            expected_size = item.get("expected_byte_size")
            if expected_hash is not None and expected_hash != observed["sha256"]:
                raise ValueError("snapshot_media_hash_conflict")
            if expected_size is not None and expected_size != observed["byte_size"]:
                raise ValueError("snapshot_media_size_conflict")
    entrypoint_file = next(item for item in decoded_files if item["relative_path"] == entrypoint)
    if entrypoint_file["sha256"] != payload.get("source_sha256"):
        raise ValueError("snapshot_source_hash_mismatch")
    data_base64 = payload.get("render_data_base64")
    data_sha256 = payload.get("render_data_sha256")
    data_size = payload.get("render_data_byte_size")
    if (
        not isinstance(data_base64, str)
        or not isinstance(data_sha256, str)
        or not isinstance(data_size, int)
        or isinstance(data_size, bool)
        or data_size < 0
        or data_size > MAX_RENDER_DATA_BYTES
    ):
        raise ValueError("snapshot_render_data_invalid")
    try:
        render_data = base64.b64decode(data_base64, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("snapshot_render_data_encoding_invalid") from exc
    if len(render_data) != data_size or hashlib.sha256(render_data).hexdigest() != data_sha256:
        raise ValueError("snapshot_render_data_hash_mismatch")
    payload["render_data_content"] = render_data
    payload["files"] = decoded_files
    payload["media"] = decoded_media
    return payload


def _decode_file(item: object, *, path_key: str = "relative_path") -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError("snapshot_file_invalid")
    path = safe_bundle_path(item.get(path_key))
    encoded = item.get("content_base64")
    declared_hash = item.get("sha256")
    declared_size = item.get("byte_size")
    if (
        path is None
        or not isinstance(encoded, str)
        or not isinstance(declared_hash, str)
        or len(declared_hash) != 64
        or not isinstance(declared_size, int)
        or isinstance(declared_size, bool)
        or declared_size < 0
        or declared_size > MAX_SNAPSHOT_FILE_BYTES
    ):
        raise ValueError("snapshot_file_invalid")
    try:
        data = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("snapshot_file_encoding_invalid") from exc
    digest = hashlib.sha256(data).hexdigest()
    if len(data) != declared_size or digest != declared_hash:
        raise ValueError("snapshot_file_hash_mismatch")
    return {
        **item,
        "relative_path": path,
        "sha256": digest,
        "byte_size": len(data),
        "content": data,
    }
