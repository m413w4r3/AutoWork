from __future__ import annotations

import re
import unicodedata
from datetime import date
from pathlib import PurePath

from cti_app.domain.classification import TLP

_UNSAFE_FILENAME_CHARACTERS = re.compile(r'[\\/:*?"<>|]')
_CONTROL_CHARACTERS = re.compile(r"[\x00-\x1f\x7f-\x9f]")
_MULTIPLE_SPACES = re.compile(r"\s+")
_MIME_EXTENSIONS = {
    "application/json": ".json",
    "application/pdf": ".pdf",
    "text/html": ".html",
    "text/plain": ".txt",
}


def extension_for_mime(mime_type: str) -> str:
    """Return a non-executable extension derived only from detected MIME."""
    normalized = mime_type.split(";", 1)[0].strip().casefold()
    return _MIME_EXTENSIONS.get(normalized, ".bin")


def analyst_filename(
    *,
    published_at: date | None,
    tlp: TLP,
    title: str | None,
    publisher: str | None,
    detected_mime_type: str,
    decoded_sha256: str,
    existing_names: set[str] | frozenset[str] = frozenset(),
    max_bytes: int = 240,
) -> str:
    """Build the stable, safe business filename shown to analysts."""
    extension = extension_for_mime(detected_mime_type)
    publication_date = published_at.isoformat() if published_at else "date-inconnue"
    safe_title = _safe_component(title, "titre-inconnu")
    safe_publisher = _safe_component(publisher, "publisher-inconnu")
    stem = f"{publication_date}_TLP {tlp.value}_{safe_title}_{safe_publisher}"
    filename = _fit_utf8(stem, extension, max_bytes=max_bytes)
    if filename in existing_names:
        filename = _fit_utf8(
            stem,
            f"__{decoded_sha256[:8]}{extension}",
            max_bytes=max_bytes,
        )
    return filename


def validate_logical_filename(value: str) -> str:
    """Reject path-like names even when they came from persisted metadata."""
    if not value or value in {".", ".."}:
        raise ValueError("A logical filename is required")
    if PurePath(value).name != value or "/" in value or "\\" in value or ".." in value:
        raise ValueError("Logical filename must not contain a path")
    if _CONTROL_CHARACTERS.search(value):
        raise ValueError("Logical filename contains control characters")
    return value


def ascii_download_filename(value: str) -> str:
    extension = PurePath(value).suffix or ".bin"
    stem = value[: -len(extension)] if extension else value
    ascii_stem = unicodedata.normalize("NFKD", stem).encode("ascii", "ignore").decode("ascii")
    safe = _safe_component(ascii_stem, "source")
    return _fit_utf8(safe, extension.casefold(), max_bytes=180)


def _safe_component(value: str | None, fallback: str) -> str:
    normalized = unicodedata.normalize("NFC", value or "")
    normalized = _CONTROL_CHARACTERS.sub("", normalized)
    normalized = _UNSAFE_FILENAME_CHARACTERS.sub("_", normalized)
    normalized = _MULTIPLE_SPACES.sub(" ", normalized).strip(" .")
    while ".." in normalized:
        normalized = normalized.replace("..", ".")
    normalized = normalized.strip(" .")
    return normalized or fallback


def _fit_utf8(stem: str, suffix: str, *, max_bytes: int) -> str:
    suffix_size = len(suffix.encode("utf-8"))
    if suffix_size >= max_bytes:
        raise ValueError("Filename suffix exceeds the configured byte limit")
    remaining = max_bytes - suffix_size
    raw = stem.encode("utf-8")[:remaining]
    while raw:
        try:
            fitted = raw.decode("utf-8").rstrip(" .")
            break
        except UnicodeDecodeError:
            raw = raw[:-1]
    else:
        fitted = "source"
    return f"{fitted or 'source'}{suffix}"
