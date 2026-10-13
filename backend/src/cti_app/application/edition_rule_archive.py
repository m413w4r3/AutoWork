"""Edition-wide detection rule archive built from canonical extraction artifacts.

The published bulletin does not carry detection rules: ``PublicationDocumentV4``
holds the narrative, the indicators and the sources, while YARA/Sigma/Suricata
rules live in the Extraction artifact of each article.  The workspace sidecars
under ``items/*/article/rules`` are a disposable projection marked
``"canonical": false``.

This service therefore rebuilds the archive from the canonical artifacts pinned
by the publication manifest, so the ZIP always describes exactly the articles
that were frozen into the bulletin — even when the workspace has been purged.
"""

from __future__ import annotations

import hashlib
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
from cti_app.application.production_extraction import legacy_technical_extraction_from_payload
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import (
    AnalystDecision,
    AnalystDecisionTargetType,
    AnalystDecisionType,
)
from cti_app.domain.investigation import (
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    EvidenceKind,
    EvidenceObservation,
)
from cti_app.domain.production import (
    DetectionRule,
    DetectionRuleType,
    ProductionArtifact,
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

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.content).hexdigest()


@dataclass(frozen=True, slots=True)
class _ArticleRules:
    position: int
    subject_id: UUID
    title: str
    production_run_id: UUID
    publication_artifact_id: UUID
    publication_version: int
    publication_sha256: str
    rules: tuple[DetectionRule, ...]


@dataclass(frozen=True, slots=True)
class _AnalystRule:
    version: DetectionCandidateVersion
    decision: AnalystDecision
    test_report: EvidenceObservation
    article: _ArticleRules
    report_payload: Mapping[str, Any]


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
            manifest = await uow.publication_manifests.get_latest_manifest_for_edition(edition_id)
            if manifest is None:
                raise EditionRuleArchiveError("publication_manifest_not_found")
            articles = [
                await self._read_article(uow, entry)
                for entry in sorted(manifest.entries, key=lambda item: item.position)
            ]
            analyst_rules = await self._read_approved_analyst_rules(uow, manifest, articles)

        return _build_archive(
            edition,
            manifest_id=manifest.id,
            manifest_sha256=manifest.content_sha256,
            articles=articles,
            analyst_rules=analyst_rules,
        )

    async def _read_article(self, uow: Any, entry: Any) -> _ArticleRules:
        publication = await uow.production_artifacts.get(entry.document_artifact_id)
        if (
            publication is None
            or publication.production_run_id != entry.production_run_id
            or publication.subject_id != entry.subject_id
            or publication.version != entry.document_artifact_version
            or publication.input_hash != entry.document_input_hash
            or publication.stage is not ProductionArtifactStage.PUBLICATION
            or publication.status is not ProductionArtifactStatus.VERIFIED
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
            rules = legacy_technical_extraction_from_payload(payload).rules
        except (KeyError, TypeError, ValueError) as exc:
            raise EditionRuleArchiveError("extraction_document_invalid") from exc

        title_source = document
        if (
            isinstance(document, Mapping)
            and document.get("schema_version") == "6"
            and isinstance(document.get("base_publication"), Mapping)
        ):
            title_source = document["base_publication"]
        title = title_source.get("title") if isinstance(title_source, Mapping) else None
        publication_sha256 = getattr(entry, "document_sha256", None)
        actual_publication_sha256 = hashlib.sha256(
            ProductionArtifactStore.canonical_json_bytes(document)
        ).hexdigest()
        if publication_sha256 is not None and publication_sha256 != actual_publication_sha256:
            raise EditionRuleArchiveError("manifest_artifact_mismatch")
        return _ArticleRules(
            position=entry.position,
            subject_id=entry.subject_id,
            title=str(title) if isinstance(title, str) and title.strip() else "Sans titre",
            production_run_id=entry.production_run_id,
            publication_artifact_id=publication.id,
            publication_version=publication.version,
            publication_sha256=publication_sha256 or actual_publication_sha256,
            rules=tuple(rules),
        )

    async def _read_approved_analyst_rules(
        self, uow: Any, manifest: Any, articles: list[_ArticleRules]
    ) -> tuple[_AnalystRule, ...]:
        # Lightweight archive unit fakes may omit investigation repositories;
        # the real production UoW always exposes all four ports together.
        repositories = (
            "analyst_investigations",
            "analyst_decisions",
            "detection_candidates",
            "evidence_observations",
        )
        available = [hasattr(uow, name) for name in repositories]
        if not any(available):
            return ()
        if not all(available):
            raise EditionRuleArchiveError("analyst_archive_repository_unavailable")

        result: list[_AnalystRule] = []
        for article in articles:
            investigation = await uow.analyst_investigations.get_for_run(article.production_run_id)
            if investigation is None or investigation.subject_id != article.subject_id:
                continue
            decisions = tuple(await uow.analyst_decisions.list_for_investigation(investigation.id))
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(investigation.id)
            )
            by_id = {item.id: item for item in observations}
            effective: dict[UUID, AnalystDecision] = {}
            for decision in sorted(decisions, key=lambda item: (item.occurred_at, str(item.id))):
                if (
                    decision.target_type is AnalystDecisionTargetType.DETECTION
                    and decision.decision_type
                    in {
                        AnalystDecisionType.DETECTION_APPROVE,
                        AnalystDecisionType.DETECTION_REJECT,
                    }
                    and decision.occurred_at <= manifest.created_at
                ):
                    effective[decision.target_id] = decision

            for version_id, decision in sorted(effective.items(), key=lambda item: str(item[0])):
                if decision.decision_type is not AnalystDecisionType.DETECTION_APPROVE:
                    continue
                version = await uow.detection_candidates.get(version_id)
                if (
                    version is None
                    or version.investigation_id != investigation.id
                    or version.status is DetectionCandidateStatus.REJECTED
                ):
                    continue
                archived = await self._read_approved_analyst_rule(
                    uow,
                    article,
                    version,
                    decision,
                    by_id,
                )
                if archived is not None:
                    result.append(archived)
        return tuple(
            sorted(
                result,
                key=lambda item: (
                    item.article.position,
                    str(item.version.candidate_id),
                    item.version.version,
                    str(item.version.id),
                ),
            )
        )

    async def _read_approved_analyst_rule(
        self,
        uow: Any,
        article: _ArticleRules,
        version: DetectionCandidateVersion,
        decision: AnalystDecision,
        observations: Mapping[UUID, EvidenceObservation],
    ) -> _AnalystRule | None:
        details = decision.details
        base_details = details.get("base_publication")
        report_details = details.get("test_report")
        if (
            details.get("schema") != "detection-review.v1"
            or details.get("source_sha256") != version.source_sha256
            or hashlib.sha256(version.source.encode("utf-8")).hexdigest() != version.source_sha256
            or not isinstance(base_details, Mapping)
            or not isinstance(report_details, Mapping)
        ):
            return None
        try:
            base_id = UUID(str(base_details["artifact_id"]))
            report_id = UUID(str(report_details["observation_id"]))
        except (KeyError, TypeError, ValueError):
            return None
        report = observations.get(report_id)
        if (
            report is None
            or report.investigation_id != version.investigation_id
            or report.kind is not EvidenceKind.YARA_MATCH
            or report.object_type != "detection_candidate_version"
            or report.object_id != str(version.id)
            or report.sha256 != report_details.get("sha256")
        ):
            return None
        try:
            report_payload = await self._artifact_store.read_json(report.blob_id)
            report_hash = hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(report_payload)
            ).hexdigest()
        except (KeyError, TypeError, ValueError):
            return None
        if (
            not isinstance(report_payload, Mapping)
            or report_hash != report.sha256
            or report_payload.get("schema") != "investigation-yara-test.v1"
            or report_payload.get("candidate_id") != str(version.candidate_id)
            or report_payload.get("version_id") != str(version.id)
            or report_payload.get("proposal_source_sha256") != version.source_sha256
            or report_payload.get("source_sha256") != version.source_sha256
            or report_payload.get("corpus_sha256") != report_details.get("corpus_sha256")
        ):
            return None

        base = await uow.production_artifacts.get(base_id)
        if (
            base is None
            or base.stage is not ProductionArtifactStage.PUBLICATION
            or base.status is not ProductionArtifactStatus.VERIFIED
            or base.production_run_id != article.production_run_id
            or base.subject_id != article.subject_id
            or base.canonical_blob_id is None
        ):
            return None
        try:
            base_payload = await self._artifact_store.read_json(base.canonical_blob_id)
            base_sha256 = hashlib.sha256(
                ProductionArtifactStore.canonical_json_bytes(base_payload)
            ).hexdigest()
        except (KeyError, TypeError, ValueError):
            return None
        if base_sha256 != base_details.get("sha256"):
            return None
        if not await self._is_publication_descendant(
            uow, base, article, base_payload, version.investigation_id
        ):
            return None
        return _AnalystRule(
            version=version,
            decision=decision,
            test_report=report,
            article=article,
            report_payload=report_payload,
        )

    async def _is_publication_descendant(
        self,
        uow: Any,
        base: ProductionArtifact,
        article: _ArticleRules,
        base_payload: Any,
        investigation_id: UUID,
    ) -> bool:
        current = base
        payload = base_payload
        seen: set[UUID] = set()
        while current.id not in seen:
            if (
                current.id == article.publication_artifact_id
                and current.version == article.publication_version
            ):
                return True
            seen.add(current.id)
            if not isinstance(payload, Mapping) or payload.get("schema_version") != "6":
                return False
            metadata_parent = current.metadata.get("previous_publication_artifact_id")
            augmentation = current.metadata.get("publication_augmentation")
            provenance = payload.get("provenance")
            if (
                metadata_parent is None
                or not isinstance(augmentation, Mapping)
                or str(augmentation.get("investigation_id")) != str(investigation_id)
                or not isinstance(provenance, Mapping)
                or str(provenance.get("base_publication_artifact_id")) != str(metadata_parent)
            ):
                return False
            try:
                parent_id = UUID(str(metadata_parent))
            except ValueError:
                return False
            parent = await uow.production_artifacts.get(parent_id)
            if (
                parent is None
                or parent.stage is not ProductionArtifactStage.PUBLICATION
                or parent.production_run_id != article.production_run_id
                or parent.subject_id != article.subject_id
                or parent.canonical_blob_id is None
            ):
                return False
            try:
                payload = await self._artifact_store.read_json(parent.canonical_blob_id)
                parent_sha256 = hashlib.sha256(
                    ProductionArtifactStore.canonical_json_bytes(payload)
                ).hexdigest()
            except (KeyError, TypeError, ValueError):
                return False
            if (
                provenance.get("base_publication_sha256") != parent_sha256
                or parent.status is not ProductionArtifactStatus.VERIFIED
            ):
                return False
            current = parent
        return False

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
    analyst_rules: tuple[_AnalystRule, ...] = (),
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
    if analyst_rules:
        counts_by_type["analyst_yara"] = len(analyst_rules)

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
        "counts": {"total": len(ordered) + len(analyst_rules), **counts_by_type},
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
    analyst_files = [
        (
            _analyst_rule_path(item.version),
            item,
        )
        for item in analyst_rules
    ]
    if analyst_files:
        manifest_payload["analyst_rules"] = [
            _analyst_rule_manifest(path, item) for path, item in analyst_files
        ]

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        _write(
            archive,
            "README.md",
            _readme(edition, len(ordered) + len(analyst_rules), counts_by_type),
        )
        _write(archive, "manifest.json", _canonical_json(manifest_payload))
        for path, (rule, _) in ordered:
            _write(archive, path, rule.body)
        for path, item in analyst_files:
            _write(archive, path, item.version.source)

    content = buffer.getvalue()
    return EditionRuleArchive(
        edition_id=edition.id,
        filename=_filename(edition),
        content=content,
        rule_count=len(ordered) + len(analyst_rules),
        counts_by_type=counts_by_type,
    )


def _analyst_rule_path(version: DetectionCandidateVersion) -> str:
    return f"analyst/yara/{version.candidate_id}-v{version.version}-{version.id}.yar"


def _analyst_rule_manifest(path: str, item: _AnalystRule) -> dict[str, Any]:
    report_payload = item.report_payload
    test_report = item.decision.details["test_report"]
    return {
        "path": path,
        "id": str(item.version.id),
        "candidate_id": str(item.version.candidate_id),
        "version": item.version.version,
        "source_sha256": item.version.source_sha256,
        "article": {
            "position": item.article.position,
            "subject_id": str(item.article.subject_id),
            "artifact_id": str(item.article.publication_artifact_id),
            "version": item.article.publication_version,
            "sha256": item.article.publication_sha256,
            "title": item.article.title,
        },
        "investigation_id": str(item.version.investigation_id),
        "approver": item.decision.actor_id,
        "approved_at": item.decision.occurred_at.isoformat(),
        "known_limitations": item.decision.details.get("known_limitations", ""),
        "test_report": {
            "observation_id": str(item.test_report.id),
            "sha256": item.test_report.sha256,
            "corpus_sha256": test_report.get("corpus_sha256"),
            "engine_version": report_payload.get("engine_version"),
        },
    }


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
