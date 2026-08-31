"""Best-effort local projection of an edition's production checkpoints."""

from __future__ import annotations

import hashlib
import json
import os
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.diagnostics import DiagnosticsLog
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_resolver import current_publication_artifact
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import technical_extraction_from_json
from cti_app.application.production_state import (
    ProductionStateError,
    ProductionStateService,
    ProductionStateSnapshotV1,
    ProductionStateSnapshotV2,
)
from cti_app.domain.production import DetectionRule, DetectionRuleType

_COUNTRY_CODE = re.compile(r"^[A-Z]{2}$")
_SAFE_COMPONENT = re.compile(r"[^A-Za-z0-9_-]+")
_MAX_SLUG_LENGTH = 80


@dataclass(frozen=True, slots=True)
class EditionWorkspaceMaterialization:
    path: Path
    item_path: Path
    files: tuple[Path, ...]
    rule_sidecar_error: str | None = None


@dataclass(frozen=True, slots=True)
class _CheckpointContext:
    run_id: UUID
    edition_id: UUID
    subject_id: UUID
    position: int
    period: date
    country_code: str
    subject_title: str


def safe_slug(value: str) -> str:
    """Return a bounded path component derived from user-controlled text."""
    normalized = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode("ascii")
    slug = _SAFE_COMPONENT.sub("-", normalized).strip("-_")[:_MAX_SLUG_LENGTH]
    slug = slug.strip("-_")
    return slug or "item"


class EditionWorkspaceMaterializer:
    """Materialize only the small, already available editorial projections."""

    def __init__(self, workspace_root: Path) -> None:
        self._workspace_root = workspace_root

    async def materialize(
        self,
        *,
        edition_id: UUID,
        period: date,
        country_code: str,
        position: int,
        subject_id: UUID,
        subject_title: str,
        production_state: ProductionStateSnapshotV1 | ProductionStateSnapshotV2,
        publication: Mapping[str, Any] | None = None,
        rendered_content: str | None = None,
        sources: Sequence[Mapping[str, Any]] = (),
        assets: Sequence[Mapping[str, Any]] = (),
        rules: Sequence[DetectionRule] | None = None,
    ) -> EditionWorkspaceMaterialization:
        normalized_country = self._normalize_country_code(country_code)
        edition_path = self._prepare_edition_path(
            self._workspace_root, edition_id, period, normalized_country
        )
        self._write_json(
            edition_path / "manifest.json",
            {
                "canonical": False,
                "country_code": normalized_country,
                "edition_id": str(edition_id),
                "period": period.strftime("%Y-%m"),
            },
        )

        item_path = self._prepare_item_path(
            edition_path, position, safe_slug(subject_title), subject_id
        )
        files: list[Path] = []
        state_path = item_path / "pipeline" / "production-state.json"
        state_payload = production_state.model_dump(mode="json")
        self._write_state_if_changed(state_path, state_payload)
        files.append(state_path)

        if publication is not None:
            publication_path = item_path / "article" / "publication.json"
            self._write_json(publication_path, dict(publication))
            files.append(publication_path)
        if rendered_content is not None:
            rendered_path = item_path / "article" / "publication.md"
            self._write_text(rendered_path, rendered_content)
            files.append(rendered_path)
        if sources:
            sources_path = item_path / "sources" / "manifest.json"
            self._write_json(sources_path, {"canonical": False, "sources": list(sources)})
            files.append(sources_path)
        if assets:
            assets_path = item_path / "assets" / "manifest.json"
            self._write_json(assets_path, {"canonical": False, "assets": list(assets)})
            files.append(assets_path)

        rule_sidecar_error: str | None = None
        if rules is not None:
            try:
                files.extend(self._materialize_rule_sidecars(item_path, rules))
            except Exception as exc:
                # Sidecars are a filesystem projection only. The canonical
                # extraction remains authoritative and already persisted.
                rule_sidecar_error = f"{type(exc).__name__}: {exc}"

        return EditionWorkspaceMaterialization(
            path=edition_path,
            item_path=item_path,
            files=tuple(files),
            rule_sidecar_error=rule_sidecar_error,
        )

    @classmethod
    def _materialize_rule_sidecars(
        cls, item_path: Path, rules: Sequence[DetectionRule]
    ) -> tuple[Path, ...]:
        article_path = item_path / "article"
        rules_path = article_path / "rules"
        if rules_path.is_symlink():
            raise ValueError("Refusing to materialize rules through a symbolic link")
        rules_path.mkdir(parents=True, exist_ok=True)
        if not rules_path.resolve().is_relative_to(article_path.resolve()):
            raise ValueError("Rule sidecar path escaped the article workspace")

        entries: list[dict[str, Any]] = []
        files: list[Path] = []
        expected_filenames: set[str] = set()
        for rule in sorted(rules, key=lambda value: (value.rule_type.value, value.sha256)):
            filename = cls._rule_filename(rule)
            expected_filenames.add(filename)
            sidecar_path = rules_path / filename
            cls._write_text(sidecar_path, rule.body)
            files.append(sidecar_path)
            entries.append(
                {
                    "type": rule.rule_type.value,
                    "name": rule.name,
                    "filename": filename,
                    "sha256": rule.sha256,
                    "source_ids": list(sorted(set(rule.source_ids))),
                }
            )

        old_manifest = rules_path / "manifest.json"
        if old_manifest.is_file():
            try:
                old_payload = json.loads(old_manifest.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, ValueError):
                old_payload = {}
            old_entries = old_payload.get("rules", []) if isinstance(old_payload, dict) else []
            if isinstance(old_entries, list):
                for old_entry in old_entries:
                    if not isinstance(old_entry, dict):
                        continue
                    old_filename = old_entry.get("filename")
                    if (
                        isinstance(old_filename, str)
                        and old_filename not in expected_filenames
                        and Path(old_filename).name == old_filename
                    ):
                        stale_path = rules_path / old_filename
                        if stale_path.is_file() and not stale_path.is_symlink():
                            stale_path.unlink()

        manifest_path = rules_path / "manifest.json"
        cls._write_json(manifest_path, {"canonical": False, "rules": entries})
        files.append(manifest_path)
        return tuple(files)

    @staticmethod
    def _rule_filename(rule: DetectionRule) -> str:
        expected_sha256 = hashlib.sha256(rule.body.encode("utf-8")).hexdigest()
        if rule.sha256 != expected_sha256:
            raise ValueError("Rule sidecar hash does not match its body")
        extensions = {
            DetectionRuleType.YARA: ".yar",
            DetectionRuleType.SIGMA: ".yml",
            DetectionRuleType.SURICATA: ".rules",
            DetectionRuleType.SNORT: ".rules",
        }
        source = safe_slug(sorted(rule.source_ids)[0]) if rule.source_ids else "rule"
        name = safe_slug(rule.name or "unnamed-rule")[:48].strip("-_") or "rule"
        return f"{source}-{name}-{rule.sha256}{extensions[rule.rule_type]}"

    async def materialize_release(
        self,
        *,
        period: date,
        country_code: str,
        edition_id: UUID,
        manifest: Mapping[str, Any],
        edition: Mapping[str, Any],
        markdown: str,
        docx: bytes,
    ) -> Path:
        """Write the release projection after canonical persistence succeeds."""
        edition_path = self._prepare_edition_path(
            self._workspace_root, edition_id, period, country_code
        )
        release_path = edition_path / "release"
        self._write_canonical_json(release_path / "publication-manifest.json", manifest)
        self._write_canonical_json(release_path / "edition.json", edition)
        self._write_text(release_path / "edition.md", markdown)
        self._write_bytes(release_path / "bulletin.docx", docx)
        return release_path

    @staticmethod
    def _prepare_edition_path(
        workspace_root: Path, edition_id: UUID, period: date, country_code: str
    ) -> Path:
        normalized_country = EditionWorkspaceMaterializer._normalize_country_code(country_code)
        workspace_root.mkdir(parents=True, exist_ok=True)
        root = workspace_root.resolve()
        edition_path = root / f"{period:%Y-%m}_{normalized_country}"
        if edition_path.is_symlink():
            raise ValueError("Refusing to materialize through a symbolic link")
        edition_path.mkdir(parents=True, exist_ok=True)
        if not edition_path.resolve().is_relative_to(root):
            raise ValueError("Edition path escaped its configured root")
        for directory in ("items", "review", "release"):
            destination = edition_path / directory
            if destination.is_symlink():
                raise ValueError("Refusing to materialize through a symbolic link")
            destination.mkdir(parents=True, exist_ok=True)
        del edition_id  # Kept in the signature to make the path validation call explicit.
        return edition_path

    @staticmethod
    def _normalize_country_code(country_code: str) -> str:
        normalized = country_code.strip().upper()
        if not _COUNTRY_CODE.fullmatch(normalized):
            raise ValueError("Invalid edition country code")
        return normalized

    @staticmethod
    def _prepare_item_path(edition_path: Path, position: int, slug: str, subject_id: UUID) -> Path:
        if not 1 <= position <= 999:
            raise ValueError("Edition item position must be between 1 and 999")
        item_path = edition_path / "items" / f"{position:03d}-{slug}"
        if item_path.is_symlink():
            raise ValueError("Refusing to materialize through a symbolic link")
        item_path.mkdir(parents=True, exist_ok=True)
        if not item_path.resolve().is_relative_to((edition_path / "items").resolve()):
            raise ValueError(f"Item path escaped the edition workspace for {subject_id}")
        return item_path

    @staticmethod
    def _safe_destination(path: Path) -> None:
        if path.parent.is_symlink() or not path.parent.resolve().is_relative_to(
            path.parent.parent.resolve()
        ):
            raise ValueError("Workspace file escaped its configured root")
        if path.is_symlink():
            raise ValueError("Refusing to replace a symbolic link")

    @classmethod
    def _write_json(cls, path: Path, payload: Mapping[str, Any]) -> None:
        encoded = json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        cls._write_text(path, encoded)

    @classmethod
    def _write_canonical_json(cls, path: Path, payload: Mapping[str, Any]) -> None:
        cls._write_bytes(path, ProductionArtifactStore.canonical_json_bytes(dict(payload)))

    @classmethod
    def _write_state_if_changed(cls, path: Path, payload: Mapping[str, Any]) -> None:
        """Keep a retry's export timestamp from creating a filesystem diff."""
        if path.is_file():
            try:
                previous = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError, UnicodeError):
                previous = None
            if isinstance(previous, dict) and all(
                previous.get(key) == payload.get(key)
                for key in ("format", "schema_version", "origin", "artifacts")
            ):
                return
        cls._write_json(path, payload)

    @classmethod
    def _write_text(cls, path: Path, content: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cls._safe_destination(path)
        encoded = content.encode("utf-8")
        if path.is_file() and path.read_bytes() == encoded:
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(encoded)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    @classmethod
    def _write_bytes(cls, path: Path, content: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        cls._safe_destination(path)
        if path.is_file() and path.read_bytes() == content:
            return
        temporary = path.with_name(f".{path.name}.{uuid4().hex}.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)


class EditionProductionCheckpointService:
    """Persist a terminal production projection without affecting production."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        workspace_root: Path,
        *,
        diagnostics: DiagnosticsLog | None = None,
        materializer: EditionWorkspaceMaterializer | None = None,
        state_service: ProductionStateService | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._state = state_service or ProductionStateService(uow_factory, artifact_store)
        self._materializer = materializer or EditionWorkspaceMaterializer(workspace_root)
        self._diagnostics = diagnostics or DiagnosticsLog(None)

    async def checkpoint(
        self, run_id: UUID, *, correlation_id: str = "-"
    ) -> EditionWorkspaceMaterialization | None:
        context: _CheckpointContext | None = None
        try:
            context = await self._resolve_context(run_id)
            if context is None:
                return None
            try:
                state = await self._state.export_run_state(
                    context.run_id,
                    subject_title=context.subject_title,
                )
            except ProductionStateError as exc:
                if exc.code in {
                    "production_state_not_found",
                    "production_state_active_run",
                    "production_state_incomplete",
                    "production_state_unverified",
                }:
                    return None
                raise
            publication, rendered = await self._optional_publication(run_id)
            sources, assets = await self._optional_asset_manifests(context.subject_id)
            extraction = technical_extraction_from_json(
                state.artifacts.extraction.canonical_content
            )
            materialization = await self._materializer.materialize(
                edition_id=context.edition_id,
                period=context.period,
                country_code=context.country_code,
                position=context.position,
                subject_id=context.subject_id,
                subject_title=context.subject_title,
                production_state=state,
                publication=publication,
                rendered_content=rendered,
                sources=sources,
                assets=assets,
                rules=extraction.rules,
            )
            if materialization is not None and materialization.rule_sidecar_error is not None:
                self._diagnostics.record_failure(
                    event="production.rule_sidecar_projection_failed",
                    run_id=run_id,
                    subject_id=context.subject_id,
                    stage="checkpoint",
                    correlation_id=correlation_id,
                    error_code="rule_sidecar_projection_failed",
                    error=RuntimeError(materialization.rule_sidecar_error),
                    edition_id=str(context.edition_id),
                )
            return materialization
        except Exception as exc:
            self._diagnostics.record_failure(
                event="production.checkpoint_failed",
                run_id=run_id,
                subject_id=context.subject_id if context else None,
                stage="checkpoint",
                correlation_id=correlation_id,
                error=exc,
                checkpoint_operation="materialize",
                edition_id=str(context.edition_id) if context else None,
            )
            return None

    async def _resolve_context(self, run_id: UUID) -> _CheckpointContext | None:
        async with self._uow_factory() as uow:
            run = await uow.subject_production_runs.get(run_id)
            if run is None:
                return None
            item = await uow.edition_production_batch_items.get_by_run(run_id)
            edition = await uow.editions.get(run.edition_id)
            snapshot = await uow.production_input_snapshots.get_by_run(run_id)
            if item is None or edition is None:
                return None
            subject_title = snapshot.subject_title if snapshot is not None else str(run.subject_id)
            return _CheckpointContext(
                run_id=run.id,
                edition_id=run.edition_id,
                subject_id=run.subject_id,
                position=item.position,
                period=edition.period_start,
                country_code=edition.country_code,
                subject_title=subject_title,
            )

    async def _optional_publication(self, run_id: UUID) -> tuple[dict[str, Any] | None, str | None]:
        async with self._uow_factory() as uow:
            artifact = await current_publication_artifact(uow.production_artifacts, run_id)
        if artifact is None:
            return None, None
        publication: dict[str, Any] | None = None
        rendered: str | None = None
        if artifact.canonical_blob_id is not None:
            publication = await self._artifact_store.read_json(artifact.canonical_blob_id)
        if artifact.rendered_blob_id is not None:
            rendered = await self._artifact_store.read_text(artifact.rendered_blob_id)
        return publication, rendered

    async def _optional_asset_manifests(
        self, subject_id: UUID
    ) -> tuple[tuple[Mapping[str, Any], ...], tuple[Mapping[str, Any], ...]]:
        async with self._uow_factory() as uow:
            sources_repo = getattr(uow, "source_documents", None)
            samples_repo = getattr(uow, "samples", None)
            source_values = (
                await sources_repo.list_for_subject(subject_id) if sources_repo is not None else ()
            )
            sample_values = (
                await samples_repo.list_for_subject(subject_id) if samples_repo is not None else ()
            )
        return (
            tuple(_source_metadata(value) for value in source_values),
            tuple(_sample_metadata(value) for value in sample_values),
        )


def _source_metadata(source: object) -> Mapping[str, Any]:
    return {
        key: str(value)
        for key in (
            "id",
            "original_name",
            "logical_filename",
            "origin",
            "blob_id",
            "decoded_blob_id",
            "encoded_sha256",
            "decoded_sha256",
        )
        if (value := getattr(source, key, None)) is not None
    }


def _sample_metadata(sample: object) -> Mapping[str, Any]:
    return {
        key: str(value)
        for key in ("id", "original_name", "origin", "blob_id", "expected_hash")
        if (value := getattr(sample, key, None)) is not None
    }


__all__ = [
    "EditionProductionCheckpointService",
    "EditionWorkspaceMaterialization",
    "EditionWorkspaceMaterializer",
    "safe_slug",
]
