"""Canonical article anchors and rendered elements for note proposals (F4-D B4).

The publication base an analyst note targets is resolved exactly once, in the
same way acceptance resolves it: the pinned base of the existing opinion when
one exists, else the current verified PUBLICATION artifact.  The anchor space
itself comes from ``publication_note_body_anchors``, the single authority the
reader projection also uses, so an anchor offered by this route is always
renderable and an anchor absent here can never enter the canonical opinion.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Literal
from uuid import UUID

from cti_app.application.investigation_contracts import MAX_CONTEXT_PUBLICATION_ANCHORS
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.domain.analyst_opinion import investigation_opinion_id
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    publication_document_article_v5_body,
    publication_document_from_json,
    publication_element_sha256,
    publication_note_body_anchors,
    serialize_publication_document,
)
from cti_app.domain.semantic_annotation import lead_paragraph_anchor, section_paragraph_anchor

MAX_ARTICLE_ANCHORS = MAX_CONTEXT_PUBLICATION_ANCHORS
MAX_ARTICLE_ELEMENTS = MAX_CONTEXT_PUBLICATION_ANCHORS

ArticleBaseSource = Literal["pinned_opinion_base", "current_base"]


class ArticleBaseUnavailable(ValueError):
    """The pinned or current publication base cannot be read as an article."""


@dataclass(frozen=True, slots=True)
class ArticleBaseV1:
    """One resolved immutable article base with its exact identity."""

    artifact: ProductionArtifact
    sha256: str
    document: CanonicalPublicationDocument
    source: ArticleBaseSource


@dataclass(frozen=True, slots=True)
class ArticleAnchorV1:
    """One rendered paragraph anchor; indexes are the canonical zero-based ones."""

    anchor: str
    text: str
    text_sha256: str
    section_index: int | None
    paragraph_index: int | None


@dataclass(frozen=True, slots=True)
class ArticleElementV1:
    kind: str
    key: str
    title: str | None
    caption: str | None
    element_sha256: str
    anchor: str | None


@dataclass(frozen=True, slots=True)
class ArticleAnchorCatalogV1:
    base_publication_artifact_id: UUID
    base_publication_version: int
    base_publication_sha256: str
    source: ArticleBaseSource
    anchors: tuple[ArticleAnchorV1, ...]
    elements: tuple[ArticleElementV1, ...]
    truncated: bool


class ArticleAnchorErrorCode(StrEnum):
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    BASE_INVALID = "base_publication_invalid"


class ArticleAnchorError(ValueError):
    def __init__(
        self,
        code: ArticleAnchorErrorCode,
        *,
        status_code: int = 422,
        message: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message or code.value)


def _article_document(raw: bytes, *, require_canonical: bool) -> CanonicalPublicationDocument:
    try:
        payload = json.loads(raw)
        document = publication_document_from_json(payload)
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ArticleBaseUnavailable("The publication base is not a canonical document") from exc
    if publication_document_article_v5_body(document) is None:
        raise ArticleBaseUnavailable("The publication base is not an article body")
    if require_canonical and (
        ProductionArtifactStore.canonical_json_bytes(serialize_publication_document(document))
        != raw
    ):
        raise ArticleBaseUnavailable("The publication base is not its canonical encoding")
    return document


async def resolve_pinned_publication_base(
    uow: UnitOfWork,
    artifact_store: ProductionArtifactStore,
    *,
    artifact_id: UUID,
    sha256: str,
) -> ArticleBaseV1:
    """Read the exact base a proposal or opinion pinned, never a newer one."""
    artifact = await uow.production_artifacts.get(artifact_id)
    if artifact is None or artifact.canonical_blob_id is None or artifact.id != artifact_id:
        raise ArticleBaseUnavailable("The pinned publication base is unavailable")
    try:
        raw = await artifact_store.read_bytes(artifact.canonical_blob_id, max_bytes=None)
    except ProductionReuseStorageUnavailableError:
        raise
    except (OSError, TypeError, ValueError) as exc:
        raise ArticleBaseUnavailable("The pinned publication base is unreadable") from exc
    if hashlib.sha256(raw).hexdigest() != sha256:
        raise ArticleBaseUnavailable("The pinned publication base content changed")
    document = _article_document(raw, require_canonical=False)
    return ArticleBaseV1(
        artifact=artifact,
        sha256=sha256,
        document=document,
        source="pinned_opinion_base",
    )


async def resolve_current_publication_base(
    uow: UnitOfWork,
    artifact_store: ProductionArtifactStore,
    investigation: Any,
) -> ArticleBaseV1:
    """Resolve the current verified article base exactly as acceptance does."""
    base = await uow.production_artifacts.get_current_for_revision(
        investigation.production_run_id,
        ProductionArtifactStage.PUBLICATION.value,
    )
    if (
        base is None
        or base.stage is not ProductionArtifactStage.PUBLICATION
        or base.status is not ProductionArtifactStatus.VERIFIED
        or base.production_run_id != investigation.production_run_id
        or base.subject_id != investigation.subject_id
        or base.canonical_blob_id is None
    ):
        raise ArticleBaseUnavailable("The investigation has no verified article publication base")
    raw = await artifact_store.read_bytes(base.canonical_blob_id)
    sha256 = hashlib.sha256(raw).hexdigest()
    document = _article_document(raw, require_canonical=True)
    return ArticleBaseV1(artifact=base, sha256=sha256, document=document, source="current_base")


def _anchor_indexes(anchor: str) -> tuple[int | None, int]:
    if anchor.startswith("lead:"):
        return None, int(anchor.removeprefix("lead:")) - 1
    parts = anchor.split(":")
    return int(parts[1]), int(parts[3]) - 1


def _element_anchor(
    resolved: dict[tuple[str, str], Any],
    *,
    kind: str,
    key: str,
) -> str | None:
    placement = resolved.get((kind, key))
    if placement is None:
        return None
    if placement.paragraph_kind == "lead":
        return lead_paragraph_anchor(placement.paragraph_index + 1)
    return section_paragraph_anchor(placement.section_index or 0, placement.paragraph_index + 1)


def _catalog_elements(base: ArticleBaseV1) -> tuple[ArticleElementV1, ...]:
    body = publication_document_article_v5_body(base.document)
    if body is None:  # pragma: no cover - ArticleBaseV1 guarantees an article body
        raise ArticleBaseUnavailable("The publication base is not an article body")
    document = body.document
    resolved = {
        (item.element_kind, item.element_key): item
        for item in getattr(base.document, "resolved_placements", ())
    }
    element_items: list[ArticleElementV1] = []
    families: tuple[tuple[str, tuple[Any, ...]], ...] = (
        ("table", document.tables),
        ("chart", document.charts),
        ("diagram", document.diagrams),
        ("figure", document.figures),
    )
    for kind, items in families:
        for element in items:
            anchor = None
            if element.placement.kind is EnrichmentPlacementKind.AFTER_PARAGRAPH:
                # The legacy projection cannot resolve an explicit paragraph
                # anchor; only V9/V10 render such an element.
                anchor = _element_anchor(resolved, kind=kind, key=element.key)
                if anchor is None:
                    continue
            element_items.append(
                ArticleElementV1(
                    kind=kind,
                    key=element.key,
                    title=getattr(element, "title", None),
                    caption=element.caption,
                    element_sha256=publication_element_sha256(document, kind=kind, key=element.key),
                    anchor=anchor,
                )
            )
    return tuple(sorted(element_items, key=lambda item: (item.kind, item.key)))


class InvestigationArticleAnchorsService:
    """Read the canonical anchor space and rendered elements of one investigation."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def get(self, investigation_id: UUID) -> ArticleAnchorCatalogV1:
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            if investigation is None:
                raise ArticleAnchorError(
                    ArticleAnchorErrorCode.INVESTIGATION_NOT_FOUND, status_code=404
                )
            opinion = await uow.analyst_opinions.get(investigation_opinion_id(investigation_id))
            try:
                base = (
                    await resolve_pinned_publication_base(
                        uow,
                        self._artifact_store,
                        artifact_id=opinion.base_publication_artifact_id,
                        sha256=opinion.base_publication_sha256,
                    )
                    if opinion is not None
                    else await resolve_current_publication_base(
                        uow, self._artifact_store, investigation
                    )
                )
            except ArticleBaseUnavailable as exc:
                raise ArticleAnchorError(
                    ArticleAnchorErrorCode.BASE_INVALID, message=str(exc)
                ) from exc

        texts = publication_note_body_anchors(base.document)
        anchor_items: list[ArticleAnchorV1] = []
        for anchor, text in sorted(texts.items()):
            section_index, paragraph_index = _anchor_indexes(anchor)
            anchor_items.append(
                ArticleAnchorV1(
                    anchor=anchor,
                    text=text,
                    text_sha256=hashlib.sha256(text.encode("utf-8")).hexdigest(),
                    section_index=section_index,
                    paragraph_index=paragraph_index,
                )
            )
        element_items = list(_catalog_elements(base))
        # ``truncated`` reports either bound, so no omission is silent.
        truncated = (
            len(anchor_items) > MAX_ARTICLE_ANCHORS or len(element_items) > MAX_ARTICLE_ELEMENTS
        )
        return ArticleAnchorCatalogV1(
            base_publication_artifact_id=base.artifact.id,
            base_publication_version=base.artifact.version,
            base_publication_sha256=base.sha256,
            source=base.source,
            anchors=tuple(anchor_items[:MAX_ARTICLE_ANCHORS]),
            elements=tuple(element_items[:MAX_ARTICLE_ELEMENTS]),
            truncated=truncated,
        )


__all__ = [
    "ArticleAnchorCatalogV1",
    "ArticleAnchorError",
    "ArticleAnchorErrorCode",
    "ArticleAnchorV1",
    "ArticleBaseUnavailable",
    "ArticleBaseV1",
    "ArticleElementV1",
    "InvestigationArticleAnchorsService",
    "resolve_current_publication_base",
    "resolve_pinned_publication_base",
]
