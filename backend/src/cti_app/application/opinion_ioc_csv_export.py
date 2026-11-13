"""Reproducible IOC CSV exports from a publication document and preview projection."""

from __future__ import annotations

import argparse
import csv
import io
import json
import re
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit
from uuid import UUID

from cti_app.application.edition_ioc_export import (
    _HASH_ALGORITHMS,
    IOCRow,
    _metadata,
    _normalized_artifact_type,
    _project_value,
    project_publication_iocs,
    refang,
)
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    publication_document_from_json,
)

_PIPELINE_TYPES = ("ip", "domain", "url", "file")
_PIPELINE_TYPE_ORDER = {value: index for index, value in enumerate(_PIPELINE_TYPES)}
_HEX_HASH = re.compile(r"^[0-9a-fA-F]+$")
_UUID_MARKER = re.compile(
    r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"
)
_INTERNAL_MARKER = re.compile(r"(?i)\b(?:bridge|workbench|enquête|investigation|aw_api)\b")
_NETWORK_TYPES = {"ip", "domain", "url", "email"}
_FILE_TYPES = {"hash", "file", "filename", "filepath"}

_TYPE_LABELS = {
    "ip": "Adresse IP",
    "domain": "Domaine",
    "url": "URL",
    "email": "Adresse e-mail",
    "hash": "Empreinte de fichier",
    "file": "Empreinte de fichier",
    "filename": "Nom de fichier",
    "filepath": "Chemin de fichier",
}
_EXCLUSION_LABELS = {
    "empty_value": "Valeur vide",
    "internal_uuid": "Identifiant interne masqué",
    "unsupported_artifact_type": "Type de valeur non reconnu",
    "unsupported_hash_algorithm_or_format": "Format d'empreinte non pris en charge",
    "unsupported_type_by_ioc_reporter": "Type non pris en charge par l'analyseur CSV",
    "unsupported_type_by_article_csv_pipeline": "Type non pris en charge par le pipeline CSV",
    "legitimate_service": "Service légitime écarté par la projection",
}
_READ_ME = (
    "# Livraison des indicateurs\n\n"
    "- `ioc_pipeline_article.csv` suit le format article : métadonnées entre crochets, puis "
    "`isNew,type,value`. Copiez-le dans le dossier de l'article sous le nom `article.csv` "
    "pour l'analyseur `ioc_reporter_typst.py`. Les lignes `isNew=1` alimentent les notes "
    "de l'analyste.\n"
    "- `ioc_fichiers.csv` regroupe les empreintes de fichiers et leur nom ou chemin associé "
    "lorsqu'il est fourni.\n"
    "- `ioc_reseau.csv` regroupe les domaines, adresses IP, URL et adresses e-mail.\n"
    "- `noms_et_chemins_de_fichiers.csv` liste les noms et chemins sans les traiter comme des "
    "empreintes.\n"
    "- `pistes_non_confirmees.csv` contient les valeurs restant à vérifier.\n"
    "- `ioc_exclus.csv` donne chaque valeur absente du fichier de l'article et le motif "
    "correspondant.\n\n"
    "Les fichiers sont encodés en UTF-8, avec virgule comme séparateur. Renommez "
    "`ioc_pipeline_article.csv` en `article.csv` pour le consommer par `ioc_reporter_typst.py`.\n"
    "L'export prend un JSON local réunissant l'identité de l'avis, le document canonique et "
    "les sections rendues de son aperçu; il ne contacte aucun service.\n"
)


@dataclass(frozen=True, slots=True)
class _IOCRecord:
    artifact_type: str
    value: str
    status: str
    source: str
    associated_file: str = ""


@dataclass(frozen=True, slots=True)
class _ExcludedRecord:
    artifact_type: str
    value: str
    reason: str


def _field(value: object, name: str, default: object = None) -> object:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _text(value: object) -> str:
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, Mapping):
        text_value = value.get("text")
        if isinstance(text_value, str):
            return text_value.strip()
        nested = value.get("value")
        return nested.strip() if isinstance(nested, str) else ""
    return ""


def _artifact_type(value: object) -> str:
    return _normalized_artifact_type(value)


def _status(value: object, default: str) -> str:
    raw = _text(value)
    normalized = raw.casefold()
    if "piste" in normalized or "candidate" in normalized or "uncertain" in normalized:
        return "Piste non confirmée"
    if "découvert" in normalized or "analyse" in normalized or "original" in normalized:
        if "étayé" in normalized:
            return "Découvert dans l'analyse — constat étayé"
        return "Découvert dans l'analyse"
    if "article" in normalized or "publication" in normalized or "documenté" in normalized:
        return "Documenté dans la publication"
    if "retenu" in normalized:
        return "Retenu dans la publication"
    if "lien non démontré" in normalized:
        return "Association à vérifier"
    return default


def _source(status: str) -> str:
    if status == "Documenté dans la publication" or status == "Retenu dans la publication":
        return "Publication"
    return "Avis de l'analyste"


def _record_priority(status: str) -> int:
    if status in {"Documenté dans la publication", "Retenu dans la publication"}:
        return 0
    if status.startswith("Découvert dans l'analyse") or status == "Association à vérifier":
        return 1
    return 2


def _record_key(record: _IOCRecord) -> tuple[str, str]:
    value = record.value if record.artifact_type == "url" else record.value.casefold()
    return record.artifact_type, value


def _associated_file(row: object) -> str:
    for field_name in (
        "associated_file",
        "associated_filename",
        "file_name",
        "filename",
        "filepath",
        "file_path",
    ):
        value = _text(_field(row, field_name))
        if (
            value
            and len(value) <= 500
            and "\n" not in value
            and "\r" not in value
            and _UUID_MARKER.search(value) is None
            and _INTERNAL_MARKER.search(value) is None
        ):
            return value
    return ""


def _add_record(records: dict[tuple[str, str], _IOCRecord], record: _IOCRecord) -> None:
    if not record.artifact_type or not record.value or _UUID_MARKER.search(record.value):
        return
    key = _record_key(record)
    current = records.get(key)
    if current is None:
        records[key] = record
        return
    candidate_key = (
        _record_priority(record.status),
        not bool(record.associated_file),
        record.value.casefold(),
        record.value,
        record.status,
        record.associated_file.casefold(),
        record.associated_file,
    )
    current_key = (
        _record_priority(current.status),
        not bool(current.associated_file),
        current.value.casefold(),
        current.value,
        current.status,
        current.associated_file.casefold(),
        current.associated_file,
    )
    selected = record if candidate_key < current_key else current
    associations = [value for value in (current.associated_file, record.associated_file) if value]
    association = (
        min(associations, key=lambda value: (value.casefold(), value)) if associations else ""
    )
    if selected != current or association != current.associated_file:
        records[key] = _IOCRecord(
            selected.artifact_type,
            selected.value,
            selected.status,
            selected.source,
            association,
        )


def _section(render_data: object, section_type: str) -> Mapping[str, Any] | None:
    sections = _field(render_data, "content_sections", ())
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        return None
    for item in sections:
        if _text(_field(item, "type")) == section_type and isinstance(item, Mapping):
            return item
    return None


def _group_items(section: Mapping[str, Any] | None, key: str) -> list[object]:
    if section is None:
        return []
    groups = section.get(key, ())
    if not isinstance(groups, Sequence) or isinstance(groups, (str, bytes)):
        return []
    items: list[object] = []
    for group in groups:
        group_items = _field(group, "items", ())
        if isinstance(group_items, Sequence) and not isinstance(group_items, (str, bytes)):
            items.extend(group_items)
    return items


def _group_record(item: object, default_status: str) -> _IOCRecord:
    artifact_type = _artifact_type(_field(item, "artifact_type", _field(item, "type", "")))
    value = refang(_text(_field(item, "value")))
    status = _status(_field(item, "indicator_status_label"), default_status)
    if artifact_type == "hash" and _HEX_HASH.fullmatch(value):
        value = value.lower()
    return _IOCRecord(
        artifact_type=artifact_type,
        value=value,
        status=status,
        source=_source(status),
        associated_file=_associated_file(item),
    )


def _readable_records(
    projection_rows: Sequence[IOCRow], render_data: object
) -> tuple[_IOCRecord, ...]:
    records: dict[tuple[str, str], _IOCRecord] = {}
    for row in projection_rows:
        artifact_type = "hash" if row.type == "file" else row.type
        status = "Découvert dans l'analyse" if row.is_new else "Documenté dans la publication"
        _add_record(records, _IOCRecord(artifact_type, row.value, status, _source(status)))

    ioc_section = _section(render_data, "ioc")
    for key, default_status in (
        ("source_indicators", "Documenté dans la publication"),
        ("original_indicators", "Découvert dans l'analyse"),
        ("candidate_indicators", "Piste non confirmée"),
    ):
        for item in _group_items(ioc_section, key):
            _add_record(records, _group_record(item, default_status))

    annex = _section(render_data, "technical_annex")
    annex_rows = _field(annex, "ioc_rows", ()) if annex is not None else ()
    if isinstance(annex_rows, Sequence) and not isinstance(annex_rows, (str, bytes)):
        for item in annex_rows:
            artifact_type = _artifact_type(_field(item, "type", _field(item, "artifact_type", "")))
            value = refang(_text(_field(item, "value")))
            status = _status(_field(item, "indicator_status_label"), "Statut non précisé")
            if artifact_type == "hash" and _HEX_HASH.fullmatch(value):
                value = value.lower()
            _add_record(
                records,
                _IOCRecord(
                    artifact_type,
                    value,
                    status,
                    _source(status),
                    _associated_file(item),
                ),
            )

    return tuple(
        sorted(
            records.values(),
            key=lambda item: (
                item.artifact_type,
                item.value.casefold(),
                item.value,
                item.status,
                item.associated_file.casefold(),
            ),
        )
    )


def _type_label(artifact_type: str) -> str:
    return _TYPE_LABELS.get(artifact_type, "Autre identifiant")


def _excluded_label(reason: str) -> str:
    return _EXCLUSION_LABELS.get(reason, "Valeur écartée par les règles du pipeline CSV")


def _safe_metadata_line(document: CanonicalPublicationDocument) -> str:
    metadata_line, _title = _metadata(document)
    fields = metadata_line[1:-1].split("|")
    if len(fields) != 4:
        return "[|||]"
    safe_fields: list[str] = []
    for index, value in enumerate(fields):
        if _UUID_MARKER.search(value) or _INTERNAL_MARKER.search(value):
            safe_fields.append("")
            continue
        if index == 3 and value:
            try:
                parsed = urlsplit(value)
            except ValueError:
                safe_fields.append("")
                continue
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                safe_fields.append("")
                continue
            if parsed.username is not None or parsed.password is not None:
                safe_fields.append("")
                continue
            value = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        safe_fields.append(value)
    return "[" + "|".join(safe_fields) + "]"


def _add_excluded(
    excluded: dict[tuple[str, str, str], _ExcludedRecord],
    artifact_type: str,
    value: str,
    reason: str,
) -> None:
    if not value:
        return
    if _UUID_MARKER.search(value):
        value = "[identifiant interne masqué]"
        if "identifiant interne masqué" not in reason.casefold():
            reason += " ; identifiant interne masqué"
    item = _ExcludedRecord(artifact_type, value, reason)
    excluded[(artifact_type, value, reason)] = item


def _rendered_value_for_pipeline(item: object) -> tuple[str, str, str | None]:
    artifact_type = _artifact_type(_field(item, "artifact_type", _field(item, "type", "")))
    raw_value = _text(_field(item, "value"))
    projected_value, result = _project_value(artifact_type, raw_value)
    if _UUID_MARKER.search(projected_value or raw_value):
        return artifact_type, raw_value, "internal_uuid"
    if projected_value is None:
        return artifact_type, raw_value, result
    return artifact_type, projected_value, None


def _article_rows(
    projection_rows: Sequence[IOCRow],
    original_items: Sequence[object],
) -> tuple[tuple[IOCRow, ...], tuple[_ExcludedRecord, ...]]:
    rows: dict[tuple[int, str, str], IOCRow] = {}
    extra_excluded: dict[tuple[str, str, str], _ExcludedRecord] = {}
    for row in projection_rows:
        if _UUID_MARKER.search(row.value):
            _add_excluded(
                extra_excluded,
                row.type,
                row.value,
                _excluded_label("internal_uuid"),
            )
            continue
        normalized = (
            row.normalized_value.casefold()
            if row.type in {"ip", "domain", "file"}
            else row.normalized_value
        )
        rows[(row.is_new, row.type, normalized)] = row
    for item in original_items:
        artifact_type, value, reason = _rendered_value_for_pipeline(item)
        if reason is not None:
            _add_excluded(
                extra_excluded,
                artifact_type,
                _text(_field(item, "value")),
                _excluded_label(reason),
            )
            continue
        projected_value, ioc_type = _project_value(artifact_type, value)
        if projected_value is None:
            continue
        normalized_value = (
            projected_value.casefold() if ioc_type in {"ip", "domain", "file"} else projected_value
        )
        row = IOCRow(1, ioc_type, projected_value, normalized_value)
        key_normalized = (
            row.normalized_value.casefold()
            if row.type in {"ip", "domain", "file"}
            else row.normalized_value
        )
        key = (row.is_new, row.type, key_normalized)
        existing = rows.get(key)
        if existing is None or (row.value.casefold(), row.value) < (
            existing.value.casefold(),
            existing.value,
        ):
            rows[key] = row
    return (
        tuple(
            sorted(
                rows.values(),
                key=lambda row: (
                    row.is_new,
                    _PIPELINE_TYPE_ORDER[row.type],
                    row.normalized_value.casefold(),
                    row.value,
                ),
            )
        ),
        tuple(extra_excluded.values()),
    )


def _excluded_rows(
    projection_excluded: Sequence[object],
    original_items: Sequence[object],
    candidate_items: Sequence[object],
    extra_excluded: Sequence[_ExcludedRecord],
) -> tuple[_ExcludedRecord, ...]:
    excluded: dict[tuple[str, str, str], _ExcludedRecord] = {}
    for item in projection_excluded:
        artifact_type = _artifact_type(_field(item, "type", ""))
        raw_reason = _text(_field(item, "reason"))
        _add_excluded(
            excluded,
            artifact_type,
            _text(_field(item, "value")),
            _excluded_label(raw_reason),
        )
    for item in extra_excluded:
        _add_excluded(excluded, item.artifact_type, item.value, item.reason)
    for item in original_items:
        artifact_type, _raw_value, reason = _rendered_value_for_pipeline(item)
        if reason is not None:
            _add_excluded(
                excluded,
                artifact_type,
                _text(_field(item, "value")),
                _excluded_label(reason),
            )
    for item in candidate_items:
        artifact_type, raw_value, reason = _rendered_value_for_pipeline(item)
        reason_label = _excluded_label(reason) if reason is not None else "Piste non confirmée"
        if reason is not None:
            reason_label += " ; piste non confirmée"
        _add_excluded(excluded, artifact_type, raw_value, reason_label)
    return tuple(
        sorted(
            excluded.values(),
            key=lambda item: (
                item.artifact_type,
                item.value.casefold(),
                item.value,
                item.reason.casefold(),
            ),
        )
    )


def _csv_bytes(headers: Sequence[str], rows: Sequence[Sequence[str]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output, lineterminator="\n")
    writer.writerow(headers)
    writer.writerows(rows)
    return output.getvalue().encode("utf-8")


def build_opinion_ioc_csv_exports(
    publication_document: CanonicalPublicationDocument,
    render_data: object,
) -> dict[str, bytes]:
    """Return deterministic CSV bytes from already-loaded canonical/rendered data."""
    projection = project_publication_iocs(publication_document)
    ioc_section = _section(render_data, "ioc")
    original_items = _group_items(ioc_section, "original_indicators")
    candidate_items = _group_items(ioc_section, "candidate_indicators")
    article_rows, extra_excluded = _article_rows(projection.rows, original_items)
    excluded = _excluded_rows(projection.excluded, original_items, candidate_items, extra_excluded)
    readable_records = _readable_records(projection.rows, render_data)
    network_records = [item for item in readable_records if item.artifact_type in _NETWORK_TYPES]
    file_records = [item for item in readable_records if item.artifact_type in _FILE_TYPES]
    names_records = [
        item for item in readable_records if item.artifact_type in {"filename", "filepath"}
    ]
    candidate_records = [item for item in readable_records if item.status == "Piste non confirmée"]

    metadata_line = _safe_metadata_line(publication_document)
    article_lines = [metadata_line, "isNew,type,value"]
    article_lines.extend(f"{row.is_new},{row.type},{row.value}" for row in article_rows)
    article_content = ("\n".join(article_lines) + "\n").encode("utf-8")

    file_rows = []
    for item in file_records:
        algorithm = ""
        if item.artifact_type in {"hash", "file"}:
            algorithm = _HASH_ALGORITHMS.get(len(item.value), "Non déterminé")
            if algorithm != "Non déterminé" and _HEX_HASH.fullmatch(item.value) is None:
                algorithm = "Non déterminé"
        file_rows.append(
            (
                "Fichiers",
                algorithm,
                item.value,
                item.associated_file,
                item.status,
                item.source,
            )
        )

    network_rows = [
        ("IOC", _type_label(item.artifact_type), item.value, item.status, item.source)
        for item in network_records
    ]
    names_rows = [
        ("Fichiers", _type_label(item.artifact_type), item.value, item.status, item.source)
        for item in names_records
    ]
    candidate_rows = [
        (
            "IOC" if item.artifact_type in _NETWORK_TYPES else "Fichiers",
            _type_label(item.artifact_type),
            item.value,
            item.status,
            item.source,
        )
        for item in candidate_records
    ]
    excluded_csv_rows = [
        (_type_label(item.artifact_type), item.value, item.reason) for item in excluded
    ]

    return {
        "ioc_pipeline_article.csv": article_content,
        "LISEZ-MOI.md": _READ_ME.encode("utf-8"),
        "ioc_fichiers.csv": _csv_bytes(
            ("Catégorie", "Algorithme", "Valeur", "Nom ou chemin associé", "Statut", "Source"),
            file_rows,
        ),
        "ioc_reseau.csv": _csv_bytes(
            ("Catégorie", "Type", "Valeur", "Statut", "Source"), network_rows
        ),
        "noms_et_chemins_de_fichiers.csv": _csv_bytes(
            ("Catégorie", "Type", "Valeur", "Statut", "Source"), names_rows
        ),
        "pistes_non_confirmees.csv": _csv_bytes(
            ("Catégorie", "Type", "Valeur", "Statut", "Source"), candidate_rows
        ),
        "ioc_exclus.csv": _csv_bytes(("Type", "Valeur", "Motif"), excluded_csv_rows),
    }


def _load_input(
    path: Path, expected_opinion_id: UUID, expected_version: int
) -> tuple[CanonicalPublicationDocument, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("Impossible de lire le fichier JSON d'entrée") from exc
    if not isinstance(payload, Mapping):
        raise ValueError("Le fichier JSON d'entrée doit contenir un objet")
    try:
        opinion_id = UUID(str(payload["opinion_id"]))
        version = payload["opinion_version"]
        if isinstance(version, bool) or not isinstance(version, int):
            raise ValueError
        if opinion_id != expected_opinion_id or version != expected_version:
            raise ValueError
        document_payload = payload["publication_document"]
        render_data = payload["render_data"]
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("L'identité de l'avis ne correspond pas aux options fournies") from exc
    if not isinstance(document_payload, Mapping):
        raise ValueError("publication_document doit être un document JSON canonique")
    try:
        document = publication_document_from_json(document_payload)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError("Le document de publication canonique est invalide") from exc
    sections = _field(render_data, "content_sections")
    if not isinstance(sections, Sequence) or isinstance(sections, (str, bytes)):
        raise ValueError("render_data doit contenir les sections rendues du PDF d'aperçu")
    return document, render_data


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Exporte les IOC d'un avis vers les CSV de livraison, sans appel réseau.",
        epilog=(
            "Le JSON d'entrée doit fournir opinion_id, opinion_version, publication_document "
            "(document canonique) et render_data.content_sections (projection de l'aperçu)."
        ),
    )
    parser.add_argument("--input-json", required=True, type=Path)
    parser.add_argument("--opinion-id", required=True, type=UUID)
    parser.add_argument("--version", required=True, type=int)
    parser.add_argument("--output-dir", required=True, type=Path)
    arguments = parser.parse_args(argv)
    if arguments.version < 1:
        parser.error("--version doit être supérieure ou égale à 1")
    try:
        document, render_data = _load_input(
            arguments.input_json, arguments.opinion_id, arguments.version
        )
        exports = build_opinion_ioc_csv_exports(document, render_data)
        arguments.output_dir.mkdir(parents=True, exist_ok=True)
        for filename, content in exports.items():
            (arguments.output_dir / filename).write_bytes(content)
    except (OSError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    csv_count = sum(filename.endswith(".csv") for filename in exports)
    print(f"{csv_count} CSV et un guide écrits dans {arguments.output_dir}")
    return 0


__all__ = ["build_opinion_ioc_csv_exports", "main"]
