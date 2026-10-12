"""Read-only inspection of the immutable inputs belonging to a Typst render."""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.pdf_inspection import PdfInspectionService, PdfRenderReference
from cti_app.application.typst_input_snapshot import (
    MAX_RENDER_DATA_BYTES,
    MAX_SNAPSHOT_BYTES,
    SNAPSHOT_SCHEMA,
    decode_typst_input_snapshot,
    preview_snapshot_cache_key,
)
from cti_app.domain.typst_input_inspection import (
    RenderInputDiagnosticCategory,
    RenderInputInspectionAvailability,
    RenderInputInspectionErrorCode,
)

MAX_FILE_LINES = 200
MAX_FILE_RESPONSE_BYTES = 512 * 1024
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class RenderInputInspectionError(Exception):
    def __init__(self, code: RenderInputInspectionErrorCode) -> None:
        super().__init__(code.value)
        self.code = code


class RenderInputInspectionRepository(Protocol):
    async def find_by_render_id(self, render_id: UUID) -> PdfRenderReference | None: ...


class RenderInputBlobReader(Protocol):
    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...


class RenderInputDerivedCache(Protocol):
    def get_derived(self, key: str) -> bytes | None: ...


@dataclass(slots=True)
class _LoadedInputs:
    render: PdfRenderReference | None
    inspection_id: str
    snapshot: dict[str, Any] | None
    render_data_bytes: bytes | None
    render_data: Any
    render_data_error: str | None
    source_status: RenderInputInspectionAvailability
    source_sha256: str | None
    render_data_sha256: str | None


class RenderInputInspectionService:
    """Resolve input snapshots from render blobs or the existing bounded preview cache."""

    def __init__(
        self,
        repository: RenderInputInspectionRepository,
        blob_reader: RenderInputBlobReader,
        pdf_inspection: PdfInspectionService,
        derived_cache: RenderInputDerivedCache,
    ) -> None:
        self._repository = repository
        self._blob_reader = blob_reader
        self._pdf_inspection = pdf_inspection
        self._derived_cache = derived_cache

    async def metadata(self, render_id: str) -> dict[str, Any]:
        loaded = await self._load_persisted(render_id)
        if loaded.render is None:
            return await self._preview_metadata(loaded)
        return await self._metadata_for(loaded)

    async def preview_metadata(self, inspection_id: str) -> dict[str, Any]:
        _require_sha256(inspection_id)
        return await self._preview_metadata(await self._load_preview(inspection_id))

    async def files(self, render_id: str, *, preview: bool = False) -> dict[str, Any]:
        loaded = await (
            self._load_preview(render_id) if preview else self._load_persisted(render_id)
        )
        files = self._verified_files(loaded)
        return {
            "schema_version": "render-input-files-v1",
            "render_id": loaded.inspection_id,
            "availability": loaded.source_status,
            "files": [self._file_view(loaded.inspection_id, item) for item in files],
        }

    async def file(
        self,
        render_id: str,
        file_id: str,
        *,
        start_line: int = 1,
        line_count: int = 100,
        preview: bool = False,
    ) -> dict[str, Any]:
        if start_line < 1 or line_count < 1 or line_count > MAX_FILE_LINES:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.SIZE_LIMIT_EXCEEDED)
        loaded = await (
            self._load_preview(render_id) if preview else self._load_persisted(render_id)
        )
        files = self._verified_files(loaded)
        selected = next(
            (item for item in files if self._file_id(loaded.inspection_id, item) == file_id),
            None,
        )
        if selected is None:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.FILE_NOT_IN_BUNDLE)
        try:
            text = selected["content"].decode("utf-8")
        except UnicodeDecodeError as exc:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
            ) from exc
        lines = text.splitlines(keepends=True)
        if start_line > max(1, len(lines)):
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.INVALID_SELECTOR)
        selected_lines = lines[start_line - 1 : start_line - 1 + line_count]
        result = "".join(selected_lines)
        if len(result.encode("utf-8")) > MAX_FILE_RESPONSE_BYTES:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.SIZE_LIMIT_EXCEEDED)
        metadata = self._file_view(loaded.inspection_id, selected)
        return {
            "schema_version": "render-input-file-v1",
            "render_id": loaded.inspection_id,
            **metadata,
            "start_line": start_line,
            "end_line": start_line + max(0, len(selected_lines) - 1),
            "content": result,
        }

    async def render_data(
        self,
        render_id: str,
        *,
        subject_id: UUID | None = None,
        block_key: str | None = None,
        pointer: str | None = None,
        preview: bool = False,
    ) -> dict[str, Any]:
        loaded = await (
            self._load_preview(render_id) if preview else self._load_persisted(render_id)
        )
        if loaded.render_data_bytes is None:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
            )
        if pointer is not None and (subject_id is not None or block_key is not None):
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.INVALID_SELECTOR)
        response: dict[str, Any] = {
            "schema_version": "render-data-inspection-v1",
            "render_id": loaded.inspection_id,
            "sha256": loaded.render_data_sha256,
            "schema": loaded.render_data.get("schema_version")
            if isinstance(loaded.render_data, dict)
            else None,
        }
        if loaded.render_data_error is not None:
            response["availability"] = RenderInputInspectionAvailability.PARTIAL
            response["json"] = None
            response["error"] = "render_data_invalid_json"
            return response
        response["availability"] = RenderInputInspectionAvailability.READY
        if pointer is not None:
            try:
                response["selection"] = {
                    "pointer": pointer,
                    "value": _resolve_json_pointer(loaded.render_data, pointer),
                }
            except (KeyError, IndexError, ValueError, TypeError) as exc:
                raise RenderInputInspectionError(
                    RenderInputInspectionErrorCode.INVALID_SELECTOR
                ) from exc
        elif subject_id is not None or block_key is not None:
            response["matches"] = _select_render_data(
                loaded.render_data,
                subject_id=str(subject_id) if subject_id is not None else None,
                block_key=block_key,
                identity_subject_id=(
                    loaded.render.source.get("subject_id") if loaded.render is not None else None
                ),
            )
        else:
            response["json"] = loaded.render_data
        return response

    async def diagnostics(self, render_id: str, *, preview: bool = False) -> dict[str, Any]:
        loaded = await (
            self._load_preview(render_id) if preview else self._load_persisted(render_id)
        )
        diagnostics = _schema_diagnostics(loaded.render_data, loaded.render)
        if loaded.source_status is RenderInputInspectionAvailability.UNAVAILABLE:
            diagnostics.append(
                _diagnostic(
                    "input_snapshot_unavailable",
                    RenderInputDiagnosticCategory.TYPST_SOURCE,
                    "Aucun snapshot exact des entrées Typst n'est disponible pour ce rendu.",
                    "template_code",
                )
            )
        elif loaded.source_status is RenderInputInspectionAvailability.PARTIAL:
            diagnostics.append(
                _diagnostic(
                    "template_bundle_snapshot_missing",
                    RenderInputDiagnosticCategory.TYPST_SOURCE,
                    "Le blob historique contient l'entrypoint, mais pas le bundle complet.",
                    "template_code",
                )
            )
        if loaded.snapshot is not None:
            expected_data_hash = loaded.snapshot.get("render_data_sha256")
            if loaded.render_data_sha256 and expected_data_hash != loaded.render_data_sha256:
                diagnostics.append(
                    _diagnostic(
                        "render_data_hash_mismatch",
                        RenderInputDiagnosticCategory.CANONICAL_DATA,
                        "Le hash du JSON effectif diffère de celui du snapshot Typst.",
                        "content_revision",
                    )
                )
            for media_ref in loaded.snapshot.get("media_refs", []):
                if media_ref.get("status") == "missing":
                    diagnostics.append(
                        _diagnostic(
                            "asset_missing",
                            RenderInputDiagnosticCategory.CANONICAL_DATA,
                            "Un média requis par le rendu est absent du snapshot effectif.",
                            "enrichment_revision",
                            location={
                                "asset_id": media_ref.get("asset_id"),
                                "path": media_ref.get("relative_path"),
                            },
                        )
                    )
            diagnostics.extend(_reference_diagnostics(loaded.render_data))
            diagnostics.extend(_unmapped_block_diagnostics(loaded.render_data))
            diagnostics.extend(_manifest_identity_diagnostics(loaded.render_data, loaded.render))
        if loaded.render is not None and loaded.render.error_code is not None:
            error_code = loaded.render.error_code
            is_template_error = "template" in error_code or "manifest" in error_code
            category = (
                RenderInputDiagnosticCategory.TYPST_SOURCE
                if is_template_error
                else RenderInputDiagnosticCategory.COMPILER
            )
            location = _compiler_location(loaded.render.error_message, loaded.snapshot)
            diagnostics.append(
                _diagnostic(
                    error_code,
                    category,
                    "Le rendu Typst a échoué; aucun nouveau rendu n'a été lancé par l'inspection.",
                    "template_code" if is_template_error else "engine_diagnostic",
                    location=location,
                )
            )

        pdf = await self._pdf_summary(loaded)
        for item in pdf.get("visual_diagnostics", []):
            diagnostics.append(
                _diagnostic(
                    item.get("code", "pdf_diagnostic"),
                    RenderInputDiagnosticCategory.LAYOUT_DEPENDENT,
                    "Signal visuel calculé depuis le PDF existant; "
                    "la mise en page n'est pas évaluée statiquement.",
                    "pdf_review",
                    location={"page": item.get("page"), "measurement": item.get("measurement")},
                )
            )
        target_ids = _render_target_ids(loaded.render_data)
        mapped_target_ids = {
            item.get("target_id")
            for item in pdf.get("canonical_blocks", [])
            if isinstance(item, dict)
        }
        if target_ids:
            if (
                pdf.get("availability") == "unavailable"
                or pdf.get("mapping_status") == "not_available"
            ):
                diagnostics.append(
                    _diagnostic(
                        "block_locations_unavailable",
                        RenderInputDiagnosticCategory.LAYOUT_DEPENDENT,
                        "Les cibles canoniques existent, mais leur localisation PDF "
                        "n'est pas disponible.",
                        "pdf_review",
                    )
                )
            else:
                for target_id in sorted(target_ids - mapped_target_ids):
                    diagnostics.append(
                        _diagnostic(
                            "block_location_missing",
                            RenderInputDiagnosticCategory.LAYOUT_DEPENDENT,
                            "La cible canonique n'a pas de localisation Typst vérifiée "
                            "dans le PDF.",
                            "pdf_review",
                            location={"target_id": target_id},
                        )
                    )
        return {
            "schema_version": "render-input-diagnostics-v1",
            "render_id": loaded.inspection_id,
            "diagnostics": diagnostics,
            "warnings": {
                "compiler": {"availability": "not_persisted", "items": []},
                "pdf": {
                    "availability": (
                        "available" if pdf.get("availability") != "unavailable" else "unavailable"
                    ),
                    "items": [
                        item
                        for item in diagnostics
                        if item["category"] is RenderInputDiagnosticCategory.LAYOUT_DEPENDENT
                    ],
                },
            },
            "pdf": pdf,
        }

    async def _load_persisted(self, render_id: str) -> _LoadedInputs:
        try:
            parsed_id = UUID(render_id)
        except (ValueError, TypeError) as exc:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.RENDER_NOT_FOUND
            ) from exc
        render = await self._repository.find_by_render_id(parsed_id)
        if render is None:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.RENDER_NOT_FOUND)
        if render.source_blob_id is None:
            return _LoadedInputs(
                render,
                str(render.id),
                None,
                None,
                None,
                None,
                RenderInputInspectionAvailability.UNAVAILABLE,
                None,
                None,
            )
        try:
            source_bytes = await self._blob_reader.read_bytes(
                render.source_blob_id, max_bytes=MAX_SNAPSHOT_BYTES
            )
        except Exception as exc:
            raise _blob_read_error(exc) from exc
        actual_source_blob_hash = hashlib.sha256(source_bytes).hexdigest()
        try:
            snapshot = decode_typst_input_snapshot(source_bytes)
        except ValueError as exc:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH
            ) from exc
        source_status = RenderInputInspectionAvailability.READY
        if snapshot is None:
            entrypoint = (
                "RENDERER/publication.typ"
                if render.kind == "publication_render"
                else "RENDERER/edition.typ"
            )
            snapshot = {
                "schema_version": "legacy-source-v1",
                "entrypoint_relative_path": entrypoint,
                "template_sha256": render.template_sha256,
                "template_version": render.template_version,
                "source_sha256": actual_source_blob_hash,
                "render_data_sha256": None,
                "files": [
                    {
                        "relative_path": entrypoint,
                        "kind": "template",
                        "sha256": actual_source_blob_hash,
                        "byte_size": len(source_bytes),
                        "content": source_bytes,
                    }
                ],
                "media": [],
                "media_refs": [],
            }
            source_status = RenderInputInspectionAvailability.PARTIAL
        if snapshot.get("render_id") not in {None, str(render.id)}:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH)
        if (
            snapshot.get("template_sha256")
            and snapshot.get("schema_version") == SNAPSHOT_SCHEMA
            and snapshot["template_sha256"] != render.template_sha256
        ):
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH)
        (
            render_data_bytes,
            render_data,
            data_error,
            data_hash,
        ) = await self._read_persisted_render_data(render, snapshot)
        return _LoadedInputs(
            render,
            str(render.id),
            snapshot,
            render_data_bytes,
            render_data,
            data_error,
            source_status,
            snapshot.get("source_sha256"),
            data_hash,
        )

    async def _read_persisted_render_data(
        self, render: PdfRenderReference, snapshot: dict[str, Any]
    ) -> tuple[bytes | None, Any, str | None, str | None]:
        if render.render_data_blob_id is None:
            return None, None, "render_data_unavailable", None
        try:
            content = await self._blob_reader.read_bytes(
                render.render_data_blob_id, max_bytes=MAX_RENDER_DATA_BYTES
            )
        except Exception as exc:
            raise _blob_read_error(exc) from exc
        digest = hashlib.sha256(content).hexdigest()
        expected = snapshot.get("render_data_sha256")
        if expected is not None and expected != digest:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH)
        try:
            value = json.loads(content)
        except (UnicodeDecodeError, json.JSONDecodeError):
            return content, None, "render_data_invalid_json", digest
        return content, value, None, digest

    async def _load_preview(self, inspection_id: str) -> _LoadedInputs:
        _require_sha256(inspection_id)
        try:
            encoded = self._derived_cache.get_derived(preview_snapshot_cache_key(inspection_id))
        except Exception as exc:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
            ) from exc
        if encoded is None:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
            )
        try:
            snapshot = decode_typst_input_snapshot(encoded)
        except ValueError as exc:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH
            ) from exc
        if (
            snapshot is None
            or snapshot.get("preview_input_hash") is None
            or snapshot.get("pdf_sha256") is None
        ):
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
            )
        if snapshot.get("render_id") != inspection_id:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH)
        data_bytes = snapshot["render_data_content"]
        data_error: str | None = None
        try:
            data = json.loads(data_bytes)
        except (UnicodeDecodeError, json.JSONDecodeError):
            data = None
            data_error = "render_data_invalid_json"
        return _LoadedInputs(
            None,
            inspection_id,
            snapshot,
            data_bytes,
            data,
            data_error,
            RenderInputInspectionAvailability.READY,
            snapshot.get("source_sha256"),
            snapshot.get("render_data_sha256"),
        )

    async def _preview_metadata(self, loaded: _LoadedInputs) -> dict[str, Any]:
        return await self._metadata_for(loaded)

    async def _metadata_for(self, loaded: _LoadedInputs) -> dict[str, Any]:
        render = loaded.render
        files = self._verified_files(loaded) if loaded.snapshot is not None else []
        template_sha = (
            render.template_sha256
            if render is not None
            else loaded.snapshot.get("template_sha256")
            if loaded.snapshot
            else None
        )
        template_version = (
            render.template_version
            if render is not None
            else loaded.snapshot.get("template_version")
            if loaded.snapshot
            else None
        )
        template_actual = None
        if loaded.snapshot is not None and loaded.snapshot.get("schema_version") == SNAPSHOT_SCHEMA:
            template_actual = loaded.snapshot.get("template_sha256")
        pdf = await self._pdf_summary(loaded)
        return {
            "schema_version": "render-input-inspection-v1",
            "render": {
                "id": loaded.inspection_id,
                "type": render.kind if render is not None else "edition_preview",
                "status": render.status if render is not None else "succeeded",
                "identity": render.source
                if render is not None
                else {
                    "kind": "edition_preview",
                    "preview_input_hash": loaded.snapshot.get("preview_input_hash")
                    if loaded.snapshot
                    else None,
                },
                "input_hash": render.input_hash
                if render is not None
                else loaded.snapshot.get("preview_input_hash")
                if loaded.snapshot
                else None,
                "error_code": render.error_code if render is not None else None,
            },
            "template": {
                "version": template_version,
                "sha256": template_sha,
                "snapshot_sha256": template_actual,
                "verified": template_actual == template_sha
                if template_actual is not None
                else False,
            },
            "compiler": {
                "name": render.compiler if render is not None else "typst",
                "version": render.compiler_version if render is not None else None,
                "renderer": render.renderer if render is not None else "edition_typst_renderer",
                "renderer_version": render.renderer_version if render is not None else None,
                "font_bundle_version": render.font_bundle_version if render is not None else None,
            },
            "source": {
                "availability": loaded.source_status,
                "entrypoint": next(
                    (
                        self._file_view(loaded.inspection_id, item)
                        for item in files
                        if item["relative_path"]
                        == (loaded.snapshot or {}).get("entrypoint_relative_path")
                    ),
                    None,
                ),
                "files_href": f"/api/render-inspections/{loaded.inspection_id}/files"
                if render is not None
                else f"/api/render-inspections/previews/{loaded.inspection_id}/files",
            },
            "render_data": {
                "availability": (
                    RenderInputInspectionAvailability.UNAVAILABLE
                    if loaded.render_data_bytes is None
                    else RenderInputInspectionAvailability.PARTIAL
                    if loaded.render_data_error
                    else RenderInputInspectionAvailability.READY
                ),
                "schema_version": loaded.render_data.get("schema_version")
                if isinstance(loaded.render_data, dict)
                else None,
                "sha256": loaded.render_data_sha256,
                "href": f"/api/render-inspections/{loaded.inspection_id}/render-data"
                if render is not None
                else f"/api/render-inspections/previews/{loaded.inspection_id}/render-data",
            },
            "hashes": {
                "template_sha256": template_sha,
                "source_sha256": loaded.source_sha256,
                "render_data_sha256": loaded.render_data_sha256,
                "media": [
                    {
                        key: media.get(key)
                        for key in (
                            "asset_id",
                            "relative_path",
                            "expected_sha256",
                            "sha256",
                            "byte_size",
                        )
                    }
                    for media in (loaded.snapshot or {}).get("media", [])
                ],
                "pdf_sha256": pdf.get("sha256"),
            },
            "origin": render.source
            if render is not None
            else loaded.snapshot.get("preview_input_hash")
            if loaded.snapshot
            else None,
            "pdf": pdf,
        }

    async def _pdf_summary(self, loaded: _LoadedInputs) -> dict[str, Any]:
        render = loaded.render
        pdf_sha256 = (
            render.output_sha256
            if render is not None
            else (loaded.snapshot or {}).get("pdf_sha256")
        )
        if not isinstance(pdf_sha256, str) or _SHA256.fullmatch(pdf_sha256) is None:
            return {
                "availability": "unavailable",
                "sha256": None,
                "page_count": None,
                "href": None,
                "visual_diagnostics_href": None,
                "canonical_blocks": [],
            }
        href = f"/api/pdf-inspections/{pdf_sha256}"
        try:
            pdf_metadata = await self._pdf_inspection.metadata(pdf_sha256)
        except Exception:
            return {
                "availability": "unavailable",
                "sha256": pdf_sha256,
                "page_count": None,
                "href": href,
                "visual_diagnostics_href": href,
                "canonical_blocks": [],
            }
        mapping = pdf_metadata.get("mapping", {})
        raw_items = mapping.get("items", []) if mapping.get("status") == "partial" else []
        expected_target_ids = _render_target_ids(loaded.render_data)
        items = [
            item
            for item in raw_items
            if isinstance(item, dict) and item.get("target_id") in expected_target_ids
        ]
        return {
            "availability": pdf_metadata.get("availability", "ready"),
            "sha256": pdf_sha256,
            "page_count": pdf_metadata.get("pdf", {}).get("page_count"),
            "href": href,
            "visual_diagnostics_href": href,
            "mapping_status": getattr(mapping.get("status"), "value", mapping.get("status")),
            "page_png_url_template": pdf_metadata.get("raster", {}).get("page_png_url_template"),
            "visual_diagnostics": pdf_metadata.get("diagnostics", {}).get("items", []),
            "canonical_blocks": items,
        }

    def _verified_files(self, loaded: _LoadedInputs) -> list[dict[str, Any]]:
        if loaded.snapshot is None:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
            )
        if loaded.source_status is RenderInputInspectionAvailability.UNAVAILABLE:
            raise RenderInputInspectionError(
                RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
            )
        files = loaded.snapshot.get("files")
        if not isinstance(files, list) or any(not isinstance(item, dict) for item in files):
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH)
        if len(files) > 500:
            raise RenderInputInspectionError(RenderInputInspectionErrorCode.SIZE_LIMIT_EXCEEDED)
        return files

    @staticmethod
    def _file_id(render_id: str, item: Mapping[str, Any]) -> str:
        payload = f"{render_id}\x00{item['relative_path']}\x00{item['sha256']}".encode()
        return hashlib.sha256(payload).hexdigest()[:32]

    def _file_view(self, render_id: str, item: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "file_id": self._file_id(render_id, item),
            "path": item["relative_path"],
            "sha256": item["sha256"],
            "byte_size": item["byte_size"],
            "kind": item.get("kind", "template"),
        }


def _require_sha256(value: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise RenderInputInspectionError(RenderInputInspectionErrorCode.RENDER_NOT_FOUND)


def _blob_read_error(error: Exception) -> RenderInputInspectionError:
    text = type(error).__name__.lower()
    code = (
        RenderInputInspectionErrorCode.INPUT_HASH_MISMATCH
        if "integrity" in text or "hash" in text
        else RenderInputInspectionErrorCode.INPUT_SNAPSHOT_UNAVAILABLE
    )
    return RenderInputInspectionError(code)


def _diagnostic(
    code: str,
    category: RenderInputDiagnosticCategory,
    message: str,
    channel: str,
    *,
    location: dict[str, Any] | None = None,
) -> dict[str, Any]:
    routes = {
        "content_revision": {"route": "targeted_revision", "requires_human_decision": True},
        "enrichment_revision": {
            "route": "targeted_enrichment_revision",
            "requires_human_decision": True,
        },
        "composition": {"route": "edition_composition", "requires_human_decision": True},
        "template_code": {"route": "versioned_template_change", "requires_human_decision": True},
        "engine_diagnostic": {"route": "technical_diagnostic", "requires_human_decision": False},
        "pdf_review": {"route": "pdf_inspection", "requires_human_decision": True},
    }
    return {
        "code": code,
        "category": category,
        "severity": "error"
        if category is not RenderInputDiagnosticCategory.LAYOUT_DEPENDENT
        else "warning",
        "message": message,
        "location": location,
        "suggested_channel": {"id": channel, **routes[channel]},
    }


def _schema_diagnostics(data: Any, render: PdfRenderReference | None) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return [
            _diagnostic(
                "render_data_invalid",
                RenderInputDiagnosticCategory.CANONICAL_DATA,
                "Le JSON de rendu n'est pas un objet.",
                "content_revision",
            )
        ]
    edition = render is not None and render.kind == "edition_render"
    required = (
        ("schema_version", "edition", "publications")
        if edition
        else ("schema_version", "language", "title", "content_sections")
    )
    result = []
    for field in required:
        if field not in data:
            result.append(
                _diagnostic(
                    "render_data_required_field_missing",
                    RenderInputDiagnosticCategory.CANONICAL_DATA,
                    f"Le champ requis {field!r} est absent du JSON effectif.",
                    "composition" if field in {"edition", "publications"} else "content_revision",
                    location={"json_pointer": f"/{field}"},
                )
            )
    if "schema_version" in data and not isinstance(data["schema_version"], str):
        result.append(
            _diagnostic(
                "render_data_schema_version_invalid",
                RenderInputDiagnosticCategory.CANONICAL_DATA,
                "La version de schéma JSON est invalide.",
                "content_revision",
                location={"json_pointer": "/schema_version"},
            )
        )
    sections = data.get("content_sections")
    if not edition and sections is not None and not isinstance(sections, list):
        result.append(
            _diagnostic(
                "render_data_content_sections_invalid",
                RenderInputDiagnosticCategory.CANONICAL_DATA,
                "content_sections doit être une liste.",
                "content_revision",
                location={"json_pointer": "/content_sections"},
            )
        )
    if edition:
        publications = data.get("publications")
        if publications is not None and not isinstance(publications, list):
            result.append(
                _diagnostic(
                    "render_data_publications_invalid",
                    RenderInputDiagnosticCategory.CANONICAL_DATA,
                    "publications doit être une liste.",
                    "composition",
                    location={"json_pointer": "/publications"},
                )
            )
    return result


def _walk(value: Any, pointer: str = "") -> Iterator[tuple[str, Any]]:
    yield pointer or "", value
    if isinstance(value, dict):
        for key, nested in value.items():
            escaped = str(key).replace("~", "~0").replace("/", "~1")
            yield from _walk(nested, f"{pointer}/{escaped}")
    elif isinstance(value, list):
        for index, nested in enumerate(value):
            yield from _walk(nested, f"{pointer}/{index}")


def _reference_diagnostics(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    source_urls = {
        source.get("url")
        for _, source in _walk(data)
        if isinstance(source, dict) and isinstance(source.get("url"), str)
    }
    result = []
    for pointer, value in _walk(data):
        if not isinstance(value, dict) or not isinstance(value.get("source_urls"), list):
            continue
        for index, url in enumerate(value["source_urls"]):
            if not isinstance(url, str) or url not in source_urls:
                result.append(
                    _diagnostic(
                        "render_data_reference_missing",
                        RenderInputDiagnosticCategory.CANONICAL_DATA,
                        "Une référence de source n'a pas de correspondance dans le JSON effectif.",
                        "content_revision",
                        location={"json_pointer": f"{pointer}/source_urls/{index}"},
                    )
                )
    return result[:100]


def _unmapped_block_diagnostics(data: Any) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    counts: dict[str, int] = {}
    for _, item in _walk(data):
        if not isinstance(item, dict) or item.get("type") not in {
            "table",
            "diagram",
            "figure",
            "chart",
            "paragraph",
            "section_heading",
        }:
            continue
        if item.get("target_id") is None:
            block_type = str(item.get("type"))
            counts[block_type] = counts.get(block_type, 0) + 1
    return [
        _diagnostic(
            "block_without_canonical_mapping",
            RenderInputDiagnosticCategory.CANONICAL_DATA,
            f"{count} bloc(s) de type {block_type!r} ne portent pas "
            "d'identité canonique instrumentée.",
            "enrichment_revision"
            if block_type in {"table", "diagram", "figure", "chart"}
            else "content_revision",
            location={"block_type": block_type, "count": count},
        )
        for block_type, count in sorted(counts.items())
    ]


def _render_target_ids(data: Any) -> set[str]:
    if not isinstance(data, dict):
        return set()
    target_ids: set[str] = set()
    for _, item in _walk(data):
        if not isinstance(item, dict):
            continue
        for key in ("target_id", "article_title_target_id"):
            target_id = item.get(key)
            if isinstance(target_id, str) and target_id:
                target_ids.add(target_id)
    return target_ids


def _manifest_identity_diagnostics(
    data: Any, render: PdfRenderReference | None
) -> list[dict[str, Any]]:
    if render is None or render.kind != "edition_render" or not isinstance(data, dict):
        return []
    entries = render.source.get("entries", [])
    publications = data.get("publications", [])
    if not isinstance(entries, list) or not isinstance(publications, list):
        return []
    manifest_subjects = {entry.get("subject_id") for entry in entries if isinstance(entry, dict)}
    render_subjects = {
        publication.get("subject_id")
        for publication in publications
        if isinstance(publication, dict)
    }
    missing = sorted(str(item) for item in manifest_subjects.symmetric_difference(render_subjects))
    if not missing:
        return []
    return [
        _diagnostic(
            "manifest_render_identity_conflict",
            RenderInputDiagnosticCategory.CANONICAL_DATA,
            "Les subject_id du manifeste ne correspondent pas aux publications du render-data.",
            "composition",
            location={"subject_ids": missing[:100]},
        )
    ]


def _compiler_location(
    message: str | None, snapshot: dict[str, Any] | None
) -> dict[str, Any] | None:
    if not message:
        return None
    paths = {
        path
        for item in (snapshot or {}).get("files", [])
        if isinstance(item, dict) and isinstance(path := item.get("relative_path"), str)
    }
    for match in re.finditer(r"(?P<path>[^\n:]+\.typ):(?P<line>\d+):(?P<column>\d+)", message):
        raw_path = match.group("path").replace("\\", "/")
        path = next(
            (
                candidate
                for candidate in paths
                if raw_path == candidate or raw_path.endswith("/" + candidate)
            ),
            None,
        )
        if path is not None:
            return {
                "file": path,
                "line": int(match.group("line")),
                "column": int(match.group("column")),
            }
    return None


def _resolve_json_pointer(value: Any, pointer: str) -> Any:
    if pointer == "":
        return value
    if not pointer.startswith("/"):
        raise ValueError("invalid JSON Pointer")
    current = value
    for encoded in pointer[1:].split("/"):
        if re.search(r"~(?![01])", encoded):
            raise ValueError("invalid JSON Pointer escape")
        token = encoded.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            current = current[token]
        elif isinstance(current, list):
            if not re.fullmatch(r"0|[1-9]\d*", token):
                raise ValueError("invalid array index")
            current = current[int(token)]
        else:
            raise KeyError(token)
    return current


def _select_render_data(
    data: Any,
    *,
    subject_id: str | None,
    block_key: str | None,
    identity_subject_id: str | None,
) -> list[dict[str, Any]]:
    if not isinstance(data, dict):
        return []
    roots: list[tuple[str, Any]] = [("", data)]
    if subject_id is not None:
        publications = data.get("publications")
        if isinstance(publications, list):
            roots = [
                (f"/publications/{index}", publication)
                for index, publication in enumerate(publications)
                if isinstance(publication, dict) and publication.get("subject_id") == subject_id
            ]
        else:
            roots = [("", data)] if identity_subject_id == subject_id else []
    matches = []
    for root, value in roots:
        if block_key is None:
            matches.append({"pointer": root, "value": value})
        else:
            for pointer, item in _walk(value, root):
                if isinstance(item, dict) and item.get("key") == block_key:
                    matches.append({"pointer": pointer, "value": item})
    return matches[:100]
