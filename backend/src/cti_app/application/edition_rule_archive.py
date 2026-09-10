"""Edition-wide detection rule archive built from canonical extraction artifacts.

The published bulletin does not carry detection rules: ``PublicationDocumentV2``
holds the narrative, the indicators and the sources, while YARA/Sigma/Suricata
rules live in the Extraction artifact of each article.  The workspace sidecars
under ``items/*/article/rules`` are a disposable projection marked
``"canonical": false``.

This service therefore rebuilds the archive from the canonical artifacts pinned
by the publication manifest, so the ZIP always describes exactly the articles
that were frozen into the bulletin — even when the workspace has been purged.
"""

from __future__ import annotations

import io
import json
import zipfile
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from cti_app.application.edition_workspace import detection_rule_filename
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import technical_extraction_from_json
from cti_app.domain.editions import Edition
from cti_app.domain.production import (
    DetectionRule,
    DetectionRuleType,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)

ARCHIVE_SCHEMA_VERSION = "1"

# A fixed timestamp keeps the archive byte-for-byte reproducible: two exports of
# the same frozen edition must hash identically.
_ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)

_TYPE_DIRECTORIES = {
    DetectionRuleType.YARA: "yara",
    DetectionRuleType.SIGMA: "sigma",
    DetectionRuleType.SURICATA: "suricata",
    DetectionRuleType.SNORT: "snort",
}


class EditionRuleArchiveError(ValueError):
    """The edition cannot produce a detection rule archive."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class EditionRuleArchive:
    edition_id: UUID
    filename: str
    content: bytes
    rule_count: int
    counts_by_type: dict[str, int]


@dataclass(frozen=True, slots=True)
class _ArticleRules:
    position: int
    subject_id: UUID
    title: str
    rules: tuple[DetectionRule, ...]


class EditionRuleArchiveService:
    """Build the ZIP of every detection rule published by one edition."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def build(self, edition_id: UUID) -> EditionRuleArchive:
        async with self._uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
            if edition is None:
                raise EditionRuleArchiveError("edition_not_found")
            manifest = await uow.publication_manifests.get_latest_for_edition(edition_id)
            if manifest is None:
                raise EditionRuleArchiveError("publication_manifest_not_found")
            articles = [
                await self._read_article(uow, entry)
                for entry in sorted(manifest.entries, key=lambda item: item.position)
            ]

        return _build_archive(
            edition,
            manifest_id=manifest.id,
            manifest_sha256=manifest.content_sha256,
            articles=articles,
        )

    async def _read_article(self, uow: Any, entry: Any) -> _ArticleRules:
        publication = await uow.production_artifacts.get(entry.document_artifact_id)
        if (
            publication is None
            or publication.production_run_id != entry.production_run_id
            or publication.canonical_blob_id is None
        ):
            raise EditionRuleArchiveError("manifest_artifact_mismatch")

        extraction = await self._resolve_extraction(uow, publication, entry.production_run_id)
        if (
            extraction is None
            or extraction.stage is not ProductionArtifactStage.EXTRACTION
            or extraction.status is not ProductionArtifactStatus.VERIFIED
            or extraction.canonical_blob_id is None
        ):
            raise EditionRuleArchiveError("extraction_artifact_unavailable")

        try:
            document = await self._artifact_store.read_json(publication.canonical_blob_id)
            payload = await self._artifact_store.read_json(extraction.canonical_blob_id)
            rules = technical_extraction_from_json(payload).rules
        except (KeyError, TypeError, ValueError) as exc:
            raise EditionRuleArchiveError("extraction_document_invalid") from exc

        title = document.get("title") if isinstance(document, Mapping) else None
        return _ArticleRules(
            position=entry.position,
            subject_id=entry.subject_id,
            title=str(title) if isinstance(title, str) and title.strip() else "Sans titre",
            rules=tuple(rules),
        )

    @staticmethod
    async def _resolve_extraction(uow: Any, publication: Any, run_id: UUID) -> Any:
        """Follow the Extraction the frozen document itself recorded consuming.

        Documents assembled before ``input_artifacts`` existed carry no such
        pin; for those the run's current Extraction is the only available
        answer, and the freeze already proved the two are compatible.
        """
        metadata = getattr(publication, "metadata", None)
        inputs = metadata.get("input_artifacts") if isinstance(metadata, Mapping) else None
        if isinstance(inputs, Mapping):
            recorded = inputs.get("extraction_artifact_id")
            if recorded:
                try:
                    artifact_id = UUID(str(recorded))
                except ValueError as exc:
                    raise EditionRuleArchiveError("extraction_artifact_unavailable") from exc
                return await uow.production_artifacts.get(artifact_id)
        return await uow.production_artifacts.get_current(
            run_id, ProductionArtifactStage.EXTRACTION.value
        )


def _build_archive(
    edition: Edition,
    *,
    manifest_id: UUID,
    manifest_sha256: str,
    articles: list[_ArticleRules],
) -> EditionRuleArchive:
    # One rule may be published by several articles.  It is written once, under
    # its type directory, and the manifest names every article that carries it.
    files: dict[str, tuple[DetectionRule, list[_ArticleRules]]] = {}
    for article in articles:
        for rule in article.rules:
            path = f"{_TYPE_DIRECTORIES[rule.rule_type]}/{detection_rule_filename(rule)}"
            existing = files.get(path)
            if existing is None:
                files[path] = (rule, [article])
            elif all(carrier.position != article.position for carrier in existing[1]):
                existing[1].append(article)

    ordered = sorted(files.items(), key=lambda item: item[0])
    counts_by_type: dict[str, int] = {}
    for _path, (rule, _) in ordered:
        directory = _TYPE_DIRECTORIES[rule.rule_type]
        counts_by_type[directory] = counts_by_type.get(directory, 0) + 1

    manifest_payload = {
        "schema_version": ARCHIVE_SCHEMA_VERSION,
        "edition": {
            "id": str(edition.id),
            "country": edition.country,
            "country_code": edition.country_code,
            "period_start": edition.period_start.isoformat(),
            "period_end": edition.period_end.isoformat(),
            "tlp": edition.tlp.value,
        },
        "publication_manifest": {
            "id": str(manifest_id),
            "sha256": manifest_sha256,
        },
        "counts": {"total": len(ordered), **counts_by_type},
        "rules": [
            {
                "path": path,
                "type": rule.rule_type.value,
                "name": rule.name,
                "sha256": rule.sha256,
                "supported": rule.supported,
                "source_ids": sorted(set(rule.source_ids)),
                "context": rule.context,
                "articles": [
                    {
                        "position": article.position,
                        "subject_id": str(article.subject_id),
                        "title": article.title,
                    }
                    for article in sorted(carriers, key=lambda value: value.position)
                ],
            }
            for path, (rule, carriers) in ordered
        ],
    }

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write(archive, "README.md", _readme(edition, len(ordered), counts_by_type))
        _write(archive, "manifest.json", _canonical_json(manifest_payload))
        for path, (rule, _) in ordered:
            _write(archive, path, rule.body)

    return EditionRuleArchive(
        edition_id=edition.id,
        filename=_filename(edition),
        content=buffer.getvalue(),
        rule_count=len(ordered),
        counts_by_type=counts_by_type,
    )


def _write(archive: zipfile.ZipFile, path: str, text: str) -> None:
    info = zipfile.ZipInfo(path, date_time=_ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o644 << 16
    archive.writestr(info, text.encode("utf-8"))


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _period(edition: Edition) -> str:
    return edition.period_start.strftime("%Y-%m")


def _filename(edition: Edition) -> str:
    return f"regles-{edition.country_code}-{_period(edition)}.zip"


def _readme(edition: Edition, total: int, counts_by_type: dict[str, int]) -> str:
    lines = [
        f"# Règles de détection — {edition.country} ({_period(edition)})",
        "",
        f"TLP:{edition.tlp.value}",
        "",
        f"Cette archive contient les {total} règle(s) de détection publiées par le",
        "bulletin, regroupées par moteur. Elle est construite à partir des artefacts",
        "canoniques épinglés par le manifest de publication : son contenu correspond",
        "exactement aux articles retenus dans le bulletin.",
        "",
        "## Contenu",
        "",
    ]
    if counts_by_type:
        lines.extend(
            f"- `{directory}/` — {counts_by_type[directory]} règle(s)"
            for directory in sorted(counts_by_type)
        )
    else:
        lines.append("- aucune règle de détection n'a été publiée par cette édition")
    lines.extend(
        [
            "- `manifest.json` — provenance de chaque règle : type, empreinte SHA-256,",
            "  sources d'origine et articles qui la portent.",
            "",
            "Une règle publiée par plusieurs articles n'est écrite qu'une fois ; le",
            "manifest en nomme tous les articles porteurs.",
            "",
            "Les règles sont reproduites telles que publiées, sans validation de",
            "compilation. Le champ `supported` du manifest indique si la source la",
            "présentait comme éprouvée.",
            "",
        ]
    )
    return "\n".join(lines)


__all__ = [
    "ARCHIVE_SCHEMA_VERSION",
    "EditionRuleArchive",
    "EditionRuleArchiveError",
    "EditionRuleArchiveService",
]
