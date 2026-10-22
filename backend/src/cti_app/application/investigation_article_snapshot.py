"""Resolve verified synthesis and extraction artifacts into article context."""

from __future__ import annotations

import hashlib
import ipaddress
import re
from typing import Any, Literal
from urllib.parse import urlsplit
from uuid import UUID

from cti_app.application.analyst_input_pack import (
    ANALYST_INPUT_PACK_SCHEMA_VERSION,
    AnalystInputPackV1,
)
from cti_app.application.investigation_contracts import (
    MAX_CONTEXT_CONFIRMED_IOCS,
    MAX_CONTEXT_PUBLICATION_ANCHORS,
    MAX_SUMMARY_CHARS,
    ArticleClaimV1,
    ArticleContextV1,
    ConfirmedIOCContextV1,
    PublicationAnchorV1,
)
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.research_context_builder import ArticlePassageSnapshot, ArticleSnapshot
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
)
from cti_app.domain.publication import ArtifactType
from cti_app.domain.publication_document import (
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    publication_document_from_json,
    publication_document_text_anchors,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_NARRATIVE_HASH = re.compile(
    r"(?i)(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])"
)
_MAX_ARTICLE_HASHES = 4_096
_MAX_ARTICLE_NODES = 10_000


class ProductionArticleSnapshotAdapter:
    """Read only the immutable canonical SYNTHESIS, EXTRACTION and input-pack blobs."""

    def __init__(
        self,
        uow_factory: ProductionUnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store

    async def resolve(self, synthesis_artifact_id: UUID) -> ArticleSnapshot:
        async with self._uow_factory() as uow:
            synthesis = await uow.production_artifacts.get(synthesis_artifact_id)
            if (
                synthesis is None
                or synthesis.stage is not ProductionArtifactStage.SYNTHESIS
                or synthesis.status is not ProductionArtifactStatus.VERIFIED
                or synthesis.canonical_blob_id is None
            ):
                raise ValueError("Article context requires the exact verified SYNTHESIS artifact")
            investigation = await uow.analyst_investigations.get_for_run(
                synthesis.production_run_id
            )
            if investigation is None or investigation.synthesis_artifact_id != synthesis.id:
                raise ValueError("Verified synthesis is not bound to its analyst investigation")
            pack_record = await uow.analyst_input_packs.get_for_investigation(investigation.id)
            if (
                pack_record is None
                or investigation.input_pack_blob_id != pack_record.blob_id
                or investigation.input_sha256 != pack_record.sha256
                or pack_record.schema_version != ANALYST_INPUT_PACK_SCHEMA_VERSION
            ):
                raise ValueError("Analyst input pack identity is inconsistent")
            artifacts = tuple(
                await uow.production_artifacts.list_for_run(synthesis.production_run_id)
            )
            publications = sorted(
                (
                    item
                    for item in artifacts
                    if item.stage is ProductionArtifactStage.PUBLICATION
                    and item.status is ProductionArtifactStatus.VERIFIED
                    and item.canonical_blob_id is not None
                ),
                key=lambda item: (item.version, str(item.id)),
                reverse=True,
            )
            publication = publications[0] if publications else None
            pack_blob_id = pack_record.blob_id
            pack_sha256 = pack_record.sha256
            investigation_id = investigation.id
            run_id = synthesis.production_run_id
            subject_id = synthesis.subject_id

        pack_payload = await self._artifact_store.read_json(pack_blob_id)
        if (
            hashlib.sha256(self._artifact_store.canonical_json_bytes(pack_payload)).hexdigest()
            != pack_sha256
        ):
            raise ValueError("Analyst input pack content does not match its stored SHA-256")
        pack = AnalystInputPackV1(payload=pack_payload)
        if pack.payload.get("schema_version") != ANALYST_INPUT_PACK_SCHEMA_VERSION:
            raise ValueError("Unsupported analyst input pack schema")
        if str(pack.payload.get("investigation", {}).get("id")) != str(investigation_id):
            raise ValueError("Input pack belongs to another investigation")
        pack_synthesis = pack.payload.get("synthesis_artifact", {})
        if (
            not isinstance(pack_synthesis, dict)
            or str(pack_synthesis.get("id")) != str(synthesis_artifact_id)
            or str(pack_synthesis.get("canonical_blob_id")) != str(synthesis.canonical_blob_id)
        ):
            raise ValueError("Input pack does not identify the exact synthesis artifact content")

        extraction_records: list[ProductionArtifact] = []
        async with self._uow_factory() as uow:
            for retained in pack_payload.get("retained_extraction_artifacts", []):
                if not isinstance(retained, dict):
                    continue
                try:
                    artifact_id = UUID(str(retained["artifact_id"]))
                except (KeyError, ValueError):
                    continue
                artifact = await uow.production_artifacts.get(artifact_id)
                if (
                    artifact is not None
                    and artifact.stage is ProductionArtifactStage.EXTRACTION
                    and artifact.status is ProductionArtifactStatus.VERIFIED
                    and artifact.canonical_blob_id is not None
                    and str(artifact.canonical_blob_id) == str(retained.get("canonical_blob_id"))
                    and artifact.input_hash == retained.get("input_hash")
                    and artifact.version == retained.get("version")
                ):
                    extraction_records.append(artifact)

        synthesis_payload = await self._artifact_store.read_json(synthesis.canonical_blob_id)
        synthesis_bytes = self._artifact_store.canonical_json_bytes(synthesis_payload)
        synthesis_sha256 = hashlib.sha256(synthesis_bytes).hexdigest()
        if synthesis_payload.get("subject_id") != str(subject_id):
            raise ValueError("Synthesis payload subject does not match its artifact")
        claims, narrative, chain, uncertainties = _synthesis_content(synthesis_payload)
        narrative_hashes = _extract_narrative_hashes(synthesis_payload)
        documented_samples: set[str] = set()
        ioc_rows: list[dict[str, Any]] = []
        confirmed_iocs: list[ConfirmedIOCContextV1] = []
        confirmed_iocs_truncated = False
        for indicator in pack_payload.get("file_indicators", []):
            if not isinstance(indicator, dict):
                continue
            value = indicator.get("value")
            if isinstance(value, str) and _SHA256.fullmatch(value):
                documented_samples.add(value)
                ioc_rows.append(
                    {
                        "artifact_type": "hash",
                        "value": value,
                        "status": "confirmed_ioc",
                        "source_refs": _pack_source_refs(indicator),
                    }
                )
        for artifact in extraction_records:
            if artifact.canonical_blob_id is None:
                continue
            extraction_payload = await self._artifact_store.read_json(artifact.canonical_blob_id)
            if len(narrative_hashes) < _MAX_ARTICLE_HASHES:
                narrative_hashes.update(
                    _extract_narrative_hashes(
                        extraction_payload,
                        limit=_MAX_ARTICLE_HASHES - len(narrative_hashes),
                    )
                )
            for source in extraction_payload.get("sources", []):
                if not isinstance(source, dict):
                    continue
                for indicator in source.get("indicators", []):
                    if (
                        not isinstance(indicator, dict)
                        or indicator.get("indicator_status") != "confirmed_ioc"
                    ):
                        continue
                    value = indicator.get("value")
                    if not isinstance(value, str):
                        continue
                    refs = tuple(str(item) for item in indicator.get("source_document_ids", []))
                    ioc_rows.append(
                        {
                            "artifact_type": str(indicator.get("artifact_type", "other")),
                            "value": value,
                            "status": "confirmed_ioc",
                            "context": str(indicator.get("context", "")),
                            "evidence_quote": str(indicator.get("evidence_quote", "")),
                            "source_refs": refs,
                            "extraction_artifact_id": str(artifact.id),
                            "extraction_artifact_version": artifact.version,
                        }
                    )
                    inventory_item = _confirmed_ioc_context(indicator, artifact)
                    if inventory_item is not None:
                        if len(confirmed_iocs) < MAX_CONTEXT_CONFIRMED_IOCS:
                            confirmed_iocs.append(inventory_item)
                        else:
                            confirmed_iocs_truncated = True
                    if indicator.get("artifact_type") == "hash" and _SHA256.fullmatch(value):
                        documented_samples.add(value)
        documented_iocs = tuple({"kind": "documented_ioc", **row} for row in ioc_rows[:128])
        known_chain = (*documented_iocs, *chain[:128])
        short_summary = "\n\n".join(narrative)
        if len(short_summary) > MAX_SUMMARY_CHARS:
            short_summary = short_summary[: MAX_SUMMARY_CHARS - 1].rstrip() + "…"
        article = ArticleContextV1(
            short_summary=short_summary,
            claims=tuple(claims),
            documented_samples=tuple(sorted(documented_samples)[:256]),
            known_chain=known_chain,
            open_questions=tuple(uncertainties),
            publication_anchors=(
                await self._publication_anchors(publication.canonical_blob_id)
                if publication is not None
                else ()
            ),
        )
        policy = pack_payload.get("policy", {})
        return ArticleSnapshot(
            subject_id=subject_id,
            production_run_id=run_id,
            synthesis_artifact_id=synthesis_artifact_id,
            synthesis_sha256=synthesis_sha256,
            article=article,
            base_publication_artifact_id=publication.id if publication else None,
            base_publication_sha256=(
                await self._sha256_blob(publication.canonical_blob_id) if publication else None
            ),
            tlp=policy.get("tlp") if isinstance(policy, dict) else None,
            do_not_submit=bool(policy.get("do_not_submit", True))
            if isinstance(policy, dict)
            else True,
            external_llm_allowed=bool(policy.get("external_llm_allowed", False))
            if isinstance(policy, dict)
            else False,
            narrative_hashes=tuple(sorted(narrative_hashes)),
            confirmed_iocs=tuple(confirmed_iocs),
            confirmed_iocs_truncated=confirmed_iocs_truncated,
            article_passages=_synthesis_passages(synthesis_payload),
        )

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()
    ) -> tuple[ArticleClaimV1, ...]:
        async with self._uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            return ()
        snapshot = await self.resolve(investigation.synthesis_artifact_id)
        claims = snapshot.article.claims
        if not claim_ids:
            return claims
        wanted = set(claim_ids)
        return tuple(item for item in claims if item.id in wanted)

    async def _sha256_blob(self, blob_id: UUID | None) -> str:
        if blob_id is None:
            raise ValueError("Verified publication has no canonical payload")
        payload = await self._artifact_store.read_json(blob_id)
        return hashlib.sha256(self._artifact_store.canonical_json_bytes(payload)).hexdigest()

    async def _publication_anchors(self, blob_id: UUID | None) -> tuple[PublicationAnchorV1, ...]:
        if blob_id is None:
            return ()
        payload = await self._artifact_store.read_json(blob_id)
        if payload.get("schema_version") not in {"4", "5", "6"}:
            return ()
        document = publication_document_from_json(payload)
        if isinstance(document, PublicationDocumentV6):
            publication = document.base_publication
        elif isinstance(document, PublicationDocumentV5):
            publication = document
        elif isinstance(document, PublicationDocumentV4):
            anchors = publication_document_text_anchors(document)
            publication = None
        else:  # pragma: no cover - exhaustive union guard
            return ()
        if publication is not None:
            anchors = publication_document_text_anchors(
                publication.document,
                references=publication.references,
            )
        paragraph_anchors = {
            anchor: text
            for anchor, text in anchors.items()
            if re.fullmatch(r"lead:\d{4}|section:\d+:paragraph:\d{4}", anchor)
        }
        result = []
        for anchor, text in sorted(paragraph_anchors.items())[:MAX_CONTEXT_PUBLICATION_ANCHORS]:
            excerpt = " ".join(text.split())[:160].rstrip()
            if excerpt:
                result.append(PublicationAnchorV1(anchor=anchor, excerpt=excerpt))
        return tuple(result)


def _synthesis_content(
    payload: dict[str, Any],
) -> tuple[list[ArticleClaimV1], list[str], list[dict[str, Any]], list[str]]:
    if not isinstance(payload.get("title"), str):
        raise ValueError("Canonical synthesis has no title")
    claims: list[ArticleClaimV1] = []
    narrative = [payload["title"]]
    chain: list[dict[str, Any]] = []
    uncertainties: list[str] = []

    def add_claim(text: Any, refs: Any, claim_id: str) -> None:
        if not isinstance(text, str) or not text.strip():
            return
        citation_refs = (
            tuple(_citation_ref(item) for item in refs if isinstance(item, dict))
            if isinstance(refs, list)
            else ()
        )
        for part_index, offset in enumerate(range(0, len(text), 3_900), 1):
            claims.append(
                ArticleClaimV1(
                    id=f"{claim_id}.{part_index}",
                    statement=text[offset : offset + 3_900],
                    source_refs=citation_refs,
                )
            )

    for index, paragraph in enumerate(payload.get("lead", []), 1):
        if not isinstance(paragraph, dict):
            continue
        text = paragraph.get("text")
        add_claim(text, paragraph.get("evidence_refs", []), f"L{index}")
        if isinstance(text, str):
            narrative.append(text)
    for section_index, section in enumerate(payload.get("sections", []), 1):
        if not isinstance(section, dict):
            continue
        kind = str(section.get("kind", "other"))
        heading = str(section.get("heading", ""))
        for paragraph_index, paragraph in enumerate(section.get("paragraphs", []), 1):
            if not isinstance(paragraph, dict):
                continue
            text = paragraph.get("text")
            claim_id = f"S{section_index}P{paragraph_index}"
            add_claim(text, paragraph.get("evidence_refs", []), claim_id)
            if isinstance(text, str):
                narrative.append(f"{heading}: {text}" if heading else text)
                if kind == "infection_chain":
                    chain.append(
                        {
                            "kind": "infection_chain",
                            "statement": text,
                            "source_refs": [
                                _citation_ref(item)
                                for item in paragraph.get("evidence_refs", [])
                                if isinstance(item, dict)
                            ],
                        }
                    )
    for index, item in enumerate(payload.get("timeline", []), 1):
        if not isinstance(item, dict):
            continue
        text = item.get("text")
        if isinstance(text, str):
            claims.append(
                ArticleClaimV1(
                    id=f"T{index}",
                    statement=text,
                    source_refs=tuple(
                        _citation_ref(ref)
                        for ref in item.get("evidence_refs", [])
                        if isinstance(ref, dict)
                    ),
                )
            )
            narrative.append(text)
            chain.append({"kind": "timeline", "date": item.get("date_text"), "statement": text})
    for item in payload.get("uncertainties", []):
        if isinstance(item, dict) and isinstance(item.get("text"), str):
            uncertainties.append(item["text"])
            narrative.append(f"Incertitude: {item['text']}")
    return claims[:128], narrative, chain[:256], uncertainties[:128]


def _synthesis_passages(payload: dict[str, Any]) -> tuple[ArticlePassageSnapshot, ...]:
    """Retain verbatim lead, section paragraphs, and timeline claims for task selection."""
    result: list[ArticlePassageSnapshot] = []

    def add(text: object, claim_id: str, *, section_index: int | None, heading: str) -> None:
        if isinstance(text, str) and text.strip():
            result.append(
                ArticlePassageSnapshot(
                    claim_id=claim_id,
                    section_index=section_index,
                    heading=heading.strip(),
                    text=text,
                )
            )

    for index, paragraph in enumerate(payload.get("lead", []), 1):
        if isinstance(paragraph, dict):
            add(paragraph.get("text"), f"L{index}.1", section_index=None, heading="Introduction")
    for section_index, section in enumerate(payload.get("sections", []), 1):
        if not isinstance(section, dict):
            continue
        heading = str(section.get("heading", ""))
        for paragraph_index, paragraph in enumerate(section.get("paragraphs", []), 1):
            if isinstance(paragraph, dict):
                add(
                    paragraph.get("text"),
                    f"S{section_index}P{paragraph_index}.1",
                    section_index=section_index,
                    heading=heading,
                )
    for index, item in enumerate(payload.get("timeline", []), 1):
        if isinstance(item, dict):
            add(
                item.get("text"),
                f"T{index}",
                section_index=None,
                heading="Chronologie",
            )
    return tuple(result)


def _citation_ref(value: dict[str, Any]) -> str:
    return ":".join(
        str(value.get(key, "")) for key in ("source_document_id", "kind", "evidence_key")
    )[:256]


def _pack_source_refs(indicator: dict[str, Any]) -> tuple[str, ...]:
    provenance = indicator.get("provenance")
    if not isinstance(provenance, dict):
        return ()
    refs = provenance.get("source_ids", ())
    return tuple(str(item) for item in refs if isinstance(item, str))[:64]


_SENSITIVE_URL_QUERY = re.compile(
    r"(?i)[?&](?:x-amz-[^=]+|x-goog-[^=]+|signature|sig|access_token|api[_-]?key|"
    r"token|secret|password|authorization|credential)="
)


def _confirmed_ioc_context(
    indicator: dict[str, Any], artifact: ProductionArtifact
) -> ConfirmedIOCContextV1 | None:
    if indicator.get("indicator_status") != "confirmed_ioc":
        return None
    raw_value = indicator.get("value")
    try:
        artifact_type = ArtifactType(str(indicator.get("artifact_type", "")))
        if not isinstance(raw_value, str):
            return None
        value = normalize_indicator_value(raw_value, artifact_type)
    except (TypeError, ValueError):
        return None

    indicator_type: Literal["md5", "sha1", "sha256", "ipv4", "domain", "url"]
    if artifact_type is ArtifactType.HASH:
        if len(value) == 32:
            indicator_type = "md5"
        elif len(value) == 40:
            indicator_type = "sha1"
        elif len(value) == 64:
            indicator_type = "sha256"
        else:
            return None
        if re.fullmatch(r"[0-9a-f]+", value) is None:
            return None
    elif artifact_type is ArtifactType.IP:
        try:
            if ipaddress.ip_address(value).version != 4:
                return None
        except ValueError:
            return None
        indicator_type = "ipv4"
    elif artifact_type is ArtifactType.DOMAIN:
        if not value or len(value) > 253:
            return None
        indicator_type = "domain"
    elif artifact_type is ArtifactType.URL:
        parts = urlsplit(value)
        if (
            not parts.scheme
            or not parts.hostname
            or parts.username is not None
            or parts.password is not None
            or _SENSITIVE_URL_QUERY.search(value)
            or len(value) > 2_048
        ):
            return None
        indicator_type = "url"
    else:
        return None

    raw_source_ids = indicator.get("source_document_ids", ())
    if not isinstance(raw_source_ids, list | tuple) or len(raw_source_ids) > 16:
        return None
    try:
        source_document_ids = tuple(UUID(str(item)) for item in raw_source_ids)
    except (TypeError, ValueError):
        return None
    if not source_document_ids:
        return None
    return ConfirmedIOCContextV1(
        indicator_type=indicator_type,
        value=value,
        extraction_artifact_id=artifact.id,
        extraction_artifact_version=artifact.version,
        source_document_ids=source_document_ids,
    )


def _extract_narrative_hashes(payload: Any, *, limit: int = _MAX_ARTICLE_HASHES) -> set[str]:
    """Find bounded MD5/SHA-1/SHA-256 identities across article text and sources."""
    hashes: set[str] = set()
    pending = [payload]
    visited = 0
    while pending and visited < _MAX_ARTICLE_NODES and len(hashes) < limit:
        value = pending.pop()
        visited += 1
        if isinstance(value, str):
            for match in _NARRATIVE_HASH.finditer(value[:65_536]):
                candidate = match.group(0).lower()
                family = {32: "md5", 40: "sha1", 64: "sha256"}.get(len(candidate))
                if family is not None:
                    hashes.add(candidate)
                    if len(hashes) >= limit:
                        break
        elif isinstance(value, dict):
            pending.extend(list(value.values())[:_MAX_ARTICLE_NODES])
            pending.extend(list(value.keys())[:_MAX_ARTICLE_NODES])
        elif isinstance(value, list | tuple):
            pending.extend(value[:_MAX_ARTICLE_NODES])
    return hashes


__all__ = ["ProductionArticleSnapshotAdapter"]
