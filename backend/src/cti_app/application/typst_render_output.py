"""Verified reads of persisted Typst PDF outputs."""

from __future__ import annotations

import hashlib
from typing import Protocol
from uuid import UUID

from cti_app.application.typst_compilation import TYPST_MAX_PDF_BYTES


class TypstRenderOutputError(Exception):
    """Base class for unreadable or inconsistent persisted PDF outputs."""


class TypstRenderOutputStorageError(TypstRenderOutputError):
    """The output blob could not be read."""


class TypstRenderOutputIntegrityError(TypstRenderOutputError):
    """The output blob does not match the size and SHA-256 recorded by its render."""


class _OutputReader(Protocol):
    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...


class _RenderOutput(Protocol):
    @property
    def output_blob_id(self) -> UUID | None: ...

    @property
    def output_sha256(self) -> str | None: ...

    @property
    def output_byte_size(self) -> int | None: ...


async def read_verified_render_pdf(store: _OutputReader, render: _RenderOutput) -> bytes:
    """Return the PDF recorded by a render only if it matches the recorded size and hash."""
    if (
        render.output_blob_id is None
        or render.output_sha256 is None
        or render.output_byte_size is None
    ):
        raise TypstRenderOutputIntegrityError("Render has no recorded PDF output")
    try:
        content = await store.read_bytes(render.output_blob_id, max_bytes=TYPST_MAX_PDF_BYTES)
    except Exception as exc:
        raise TypstRenderOutputStorageError("Unable to read the rendered PDF blob") from exc
    if (
        len(content) != render.output_byte_size
        or hashlib.sha256(content).hexdigest() != render.output_sha256
    ):
        raise TypstRenderOutputIntegrityError("Rendered PDF does not match its recorded hash")
    return content


__all__ = [
    "TypstRenderOutputError",
    "TypstRenderOutputIntegrityError",
    "TypstRenderOutputStorageError",
    "read_verified_render_pdf",
]
