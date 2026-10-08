"""Deterministic IOC CSV and legacy export from one frozen edition document."""

from __future__ import annotations

import io
import json
import re
import zipfile
from dataclasses import dataclass
from datetime import date
from typing import Any
from urllib.parse import urlsplit

from cti_app.application.edition_classification import classify_edition_publications
from cti_app.domain.edition_publication import EditionDocumentV2, EditionPublicationV2
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import CanonicalPublicationDocument

_HASH_ALGORITHMS = {
    32: "MD5",
    40: "SHA-1",
    64: "SHA-256",
}
_HASH_PREFIX = re.compile(r"^(?:md5|sha-?1|sha-?256)[:=]", re.IGNORECASE)
_METADATA_GROUP = re.compile(r"^\s*\[([^\]]+)\]\s*(.*)$", re.DOTALL)
_IOC_TYPES = ("ip", "domain", "url", "file")
_IOC_TYPE_ORDER = {value: index for index, value in enumerate(_IOC_TYPES)}


@dataclass(frozen=True, slots=True)
class IOCRow:
    is_new: int
    type: str
    value: str
    normalized_value: str


@dataclass(frozen=True, slots=True)
class ExcludedIOC:
    is_new: int
    type: str
    value: str
    reason: str

    def to_json(self) -> dict[str, Any]:
        return {
            "isNew": self.is_new,
            "type": self.type,
            "value": _safe_field(self.value),
            "reason": self.reason,
        }


@dataclass(frozen=True, slots=True)
class PublicationIOCProjection:
    rows: tuple[IOCRow, ...]
    excluded: tuple[ExcludedIOC, ...]

    @property
    def counts(self) -> dict[str, int]:
        return {ioc_type: sum(row.type == ioc_type for row in self.rows) for ioc_type in _IOC_TYPES}


@dataclass(frozen=True, slots=True)
class EditionIOCArchive:
    content: bytes
    filename: str
    edition_identifier: str
    article_count: int
    ioc_count: int


@dataclass(frozen=True, slots=True)
class _ArticleCSV:
    path: str
    metadata_line: str
    content: bytes
    projection: PublicationIOCProjection
    title: str


def refang(value: str) -> str:
    """Convert common IOC defanging forms to the clear values used downstream."""
    value = re.sub(r"hxxps(?=\s*\[:\]|://|\b)", "https", value, flags=re.IGNORECASE)
    value = re.sub(r"hxxp(?=\s*\[:\]|://|\b)", "http", value, flags=re.IGNORECASE)
    for old, new in (
        ("[.]", "."),
        ("(.)", "."),
        ("{.}", "."),
        ("[dot]", "."),
        ("(dot)", "."),
        ("{dot}", "."),
        ("[:]", ":"),
        ("[@]", "@"),
    ):
        value = re.sub(re.escape(old), new, value, flags=re.IGNORECASE)
    return value


def _safe_field(value: str) -> str:
    """Keep the pipeline's hand-parsed format unambiguous without losing data."""
    return value.replace("\r", "%0D").replace("\n", "%0A").replace(",", "%2C").replace("|", "%7C")


def _normalized_artifact_type(value: object) -> str:
    return str(getattr(value, "value", value)).strip().lower()


def _project_value(artifact_type: str, value: str) -> tuple[str, str] | tuple[None, str]:
    if not value.strip():
        return None, "empty_value"
    try:
        parsed_type = ArtifactType(artifact_type)
    except ValueError:
        return None, "unsupported_artifact_type"

    if parsed_type is ArtifactType.HASH:
        hash_value = _HASH_PREFIX.sub("", value.strip())
        algorithm = _HASH_ALGORITHMS.get(len(hash_value))
        if algorithm is None or re.fullmatch(r"[0-9a-fA-F]+", hash_value) is None:
            return None, "unsupported_hash_algorithm_or_format"
        # The legacy reporter infers MD5/SHA-1/SHA-256 from the hash length.
        return hash_value.lower(), "file"
    if parsed_type is ArtifactType.IP:
        return _safe_field(refang(value.strip())), "ip"
    if parsed_type is ArtifactType.DOMAIN:
        return _safe_field(refang(value.strip())), "domain"
    if parsed_type is ArtifactType.URL:
        return _safe_field(refang(value.strip())), "url"
    if parsed_type is ArtifactType.EMAIL:
        return None, "unsupported_type_by_ioc_reporter"
    if parsed_type is ArtifactType.CVE:
        return None, "unsupported_type_by_article_csv_pipeline"
    return None, "unsupported_type_by_article_csv_pipeline"


def project_publication_iocs(document: CanonicalPublicationDocument) -> PublicationIOCProjection:
    """Map frozen V4/V5 publication indicator groups into supported CSV rows."""
    candidates: dict[tuple[int, str, str], IOCRow] = {}
    excluded: dict[tuple[int, str, str, str], ExcludedIOC] = {}
    groups = [(0, group) for group in document.indicators]
    groups.extend((1, group) for group in getattr(document, "original_indicators", ()))

    for is_new, group in groups:
        artifact_type = _normalized_artifact_type(group.artifact_type)
        for indicator in group.indicators:
            raw_value = str(indicator.value)
            projected_value, result = _project_value(artifact_type, raw_value)
            if projected_value is None:
                item = ExcludedIOC(
                    is_new=is_new,
                    type=artifact_type,
                    value=raw_value,
                    reason=result,
                )
                excluded[(is_new, artifact_type, raw_value, result)] = item
                continue

            ioc_type = result
            normalized_raw = str(getattr(indicator, "normalized_value", raw_value))
            normalized_projected, normalized_result = _project_value(artifact_type, normalized_raw)
            if normalized_projected is None or normalized_result != ioc_type:
                normalized_projected = projected_value
            normalized_key = normalized_projected
            if ioc_type in {"ip", "domain", "file"}:
                normalized_key = normalized_key.casefold()
            row = IOCRow(
                is_new=is_new,
                type=ioc_type,
                value=projected_value,
                normalized_value=normalized_key,
            )
            key = (is_new, ioc_type, normalized_key)
            existing = candidates.get(key)
            if existing is None or (row.value.casefold(), row.value) < (
                existing.value.casefold(),
                existing.value,
            ):
                candidates[key] = row

    rows = tuple(
        sorted(
            candidates.values(),
            key=lambda row: (
                row.is_new,
                _IOC_TYPE_ORDER[row.type],
                row.normalized_value.casefold(),
                row.value,
            ),
        )
    )
    excluded_rows = tuple(
        sorted(
            excluded.values(),
            key=lambda row: (row.is_new, row.type, row.value.casefold(), row.value, row.reason),
        )
    )
    return PublicationIOCProjection(rows=rows, excluded=excluded_rows)


def _metadata(document: CanonicalPublicationDocument) -> tuple[str, str]:
    match = _METADATA_GROUP.match(document.title)
    if match is None:
        group, title = "", document.title.strip()
    else:
        group, title = match.group(1).strip(), match.group(2).strip()
    core_sources = [
        source
        for source in document.sources
        if getattr(source.tier, "value", source.tier) == "core"
        and getattr(source.kind, "value", source.kind) == "publication"
    ]
    core_sources.sort(key=lambda source: (source.canonical_url, str(source.source_document_id)))
    core_source = core_sources[0] if core_sources else None
    editor = ""
    source_url = ""
    if core_source is not None:
        editor = (core_source.publisher or "").strip()
        if not editor:
            editor = (urlsplit(core_source.canonical_url).hostname or "").removeprefix("www.")
        source_url = core_source.canonical_url
    line = "[" + "|".join(_safe_field(value) for value in (group, title, editor, source_url)) + "]"
    return line, title


def _article_csv(
    publication: EditionPublicationV2,
    number: str,
    kind: str,
) -> _ArticleCSV:
    document = publication.document
    metadata_line, title = _metadata(document)
    projection = project_publication_iocs(document)
    lines = [metadata_line, "isNew,type,value"]
    lines.extend(f"{row.is_new},{row.type},{row.value}" for row in projection.rows)
    content = ("\n".join(lines) + "\n").encode("utf-8")
    folder = "articles" if kind == "article" else "breves"
    stem = "article" if kind == "article" else "breve"
    path = f"{folder}/{stem}{number}/article.csv"
    return _ArticleCSV(
        path=path,
        metadata_line=metadata_line,
        content=content,
        projection=projection,
        title=title,
    )


def edition_identifier(edition: dict[str, Any]) -> str:
    country_code = str(edition.get("country_code") or "").strip()
    period_start = edition.get("period_start")
    suffix = ""
    if isinstance(period_start, str):
        try:
            parsed = date.fromisoformat(period_start)
        except ValueError:
            parsed = None
        if parsed is not None:
            suffix = f"{parsed.year % 100:02d}{parsed.month:02d}"
    identifier = country_code + suffix
    if not identifier:
        identifier = str(edition.get("id") or "edition")[:12]
    return re.sub(r"[^A-Za-z0-9_-]", "_", identifier)


def _add_zip_file(archive: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_STORED
    info.create_system = 3
    info.external_attr = 0o100644 << 16
    info.extra = b""
    info.comment = b""
    archive.writestr(info, content)


def build_edition_ioc_archive(
    document: EditionDocumentV2,
    *,
    edition_sha256: str,
) -> EditionIOCArchive:
    """Build a byte-reproducible ZIP from the canonical, frozen edition blob."""
    if re.fullmatch(r"[0-9a-f]{64}", edition_sha256) is None:
        raise ValueError("edition_sha256 must be lowercase SHA-256")
    classified = classify_edition_publications(document.publications)
    articles = tuple(
        _article_csv(
            item.publication,
            item.number,
            item.kind,
        )
        for item in classified
    )
    identifier = edition_identifier(document.edition)
    file_names = {
        "legacy": f"legacy_iocs_{identifier}.txt",
        "manifest": "manifest.json",
    }
    legacy_lines: list[str] = []
    article_manifests: list[dict[str, Any]] = []
    totals = {ioc_type: 0 for ioc_type in _IOC_TYPES}
    for article, item in zip(articles, classified, strict=True):
        legacy_lines.append(article.metadata_line)
        legacy_lines.extend(row.value for row in article.projection.rows)
        legacy_lines.append("")
        counts = article.projection.counts
        for ioc_type, count in counts.items():
            totals[ioc_type] += count
        article_manifests.append(
            {
                "position": item.publication.position,
                "subject_id": str(item.publication.subject_id),
                "number": item.number,
                "kind": item.kind,
                "file": article.path,
                "title": _safe_field(article.title),
                "counts": counts,
                "excluded": [item.to_json() for item in article.projection.excluded],
            }
        )
    legacy = ("\n".join(legacy_lines) + ("\n" if legacy_lines else "")).encode("utf-8")
    manifest = {
        "schema_version": "1",
        "edition_id": str(document.edition.get("id") or ""),
        "edition_sha256": edition_sha256,
        "edition_identifier": identifier,
        "publications": article_manifests,
        "totals": totals,
    }
    manifest_bytes = json.dumps(
        manifest,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")

    output = io.BytesIO()
    with zipfile.ZipFile(output, mode="w", compression=zipfile.ZIP_STORED) as archive:
        for article in articles:
            _add_zip_file(archive, article.path, article.content)
        _add_zip_file(archive, file_names["legacy"], legacy)
        _add_zip_file(archive, file_names["manifest"], manifest_bytes)
    return EditionIOCArchive(
        content=output.getvalue(),
        filename=f"iocs_{identifier}.zip",
        edition_identifier=identifier,
        article_count=len(articles),
        ioc_count=sum(totals.values()),
    )


__all__ = [
    "EditionIOCArchive",
    "ExcludedIOC",
    "IOCRow",
    "PublicationIOCProjection",
    "build_edition_ioc_archive",
    "edition_identifier",
    "project_publication_iocs",
    "refang",
]
