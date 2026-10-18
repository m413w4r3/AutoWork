"""Pure projection of canonical publication documents into Typst input data."""

from __future__ import annotations

import hashlib
import ipaddress
import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, cast
from urllib.parse import urlsplit
from uuid import UUID

from cti_app.application.investigation_figures import (
    build_investigation_chain_figure,
    build_investigation_graph_figure,
    build_publication_timeline_figure,
)
from cti_app.application.pdf_mapping import MAPPING_RENDERER_VERSION, target_descriptor
from cti_app.application.typst_bundle import hash_bundle_contents, resolve_bundle_files
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlockType,
)
from cti_app.domain.investigation_graph import InvestigationGraph
from cti_app.domain.media_assets import MediaAssetKind
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.domain.production_synthesis import ProductionSynthesisV1
from cti_app.domain.publication import (
    ArtifactType,
    PublicationEvidenceRefV1,
    PublicationSourceV1,
)
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    PublicationChartV1,
    PublicationDetectionReason,
    PublicationDetectionStatus,
    PublicationDetectionV1,
    PublicationDiagramV1,
    PublicationDocumentV4,
    PublicationDocumentV5,
    PublicationDocumentV6,
    PublicationIOCDisposition,
    PublicationSourceFigureV1,
    PublicationTableV1,
    publication_document_text_anchors,
    publication_document_v5_sha256,
)
from cti_app.domain.semantic_annotation import (
    SEMANTIC_ROLE_TYPST_FUNCTION_V1,
    lead_paragraph_anchor,
    section_paragraph_anchor,
    timeline_anchor,
)

_RENDER_DATA_SCHEMA_VERSION = "typst-publication-model-v24-canonical-investigation-figures"
_PUBLICATION_RENDERER_MANIFEST = "renderer-manifest.json"
_FRENCH_MONTH_NAMES = (
    "janvier",
    "février",
    "mars",
    "avril",
    "mai",
    "juin",
    "juillet",
    "août",
    "septembre",
    "octobre",
    "novembre",
    "décembre",
)
_MEDIA_EXTENSIONS = {
    "image/svg+xml": ".svg",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
}
_INDICATOR_KEYS = {
    ArtifactType.IP: "ips",
    ArtifactType.DOMAIN: "domains",
    ArtifactType.URL: "urls",
    ArtifactType.EMAIL: "emails",
    ArtifactType.HASH: "hashes",
}
_IOC_DISPOSITION_LABELS = {
    PublicationIOCDisposition.RETAINED: "Retenu",
    PublicationIOCDisposition.UNCERTAIN: "Incertain",
    PublicationIOCDisposition.EXCLUDED: "Écarté",
}
_BREAKABLE_SEMANTIC_ROLES = frozenset({"ioc", "path", "command", "technical_literal"})
_TYPOGRAPHIC_BREAK_INTERVAL = 8
_TYPOGRAPHIC_BREAK_THRESHOLD = 32
_TABLE_TYPOGRAPHIC_BREAK_INTERVAL = 6
_TECHNICAL_BREAK_SEPARATORS = frozenset(".\\/-_")
_TABLE_SEMANTIC_STYLE_BY_STYLE = {
    "semantic-command": "semantic-table-command",
    "semantic-ioc": "semantic-table-ioc",
    "semantic-path": "semantic-table-path",
    "semantic-technical-literal": "semantic-table-technical",
}
_TABLE_SHA256 = re.compile(r"^(?:sha-?256(?:\s*:\s*|\s+))?[0-9a-f]{64}$", re.IGNORECASE)
_TABLE_DOMAIN = re.compile(
    r"^(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}\.?$",
    re.IGNORECASE,
)
_TABLE_WINDOWS_PATH = re.compile(r"^[a-z]:[\\/].+", re.IGNORECASE)
_TABLE_WINDOWS_REGISTRY_PATH = re.compile(
    r"^(?:HK(?:CU|LM|CR|U|CC)|HKEY_(?:CLASSES_ROOT|CURRENT_USER|LOCAL_MACHINE|USERS|CURRENT_CONFIG))\\",
    re.IGNORECASE,
)
_OPINION_BLOCK_HEADINGS = {
    OpinionBlockType.KEY_FINDINGS: "Apports de l'enquête",
    OpinionBlockType.ADDITIONAL_SAMPLES: "Échantillons complémentaires",
    OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES: "Chaîne d'infection et capacités",
    OpinionBlockType.NETWORK_AND_INFRASTRUCTURE: "Activité réseau et infrastructure",
    OpinionBlockType.CLAIM_VERIFICATION: "Vérification des affirmations",
    OpinionBlockType.PROPOSED_DETECTIONS: "Détections proposées",
    OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS: "Incertitudes et prochaines pistes",
}
_OBSERVATION_STATE_LABELS = {
    AnalystObservationState.OBSERVED: "Observation étayée",
    AnalystObservationState.INFERRED: "Inférence de l'analyste",
    AnalystObservationState.UNRESOLVED: "Point non résolu",
}
_OBSERVATION_RELATION_LABELS = {
    AnalystObservationRelation.CONFIRMS: "confirme",
    AnalystObservationRelation.REFUTES: "contredit",
    AnalystObservationRelation.QUALIFIES: "nuance",
    AnalystObservationRelation.UNRESOLVED: "ne permet pas de trancher",
}


def _breakable_typst_display_text(
    value: str,
    *,
    interval: int = _TYPOGRAPHIC_BREAK_INTERVAL,
    break_at_technical_separators: bool = False,
) -> dict[str, Any]:
    """Keep exact text and add wrap points only inside long unbroken tokens."""
    tokens = re.findall(r"\s+|\S+", value)

    def should_break(token: str) -> bool:
        if token.isspace() or len(token) <= interval:
            return False
        return len(token) > _TYPOGRAPHIC_BREAK_THRESHOLD or (
            break_at_technical_separators
            and any(character in _TECHNICAL_BREAK_SEPARATORS for character in token)
        )

    if not any(should_break(token) for token in tokens):
        return {"text": value, "break_chunks": []}

    chunks: list[str] = []
    break_after: list[bool] = []
    for token in tokens:
        pieces = (
            [token[index : index + interval] for index in range(0, len(token), interval)]
            if should_break(token)
            else [token]
        )
        for index, piece in enumerate(pieces):
            if chunks:
                break_after.append(index > 0)
            chunks.append(piece)

    return {"text": value, "break_chunks": chunks, "break_after": break_after}


def _indicator_values(groups: tuple[Any, ...]) -> dict[str, list[dict[str, Any]]]:
    """Merge display IOC values and deduplicate without changing canonical records."""
    result: dict[str, list[dict[str, Any]]] = {
        key: [] for key in ("ips", "domains", "urls", "emails", "hashes")
    }
    seen: dict[str, set[str]] = {key: set() for key in result}
    for group in groups:
        key = _INDICATOR_KEYS[group.artifact_type]
        for indicator in group.indicators:
            normalized_value = indicator.normalized_value.casefold()
            if normalized_value in seen[key]:
                continue
            seen[key].add(normalized_value)
            result[key].append(_breakable_typst_display_text(indicator.value))
    return result


_DETECTION_REASON_LABELS = {
    PublicationDetectionReason.CORPUS_EMPTY: "corpus de test vide",
    PublicationDetectionReason.POSITIVES_MISSING: "positifs réels indisponibles",
    PublicationDetectionReason.GOODWARE_MISSING: "corpus bénin vide",
    PublicationDetectionReason.SAMPLE_BYTES_MISSING: "octets d'échantillon indisponibles",
    PublicationDetectionReason.SYNTHETIC_POSITIVES: "positifs uniquement synthétiques",
    PublicationDetectionReason.FALSE_NEGATIVES: "positifs réels non tous détectés",
    PublicationDetectionReason.FALSE_POSITIVES: "faux positifs observés dans le corpus bénin",
    PublicationDetectionReason.EXECUTION_INCOMPLETE: "tests incomplets",
    PublicationDetectionReason.VALIDATED: "positifs réels et corpus bénin testés",
}


def _publication_detection_section(detection: PublicationDetectionV1) -> dict[str, Any]:
    if not detection.validation_status_is_coherent:
        raise ValueError("Published YARA validation status differs from its canonical report")
    if detection.validation_status is PublicationDetectionStatus.VALIDATED:
        banner = "VALIDÉE (positifs réels et corpus bénin testés)"
        reason = None
    else:
        banner = "CANDIDATE — NON VALIDÉE"
        reason = (
            "non testée"
            if detection.validation_status is PublicationDetectionStatus.UNTESTED
            else _DETECTION_REASON_LABELS[detection.validation_reason]
        )
    code_lines = detection.source.splitlines()
    if not code_lines:
        code_lines = [""]
    result: dict[str, Any] = {
        "type": "detection",
        "heading": "DÉTECTION",
        "candidate_id": str(detection.candidate_id),
        "version_id": str(detection.version_id),
        "version": f"v{detection.version}",
        "source_sha256": _breakable_typst_display_text(
            detection.source_sha256, break_at_technical_separators=True
        ),
        "validation_status": detection.validation_status.value,
        "validation_banner": banner,
        "validation_reason": reason,
        "rule_validation_status": detection.rule_validation_status.value,
        "positive_real_count": detection.positive_real_count,
        "positive_synthetic_count": detection.positive_synthetic_count,
        "goodware_tested_count": detection.goodware_tested_count,
        "false_positive_count": detection.false_positive_count,
        "engine_version": detection.engine_version,
        "coverage_statement": detection.coverage_statement,
        "limitations": list(detection.limitations),
        "created_date": _display_date(None, detection.created_at.date()),
        "tested_date": (
            _display_date(None, detection.tested_at.date())
            if detection.tested_at is not None
            else None
        ),
        "retrohunt_submitted": detection.retrohunt_submitted,
        "code_lines": [
            _breakable_typst_display_text(
                line,
                interval=48,
                break_at_technical_separators=True,
            )
            for line in code_lines
        ],
    }
    if detection.test_report_observation_id is not None:
        result["test_report_observation_id"] = str(detection.test_report_observation_id)
        result["test_report_sha256"] = _breakable_typst_display_text(
            detection.test_report_sha256 or "", break_at_technical_separators=True
        )
    return result


def publication_content_section_types(
    value: CanonicalPublicationDocument | TypstDraftPreviewDocument,
) -> tuple[str, ...]:
    """Return the ordered sections projected from canonical publication data."""
    draft_preview = value if isinstance(value, TypstDraftPreviewDocument) else None
    publication_v6 = value if isinstance(value, PublicationDocumentV6) else None
    publication_v5 = (
        publication_v6.base_publication
        if publication_v6 is not None
        else draft_preview.base_publication
        if draft_preview is not None
        else value
    )
    if isinstance(publication_v5, PublicationDocumentV5):
        document = publication_v5.document
    elif isinstance(value, PublicationDocumentV4):
        document = value
    else:
        raise ValueError("Unsupported canonical publication document")

    section_types = ["references", "synthesis"]
    timeline_figure = (
        build_publication_timeline_figure(publication_v5)
        if isinstance(publication_v5, PublicationDocumentV5)
        else None
    )
    if draft_preview is not None and draft_preview.synthesis is not None:
        has_chronology = bool(draft_preview.synthesis.timeline) or timeline_figure is not None
        has_limitations = bool(draft_preview.synthesis.uncertainties)
    else:
        has_chronology = (
            bool(publication_v5.timeline)
            if isinstance(publication_v5, PublicationDocumentV5)
            else False
        )
        has_chronology = has_chronology or timeline_figure is not None
        has_limitations = False
    if has_chronology:
        section_types.append("chronology")

    opinion = (
        publication_v6.analyst_opinion
        if publication_v6 is not None
        else draft_preview.analyst_opinion
        if draft_preview is not None
        else None
    )
    has_rendered_opinion = (
        publication_v6 is not None and publication_v6.rendered_opinion is not None
    )
    if has_limitations:
        section_types.append("limitations")
    if opinion is not None and (has_rendered_opinion or draft_preview is not None):
        section_types.append("analyst_opinion")
    if publication_v6 is not None and publication_v6.detection is not None:
        section_types.append("detection")

    original_indicator_groups = (
        publication_v5.original_indicators
        if isinstance(publication_v5, PublicationDocumentV5)
        else ()
    )
    ioc_details = (
        publication_v5.ioc_details if isinstance(publication_v5, PublicationDocumentV5) else ()
    )
    if (
        any(group.indicators for group in document.indicators)
        or any(group.indicators for group in original_indicator_groups)
        or ioc_details
    ):
        section_types.append("technical_annex")
    return tuple(section_types)


def _table_cell_typst_spans(value: str) -> list[dict[str, Any]]:
    """Format exact technical table values and add invisible wrap points to long cells."""
    style = "semantic-plain"
    if _TABLE_SHA256.fullmatch(value):
        style = "semantic-table-technical"
    elif (
        value.startswith(("/", "\\"))
        or _TABLE_WINDOWS_PATH.fullmatch(value)
        or _TABLE_WINDOWS_REGISTRY_PATH.match(value)
    ):
        style = "semantic-table-path"
    elif value.startswith(("http://", "https://")) and not any(char.isspace() for char in value):
        style = "semantic-table-ioc"
    elif _TABLE_DOMAIN.fullmatch(value):
        style = "semantic-table-ioc"
    else:
        try:
            ipaddress.ip_address(value)
        except ValueError:
            pass
        else:
            style = "semantic-table-ioc"
    return [
        {
            "style": style,
            **_breakable_typst_display_text(
                value,
                interval=_TABLE_TYPOGRAPHIC_BREAK_INTERVAL,
                break_at_technical_separators=True,
            ),
        }
    ]


def _table_cell_render_spans(
    value: str,
    semantic_cell_spans: list[dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    if semantic_cell_spans is None:
        return _table_cell_typst_spans(value)
    return [
        {
            "style": _TABLE_SEMANTIC_STYLE_BY_STYLE.get(span["style"], span["style"]),
            **_breakable_typst_display_text(
                span["text"],
                interval=_TABLE_TYPOGRAPHIC_BREAK_INTERVAL,
                break_at_technical_separators=True,
            ),
        }
        for span in semantic_cell_spans
    ]


@dataclass(frozen=True, slots=True)
class TypstMediaRef:
    asset_id: UUID
    expected_kind: MediaAssetKind
    expected_mime_type: str
    expected_sha256: str | None
    expected_byte_size: int | None
    media_path: str


@dataclass(frozen=True, slots=True)
class TypstRenderSource:
    source_bytes: bytes
    source_sha256: str
    render_data_bytes: bytes
    render_data_sha256: str
    media_refs: tuple[TypstMediaRef, ...]
    entrypoint_relative_path: str = "RENDERER/publication.typ"
    render_data_relative_path: str = "RENDERER/render-data.json"
    mapping_targets: tuple[dict[str, Any], ...] = ()
    mapping_unmapped: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True, slots=True)
class TypstPublicationModelV2:
    title: str
    content_sections: list[dict[str, Any]]
    media_refs: tuple[TypstMediaRef, ...]
    draft_watermark: str | None = None


@dataclass(frozen=True, slots=True)
class TypstDraftPreviewDocument:
    """Ephemeral V5 plus draft opinion projection; it is never a canonical V6."""

    base_publication: PublicationDocumentV5
    analyst_opinion: AnalystOpinionV1
    synthesis: ProductionSynthesisV1 | None = None
    infrastructure_graph: InvestigationGraph | None = None

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base_publication, name)

    def __post_init__(self) -> None:
        if self.analyst_opinion.status is not AnalystOpinionStatus.DRAFT:
            raise ValueError("Only draft opinions can use the draft preview projection")
        if self.synthesis is not None and (
            self.synthesis.subject_id != self.base_publication.subject_id
            or self.synthesis.title != self.base_publication.title
            or self.synthesis.publication_language != self.base_publication.publication_language
        ):
            raise ValueError("Draft preview synthesis does not match the pinned publication")
        if self.infrastructure_graph is not None and (
            self.infrastructure_graph.investigation_id != self.analyst_opinion.investigation_id
            or self.infrastructure_graph.publication_sha256
            != self.analyst_opinion.base_publication_sha256
        ):
            raise ValueError("Draft preview infrastructure graph is not pinned to its V5 opinion")
        if publication_document_v5_sha256(self.base_publication) != (
            self.analyst_opinion.base_publication_sha256
        ):
            raise ValueError("Draft opinion does not identify this exact V5 publication")
        anchors = publication_document_text_anchors(
            self.base_publication.document,
            references=self.base_publication.references,
        )
        if any(
            item.anchor not in anchors or not item.anchor.startswith(("lead:", "section:"))
            for item in self.analyst_opinion.observations
        ):
            raise ValueError("Draft opinion observation anchor is stale or unsupported")


@dataclass(frozen=True, slots=True)
class TemplateFile:
    relative_path: str
    content: bytes


@dataclass(frozen=True, slots=True)
class TypstTemplateBundle:
    template_version: str
    sha256: str
    files: tuple[TemplateFile, ...]


class TypstRenderer:
    """Build deterministic data and return the fixed production entrypoint."""

    def render(
        self,
        document: CanonicalPublicationDocument | TypstDraftPreviewDocument,
        template_bundle: TypstTemplateBundle,
        *,
        target_artifact_identities: Mapping[str, Mapping[str, Any]] | None = None,
        infrastructure_graph_media_ref: TypstMediaRef | None = None,
    ) -> TypstRenderSource:
        entrypoint = next(
            (
                file
                for file in template_bundle.files
                if file.relative_path == "RENDERER/publication.typ"
            ),
            None,
        )
        if entrypoint is None:
            raise TemplateBundleInvalidError(
                "Typst renderer manifest does not include RENDERER/publication.typ"
            )
        source_bytes = entrypoint.content
        model = project_publication_to_typst_model(
            document, infrastructure_graph_media_ref=infrastructure_graph_media_ref
        )
        target_descriptors: tuple[dict[str, Any], ...] = ()
        unmapped: tuple[dict[str, Any], ...] = ()
        article_title_target_id: str | None = None
        if target_artifact_identities is not None:
            target_descriptors, unmapped, article_title_target_id = instrument_publication_targets(
                model,
                article_title_identity=target_artifact_identities.get("article_title"),
                enrichment_identity=target_artifact_identities.get("enrichment"),
            )

        render_data: dict[str, Any] = {
            "schema_version": _RENDER_DATA_SCHEMA_VERSION,
            "language": document.publication_language,
            "title": model.title,
            "content_sections": model.content_sections,
        }
        if article_title_target_id is not None:
            render_data["article_title_target_id"] = article_title_target_id
        if target_descriptors or unmapped:
            render_data["mapping_instrumentation_version"] = MAPPING_RENDERER_VERSION
        if model.draft_watermark is not None:
            render_data["draft_watermark"] = model.draft_watermark
        render_data_bytes = json.dumps(
            render_data,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return TypstRenderSource(
            source_bytes=source_bytes,
            source_sha256=hashlib.sha256(source_bytes).hexdigest(),
            render_data_bytes=render_data_bytes,
            render_data_sha256=hashlib.sha256(render_data_bytes).hexdigest(),
            media_refs=model.media_refs,
            mapping_targets=target_descriptors,
            mapping_unmapped=unmapped,
        )


def instrument_publication_targets(
    model: TypstPublicationModelV2,
    *,
    article_title_identity: Mapping[str, Any] | None,
    enrichment_identity: Mapping[str, Any] | None,
) -> tuple[tuple[dict[str, Any], ...], tuple[dict[str, Any], ...], str | None]:
    """Attach target IDs using the canonical artifact that owns each element."""
    descriptors: list[dict[str, Any]] = []
    unmapped_counts: dict[tuple[str, str], int] = {}

    def record_unmapped(block_type: str, reason: str, count: int = 1) -> None:
        key = (block_type, reason)
        unmapped_counts[key] = unmapped_counts.get(key, 0) + count

    def descriptor_for(
        identity: Mapping[str, Any] | None, block_type: str, block_key: str
    ) -> dict[str, Any] | None:
        if identity is None:
            return None
        subject_id = identity.get("subject_id")
        artifact_id = identity.get("artifact_id")
        artifact_version = identity.get("artifact_version")
        artifact_input_hash = identity.get("artifact_input_hash")
        if (
            not isinstance(subject_id, str)
            or not isinstance(artifact_id, str)
            or not isinstance(artifact_version, int)
            or not isinstance(artifact_input_hash, str)
        ):
            return None
        return target_descriptor(
            subject_id=subject_id,
            artifact_id=artifact_id,
            artifact_version=artifact_version,
            artifact_input_hash=artifact_input_hash,
            block_type=block_type,
            block_key=block_key,
        )

    title = descriptor_for(article_title_identity, "article_title", "title")
    if title is None:
        article_title_target_id = None
        record_unmapped("article_title", "canonical_owner_unavailable")
    else:
        descriptors.append(title)
        article_title_target_id = str(title["target_id"])

    for section in model.content_sections:
        blocks = section.get("blocks", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type in {"table", "diagram", "figure", "chart"}:
                key = block.get("key")
                canonical_type = "source_figure" if block_type == "figure" else block_type
                if isinstance(key, str) and key:
                    descriptor = descriptor_for(enrichment_identity, canonical_type, key)
                    if descriptor is None:
                        record_unmapped(canonical_type, "canonical_owner_unavailable")
                    else:
                        block["target_id"] = descriptor["target_id"]
                        descriptors.append(descriptor)
                    if block_type == "table":
                        rows = block.get("rows", [])
                        columns = block.get("columns", [])
                        cell_count = len(columns) if isinstance(columns, list) else 0
                        if isinstance(rows, list):
                            cell_count += sum(len(row) for row in rows if isinstance(row, list))
                        record_unmapped("table_cell", "no_stable_identity", cell_count)
                else:
                    record_unmapped("enrichment_block", "no_stable_identity")
            elif block_type == "paragraph":
                record_unmapped("paragraph", "no_stable_identity")
            elif block_type == "section_heading":
                record_unmapped("section", "no_stable_identity")
    unmapped = tuple(
        {
            "block_type": block_type,
            "reason": reason,
            "count": count,
        }
        for (block_type, reason), count in sorted(unmapped_counts.items())
        if count > 0
    )
    return tuple(descriptors), unmapped, article_title_target_id


def project_publication_to_typst_model(
    value: CanonicalPublicationDocument | TypstDraftPreviewDocument,
    *,
    infrastructure_graph_media_ref: TypstMediaRef | None = None,
) -> TypstPublicationModelV2:
    """Project a canonical publication while keeping semantic content as data."""
    draft_preview = value if isinstance(value, TypstDraftPreviewDocument) else None
    publication_v6 = value if isinstance(value, PublicationDocumentV6) else None
    publication_v5 = (
        publication_v6.base_publication
        if publication_v6 is not None
        else draft_preview.base_publication
        if draft_preview is not None
        else value
    )
    rendered_opinion = publication_v6.rendered_opinion if publication_v6 is not None else None
    infrastructure_graph = (
        publication_v6.infrastructure_graph
        if publication_v6 is not None
        else draft_preview.infrastructure_graph
        if draft_preview is not None
        else None
    )
    opinion = (
        publication_v6.analyst_opinion
        if publication_v6 is not None
        else draft_preview.analyst_opinion
        if draft_preview is not None
        else None
    )
    detection = publication_v6.detection if publication_v6 is not None else None
    if isinstance(publication_v5, PublicationDocumentV5):
        document = publication_v5.document
        semantic_by_anchor = {item.anchor: item for item in publication_v5.semantic_text.paragraphs}
        reference_entries = publication_v5.references
        original_indicator_groups = publication_v5.original_indicators
        ioc_details = publication_v5.ioc_details
    elif isinstance(value, PublicationDocumentV4):
        document = value
        semantic_by_anchor = {}
        reference_entries = ()
        original_indicator_groups = ()
        ioc_details = ()
    else:
        raise ValueError("Unsupported canonical publication document")

    def semantic_spans(anchor: str, text: str) -> list[dict[str, str]] | None:
        paragraph = semantic_by_anchor.get(anchor)
        if paragraph is None:
            return None
        if paragraph.text != text:
            raise ValueError(f"Semantic text differs from publication content at {anchor}")
        result: list[dict[str, Any]] = []
        for span in paragraph.spans:
            projected_span: dict[str, Any] = {
                "style": SEMANTIC_ROLE_TYPST_FUNCTION_V1[span.role],
                "text": span.text,
            }
            if span.role.value in _BREAKABLE_SEMANTIC_ROLES:
                breakable_text = _breakable_typst_display_text(span.text)
                projected_span["break_chunks"] = breakable_text["break_chunks"]
                if "break_after" in breakable_text:
                    projected_span["break_after"] = breakable_text["break_after"]
            result.append(projected_span)
        return result

    def rich_block(block: dict[str, Any], field_name: str, anchor: str, text: str) -> None:
        spans = semantic_spans(anchor, text)
        if spans is not None:
            block[field_name] = spans

    media_refs_by_id: dict[UUID, TypstMediaRef] = {}
    rich_by_placement: dict[tuple[EnrichmentPlacementKind, int | None], list[dict[str, Any]]] = {}

    def media_ref(
        *,
        asset_id: UUID,
        expected_kind: MediaAssetKind,
        expected_mime_type: str,
        expected_sha256: str | None,
        expected_byte_size: int | None,
    ) -> TypstMediaRef:
        extension = _MEDIA_EXTENSIONS.get(expected_mime_type)
        if extension is None:
            raise ValueError(f"Unsupported Typst media MIME type: {expected_mime_type}")
        candidate = TypstMediaRef(
            asset_id=asset_id,
            expected_kind=expected_kind,
            expected_mime_type=expected_mime_type,
            expected_sha256=expected_sha256,
            expected_byte_size=expected_byte_size,
            media_path=f"media/{asset_id}{extension}",
        )
        existing = media_refs_by_id.get(asset_id)
        if existing is not None:
            if existing != candidate:
                raise ValueError(f"Publication asset {asset_id} has conflicting media metadata")
            return existing
        media_refs_by_id[asset_id] = candidate
        return candidate

    def add_rich(
        kind: EnrichmentPlacementKind,
        section_index: int | None,
        block: dict[str, Any],
    ) -> None:
        if kind is EnrichmentPlacementKind.AFTER_SECTION and (
            section_index is None or section_index >= len(document.sections)
        ):
            raise ValueError(f"Enrichment placement refers to missing section {section_index}")
        rich_by_placement.setdefault((kind, section_index), []).append(block)

    for table in document.tables:
        block = _table_block(table)
        rich_block(block, "semantic_title", f"table:{table.key}:title", table.title)
        if table.caption is not None:
            rich_block(block, "semantic_caption", f"table:{table.key}:caption", table.caption)
        column_spans = [
            semantic_spans(f"table:{table.key}:column:{column_index:04d}", column.label)
            for column_index, column in enumerate(table.columns, start=1)
        ]
        if column_spans and all(item is not None for item in column_spans):
            block["semantic_columns"] = column_spans
        cell_spans = [
            _table_cell_render_spans(
                cell,
                semantic_spans(
                    f"table:{table.key}:row:{row_index:04d}:cell:{cell_index:04d}",
                    cell,
                ),
            )
            for row_index, row in enumerate(table.rows, start=1)
            for cell_index, cell in enumerate(row.cells, start=1)
        ]
        if cell_spans:
            # Keep the renderer's flat row-cell sequence; each entry is one cell's
            # semantic spans, not a recursively flattened span array.
            block["semantic_cells"] = cell_spans
        add_rich(
            table.placement.kind,
            table.placement.section_index,
            block,
        )
    for diagram in document.diagrams:
        block = _diagram_block(diagram, media_ref)
        rich_block(block, "semantic_title", f"diagram:{diagram.key}:title", diagram.title)
        if diagram.caption is not None:
            rich_block(block, "semantic_caption", f"diagram:{diagram.key}:caption", diagram.caption)
        add_rich(
            diagram.placement.kind,
            diagram.placement.section_index,
            block,
        )
    for chart in document.charts:
        block = _chart_block(chart, media_ref)
        rich_block(block, "semantic_title", f"chart:{chart.key}:title", chart.title)
        if chart.caption is not None:
            rich_block(block, "semantic_caption", f"chart:{chart.key}:caption", chart.caption)
        add_rich(
            chart.placement.kind,
            chart.placement.section_index,
            block,
        )
    sources_by_id = {source.source_document_id: source for source in document.sources}
    for figure in document.figures:
        source = sources_by_id.get(figure.source_document_id)
        if source is None:
            raise ValueError(
                "Publication figure identifies an unknown source document: "
                f"{figure.source_document_id}"
            )
        block = _figure_block(figure, source, media_ref)
        rich_block(block, "semantic_caption", f"figure:{figure.key}:caption", figure.caption)
        add_rich(
            figure.placement.kind,
            figure.placement.section_index,
            block,
        )

    reference_blocks = list(
        rich_by_placement.get((EnrichmentPlacementKind.AFTER_TIMELINE, None), ())
    )
    investigation_id = opinion.investigation_id if opinion is not None else None

    def opinion_citation(kind: str, reference_id: UUID) -> dict[str, str]:
        noun = "Finding" if kind == "finding" else "Évidence"
        uri_kind = "findings" if kind == "finding" else "evidence"
        return {
            "label": f"{noun} {reference_id}",
            "href": (f"autowork://investigations/{investigation_id}/{uri_kind}/{reference_id}"),
        }

    notes_by_anchor: dict[str, list[dict[str, Any]]] = {}
    if opinion is not None:
        for observation in opinion.observations:
            notes_by_anchor.setdefault(observation.anchor, []).append(
                {
                    "type": "analyst_observation",
                    "state_label": _OBSERVATION_STATE_LABELS[observation.state],
                    "relation_label": _OBSERVATION_RELATION_LABELS[observation.relation],
                    "text": observation.text,
                    "finding_refs": [
                        opinion_citation("finding", reference_id)
                        for reference_id in observation.finding_refs
                    ],
                    "evidence_refs": [
                        opinion_citation("evidence", reference_id)
                        for reference_id in observation.evidence_refs
                    ],
                }
            )

    def append_paragraph(text: str, anchor: str) -> None:
        body_blocks.append(
            {
                "type": "paragraph",
                "text": text,
                **(
                    {"semantic_spans": spans}
                    if (spans := semantic_spans(anchor, text)) is not None
                    else {}
                ),
            }
        )
        body_blocks.extend(notes_by_anchor.get(anchor, ()))

    body_blocks: list[dict[str, Any]] = []
    for index, lead_paragraph in enumerate(document.lead, start=1):
        append_paragraph(lead_paragraph.text, lead_paragraph_anchor(index))
    body_blocks.extend(rich_by_placement.get((EnrichmentPlacementKind.AFTER_LEAD, None), ()))
    lead_fingerprints = {
        " ".join(lead_paragraph.text.casefold().split()) for lead_paragraph in document.lead
    }
    for section_index, section in enumerate(document.sections):
        paragraph_blocks = [
            (paragraph_index, paragraph)
            for paragraph_index, paragraph in enumerate(section.paragraphs, start=1)
            if " ".join(paragraph.text.casefold().split()) not in lead_fingerprints
        ]
        if section.heading and paragraph_blocks:
            body_blocks.append({"type": "section_heading", "text": section.heading})
        for paragraph_index, paragraph in paragraph_blocks:
            append_paragraph(
                paragraph.text,
                section_paragraph_anchor(section_index, paragraph_index),
            )
        body_blocks.extend(
            rich_by_placement.get((EnrichmentPlacementKind.AFTER_SECTION, section_index), ())
        )
    body_blocks.extend(rich_by_placement.get((EnrichmentPlacementKind.END, None), ()))

    figure_number = 0
    for block in (*reference_blocks, *body_blocks):
        if block.get("type") in {"diagram", "figure", "chart"}:
            figure_number += 1
            block["figure_number"] = figure_number

    timeline: list[dict[str, Any]] = []
    for index, publication_timeline_entry in enumerate(document.timeline, start=1):
        timeline_row: dict[str, Any] = {
            "display_date": _display_date(
                publication_timeline_entry.date_text, publication_timeline_entry.event_date
            ),
            "text": publication_timeline_entry.text,
            "source_urls": _timeline_source_urls(
                publication_timeline_entry.evidence_refs, sources_by_id
            ),
        }
        spans = semantic_spans(timeline_anchor(index), publication_timeline_entry.text)
        if spans is not None:
            timeline_row["semantic_spans"] = spans
        timeline.append(timeline_row)
    source_references: list[dict[str, Any]] = []
    for index, reference in enumerate(reference_entries, start=1):
        source = sources_by_id.get(reference.source_document_id)
        if source is None:
            raise ValueError(
                "Publication reference identifies an unknown source document: "
                f"{reference.source_document_id}"
            )
        reference_row: dict[str, Any] = {
            "display_date": (
                _display_date(None, source.published_at)
                if source.published_at is not None
                else "Date de publication non précisée"
            ),
            "text": reference.text,
            "source_urls": [source.canonical_url],
        }
        if source.kind.value == "technical_resource":
            reference_row["role_label"] = "Contexte technique"
        spans = semantic_spans(f"reference:{index:04d}", reference.text)
        if spans is not None:
            reference_row["semantic_spans"] = spans
        source_references.append(reference_row)

    def publisher_label(source: Any) -> str | None:
        if source.editorial_role is None or source.editorial_role.value != "context":
            return cast(str | None, source.publisher)
        publisher = (source.publisher or "").strip() or (
            urlsplit(source.canonical_url).hostname or "source"
        ).removeprefix("www.")
        return f"Contexte · {publisher}"

    sources = [
        {
            "title": source.title,
            "publisher": publisher_label(source),
            "date": source.published_at.isoformat() if source.published_at is not None else None,
            "url": source.canonical_url,
        }
        for source in document.sources
    ]

    indicators = _indicator_values(document.indicators)
    original_indicators = _indicator_values(original_indicator_groups)
    ioc_rows = []
    for item in ioc_details:
        source_links = []
        for source_id in item.source_document_ids:
            source = sources_by_id.get(source_id)
            if source is None:
                raise ValueError(f"Publication IOC identifies an unknown source: {source_id}")
            source_links.append(
                {
                    "label": source.title or source.publisher or source.canonical_url,
                    "url": source.canonical_url,
                }
            )
        ioc_rows.append(
            {
                "type": item.artifact_type.value,
                "value": item.value,
                "value_spans": _table_cell_typst_spans(item.value),
                "source_role": item.source_role.value,
                "context": item.context,
                "evidence_quote": item.evidence_quote,
                "source_urls": source_links,
                "reliability": item.evidence_basis.value,
                "status": item.disposition.value,
                "status_label": _IOC_DISPOSITION_LABELS[item.disposition],
                "status_reason": item.disposition_reason,
                "relevance_classification": (
                    item.relevance_classification.value
                    if item.relevance_classification is not None
                    else None
                ),
                "relevance_reason_code": (
                    item.relevance_reason_code.value
                    if item.relevance_reason_code is not None
                    else None
                ),
                "first_observed": (
                    item.first_observed.isoformat() if item.first_observed is not None else None
                ),
                "last_observed": (
                    item.last_observed.isoformat() if item.last_observed is not None else None
                ),
            }
        )
    original_publishers = sorted(
        {
            (sources_by_id[source_id].publisher or "").strip()
            or (sources_by_id[source_id].canonical_url.partition("://")[2].split("/", 1)[0])
            for group in original_indicator_groups
            for indicator in group.indicators
            for source_id in indicator.source_document_ids
            if source_id in sources_by_id
        },
        key=str.casefold,
    )
    original_indicator_note = (
        "Le lien avec le sujet n\u2019est pas démontré. Sources : "
        f"{' ; '.join(original_publishers)}."
        if original_publishers
        else "Le lien avec le sujet n\u2019est pas démontré."
    )

    content_sections: list[dict[str, Any]] = [
        {
            "type": "references",
            "timeline": (
                source_references if isinstance(publication_v5, PublicationDocumentV5) else timeline
            ),
            "blocks": reference_blocks,
            "sources": sources,
        },
        {"type": "synthesis", "blocks": body_blocks},
    ]
    timeline_figure = (
        build_publication_timeline_figure(publication_v5)
        if isinstance(publication_v5, PublicationDocumentV5)
        else None
    )
    expected_section_types = publication_content_section_types(value)
    if timeline_figure is not None:
        timeline_figure["figure_number"] = figure_number + 1
        timeline_figure["figure_id_spans"] = _breakable_typst_display_text(
            timeline_figure["figure_id"], interval=8
        )
        timeline_figure["publication_sha256_spans"] = _breakable_typst_display_text(
            timeline_figure["publication_sha256"], interval=8
        )
        for event in timeline_figure["events"]:
            for source in event["sources"]:
                source["url_spans"] = _breakable_typst_display_text(
                    source["url"], interval=12, break_at_technical_separators=True
                )
                source["source_sha256_spans"] = _breakable_typst_display_text(
                    source["source_sha256"], interval=8
                )
                source["evidence_sha256_spans"] = _breakable_typst_display_text(
                    source["evidence_sha256"], interval=8
                )

    if draft_preview is not None and draft_preview.synthesis is not None:
        chronology: list[dict[str, Any]] = []
        for synthesis_timeline_entry in draft_preview.synthesis.timeline:
            source_urls = sorted(
                {
                    sources_by_id[reference.source_document_id].canonical_url
                    for reference in synthesis_timeline_entry.evidence_refs
                    if reference.source_document_id in sources_by_id
                }
            )
            if not source_urls:
                raise ValueError(
                    "Draft preview chronology has no source represented in the pinned V5"
                )
            chronology.append(
                {
                    "display_date": _display_date(
                        synthesis_timeline_entry.date_text,
                        synthesis_timeline_entry.event_date,
                    ),
                    "text": synthesis_timeline_entry.text,
                    "source_urls": source_urls,
                }
            )
    elif isinstance(publication_v5, PublicationDocumentV5):
        chronology = timeline
    else:
        chronology = []
    if "chronology" in expected_section_types:
        chronology_section: dict[str, Any] = {
            "type": "chronology",
            "heading": "Chronologie",
            "timeline": chronology,
        }
        if timeline_figure is not None:
            chronology_section["derived_timeline_figure"] = timeline_figure
        content_sections.append(chronology_section)

    investigation_chain_figure = (
        build_investigation_chain_figure(publication_v6) if publication_v6 is not None else None
    )
    if investigation_chain_figure is not None:
        investigation_chain_figure["figure_id_spans"] = _breakable_typst_display_text(
            investigation_chain_figure["figure_id"], interval=8
        )
        investigation_chain_figure["opinion_sha256_spans"] = _breakable_typst_display_text(
            investigation_chain_figure["opinion_sha256"], interval=8
        )
        investigation_chain_figure["base_publication_sha256_spans"] = _breakable_typst_display_text(
            investigation_chain_figure["base_publication_sha256"], interval=8
        )
    infrastructure_graph_figure = (
        build_investigation_graph_figure(publication_v5, infrastructure_graph)
        if isinstance(publication_v5, PublicationDocumentV5)
        else None
    )
    if infrastructure_graph_figure is not None:
        for edge in infrastructure_graph_figure["edges"]:
            edge["source_value_spans"] = _breakable_typst_display_text(
                edge["source_value"], interval=32, break_at_technical_separators=True
            )
            edge["target_value_spans"] = _breakable_typst_display_text(
                edge["target_value"], interval=32, break_at_technical_separators=True
            )
            for reference in edge["evidence_refs"]:
                reference["sha256_spans"] = _breakable_typst_display_text(
                    reference["sha256"], interval=32, break_at_technical_separators=True
                )
        infrastructure_graph_figure["figure_id_spans"] = _breakable_typst_display_text(
            infrastructure_graph_figure["figure_id"], interval=8
        )
        infrastructure_graph_figure["graph_sha256_spans"] = _breakable_typst_display_text(
            infrastructure_graph_figure["graph_sha256"], interval=8
        )
        infrastructure_graph_figure["publication_sha256_spans"] = _breakable_typst_display_text(
            infrastructure_graph_figure["publication_sha256"], interval=8
        )
        if infrastructure_graph_media_ref is not None:
            if (
                infrastructure_graph_media_ref.expected_kind is not MediaAssetKind.DIAGRAM_SVG
                or infrastructure_graph_media_ref.expected_mime_type != "image/svg+xml"
                or infrastructure_graph_media_ref.expected_sha256 is None
                or infrastructure_graph_media_ref.expected_byte_size is None
            ):
                raise ValueError("Infrastructure graph media reference must be a pinned SVG")
            resolved_graph_media_ref = media_ref(
                asset_id=infrastructure_graph_media_ref.asset_id,
                expected_kind=infrastructure_graph_media_ref.expected_kind,
                expected_mime_type=infrastructure_graph_media_ref.expected_mime_type,
                expected_sha256=infrastructure_graph_media_ref.expected_sha256,
                expected_byte_size=infrastructure_graph_media_ref.expected_byte_size,
            )
            if resolved_graph_media_ref != infrastructure_graph_media_ref:
                raise ValueError("Infrastructure graph media path is not canonical")
            infrastructure_graph_figure["media_path"] = resolved_graph_media_ref.media_path
            infrastructure_graph_figure["svg_sha256"] = resolved_graph_media_ref.expected_sha256
    limitations = (
        [
            {
                "text": uncertainty.text,
                "source_urls": sorted(
                    {
                        sources_by_id[source_id].canonical_url
                        for source_id in uncertainty.source_document_ids
                        if source_id in sources_by_id
                    }
                ),
            }
            for uncertainty in draft_preview.synthesis.uncertainties
        ]
        if draft_preview is not None and draft_preview.synthesis is not None
        else []
    )
    if "limitations" in expected_section_types:
        content_sections.append(
            {"type": "limitations", "heading": "Limites des preuves", "items": limitations}
        )
    if opinion is not None and "analyst_opinion" in expected_section_types:
        qualification_labels = {
            "fact": "Fait étayé",
            "inference": "Inférence",
            "hypothesis": "Hypothèse",
        }
        display_blocks: list[tuple[str, str, str, float, tuple[UUID, ...], tuple[UUID, ...]]] = []
        if rendered_opinion is not None:
            for rendered_block in rendered_opinion.blocks:
                display_blocks.append(
                    (
                        rendered_block.heading,
                        rendered_block.text,
                        rendered_block.qualification.value,
                        rendered_block.certainty,
                        rendered_block.finding_refs,
                        rendered_block.evidence_refs,
                    )
                )
        else:
            for opinion_block in opinion.renderable_blocks:
                display_blocks.append(
                    (
                        _OPINION_BLOCK_HEADINGS[opinion_block.block_type],
                        opinion_block.text,
                        opinion_block.qualification.value,
                        opinion_block.certainty,
                        opinion_block.finding_refs,
                        opinion_block.evidence_refs,
                    )
                )
        opinion_figures = (
            rendered_opinion.protocol_figures
            if rendered_opinion is not None
            else opinion.protocol_figures
        )
        next_figure_number = figure_number + (1 if timeline_figure is not None else 0)
        if investigation_chain_figure is not None:
            investigation_chain_figure["figure_number"] = (
                next_figure_number + len(opinion_figures) + 1
            )
        if infrastructure_graph_figure is not None:
            infrastructure_graph_figure["figure_number"] = (
                next_figure_number
                + len(opinion_figures)
                + (1 if investigation_chain_figure is not None else 0)
                + 1
            )
        unresolved_points = (
            rendered_opinion.unresolved_points
            if rendered_opinion is not None
            else opinion.unresolved_points
        )

        opinion_section: dict[str, Any] = {
            "type": "analyst_opinion",
            "heading": "Avis de l'analyste",
            "blocks": [
                {
                    "number": f"1.{index + 1}",
                    "heading": heading,
                    "text": text,
                    "qualification": qualification,
                    "qualification_label": qualification_labels[qualification],
                    "certainty_percent": round(certainty * 100),
                    "finding_refs": [
                        opinion_citation("finding", reference_id) for reference_id in finding_refs
                    ],
                    "evidence_refs": [
                        opinion_citation("evidence", reference_id) for reference_id in evidence_refs
                    ],
                }
                for index, (
                    heading,
                    text,
                    qualification,
                    certainty,
                    finding_refs,
                    evidence_refs,
                ) in enumerate(display_blocks)
            ],
            "unresolved_points": list(unresolved_points),
        }
        if investigation_chain_figure is not None:
            opinion_section["investigation_chain_figure"] = investigation_chain_figure
        if infrastructure_graph_figure is not None:
            opinion_section["infrastructure_graph_figure"] = infrastructure_graph_figure
        if opinion_figures:
            opinion_section["protocol_figures"] = [
                {
                    "type": "diagram",
                    "key": f"protocol-{figure.kind.value}",
                    "title": _PROTOCOL_FIGURE_TITLES[figure.kind.value],
                    "caption": figure.caption,
                    "media_path": media_ref(
                        asset_id=figure.asset_id,
                        expected_kind=MediaAssetKind.DIAGRAM_SVG,
                        expected_mime_type="image/svg+xml",
                        expected_sha256=figure.asset_sha256,
                        expected_byte_size=None,
                    ).media_path,
                    "model_sha256": figure.model_sha256,
                    "spec_sha256": figure.spec_sha256,
                    "asset_sha256": figure.asset_sha256,
                    "preview_sha256": figure.preview_sha256,
                    "pin_sha256": figure.pin_sha256,
                    "compiler_name": figure.compiler_name,
                    "compiler_version": figure.compiler_version,
                    "compiler_policy_version": figure.compiler_policy_version,
                    "field_evidence": [item.to_payload() for item in figure.field_evidence],
                    "figure_number": next_figure_number + index,
                }
                for index, figure in enumerate(opinion_figures, start=1)
            ]
        content_sections.append(opinion_section)
    if detection is not None and "detection" in expected_section_types:
        content_sections.append(_publication_detection_section(detection))
    if "technical_annex" in expected_section_types:
        content_sections.append(
            {
                "type": "technical_annex",
                "indicators": indicators,
                "original_indicators": original_indicators,
                "original_indicator_note": original_indicator_note,
                "ioc_rows": ioc_rows,
            }
        )

    return TypstPublicationModelV2(
        title=document.title,
        content_sections=content_sections,
        media_refs=tuple(media_refs_by_id.values()),
        draft_watermark=("BROUILLON NON APPROUVÉ" if draft_preview is not None else None),
    )


class TemplateBundleInvalidError(ValueError):
    """Raised when the renderer manifest or one of its listed files is invalid."""


def _load_renderer_manifest(
    chp_typst_root: Path, manifest_name: str = _PUBLICATION_RENDERER_MANIFEST
) -> tuple[Path, str, tuple[str, ...]]:
    """Read one renderer manifest, returning (resolved root, version, files)."""
    try:
        root = chp_typst_root.resolve(strict=True)
    except OSError as exc:
        raise TemplateBundleInvalidError(
            f"Typst template root is missing or unreadable: {chp_typst_root}"
        ) from exc
    if not root.is_dir():
        raise TemplateBundleInvalidError(f"Typst template root is not a directory: {root}")

    manifest_relative_path = PurePosixPath(manifest_name)
    if (
        manifest_relative_path.is_absolute()
        or "\\" in manifest_name
        or ".." in manifest_relative_path.parts
        or len(manifest_relative_path.parts) != 1
    ):
        raise TemplateBundleInvalidError("Typst renderer manifest name must be a relative filename")
    manifest_path = root / manifest_name
    try:
        manifest = json.loads(manifest_path.read_bytes())
    except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise TemplateBundleInvalidError(
            f"Typst renderer manifest is missing or invalid: {manifest_path}"
        ) from exc
    if not isinstance(manifest, dict):
        raise TemplateBundleInvalidError("Typst renderer manifest must be a JSON object")
    template_version = manifest.get("template_version")
    files = manifest.get("files")
    if not isinstance(template_version, str) or not template_version.strip():
        raise TemplateBundleInvalidError(
            "Typst renderer manifest requires a non-empty template_version"
        )
    if not isinstance(files, list) or any(
        not isinstance(path, str) or not path.strip() for path in files
    ):
        raise TemplateBundleInvalidError(
            "Typst renderer manifest files must be a list of non-empty relative paths"
        )
    if len(set(files)) != len(files):
        raise TemplateBundleInvalidError("Typst renderer manifest contains duplicate file paths")
    return root, template_version, tuple(files)


def load_template_bundle(
    chp_typst_root: Path,
    *,
    manifest_name: str = _PUBLICATION_RENDERER_MANIFEST,
) -> TypstTemplateBundle:
    """Read and hash the exact manifest-listed template bytes in one operation."""
    root, template_version, files = _load_renderer_manifest(chp_typst_root, manifest_name)
    resolved_files = resolve_bundle_files(root, files, error=TemplateBundleInvalidError)
    contents: list[TemplateFile] = []
    for relative_path, source_path in resolved_files:
        try:
            content = source_path.read_bytes()
        except OSError as exc:
            raise TemplateBundleInvalidError(
                f"Manifest-listed file cannot be read: {relative_path!r}"
            ) from exc
        contents.append(TemplateFile(relative_path=relative_path, content=content))
    return TypstTemplateBundle(
        template_version=template_version,
        sha256=hash_bundle_contents(tuple((file.relative_path, file.content) for file in contents)),
        files=tuple(contents),
    )


def compute_template_bundle_hash(
    chp_typst_root: Path,
    *,
    manifest_name: str = _PUBLICATION_RENDERER_MANIFEST,
) -> tuple[str, str]:
    """Return the manifest version and stable hash of exactly its listed files."""
    bundle = load_template_bundle(chp_typst_root, manifest_name=manifest_name)
    return bundle.template_version, bundle.sha256


def _display_date(date_text: str | None, event_date: date | None) -> str:
    if date_text:
        return date_text
    if event_date is not None:
        day = "1er" if event_date.day == 1 else str(event_date.day)
        return f"{day} {_FRENCH_MONTH_NAMES[event_date.month - 1]} {event_date.year}"
    # The render-data contract requires text, and blank preserves the absence
    # of a date instead of inventing one.
    return ""


def _timeline_source_urls(
    evidence_refs: tuple[PublicationEvidenceRefV1, ...],
    sources_by_id: Mapping[UUID, PublicationSourceV1],
) -> list[str]:
    urls: list[str] = []
    seen: set[str] = set()
    for evidence_ref in evidence_refs:
        source = sources_by_id.get(evidence_ref.source_document_id)
        if source is None:
            raise ValueError(
                "Timeline evidence references an unknown source document: "
                f"{evidence_ref.source_document_id}"
            )
        if source.canonical_url not in seen:
            seen.add(source.canonical_url)
            urls.append(source.canonical_url)
    return urls


def _table_block(table: PublicationTableV1) -> dict[str, Any]:
    return {
        "type": "table",
        "key": table.key,
        "title": table.title,
        "caption": table.caption,
        "columns": [column.label for column in table.columns],
        "column_weights": _table_column_weights(table),
        "rows": [list(row.cells) for row in table.rows],
    }


def _table_column_weights(table: PublicationTableV1) -> list[float]:
    """Use bounded Typst fractions that leave room for column labels and wider fields."""
    weights: list[float] = []
    for index, column in enumerate(table.columns):
        values = [column.label, *(row.cells[index] for row in table.rows)]
        longest = max((len(value.strip()) for value in values), default=1)
        label_floor = len(column.label.strip()) / 8
        weight = min(2.4, max(0.8, label_floor, longest / 16))
        weights.append(round(weight, 2))
    return weights


def _diagram_block(
    diagram: PublicationDiagramV1,
    media_ref_factory: Callable[..., TypstMediaRef],
) -> dict[str, Any]:
    media = media_ref_factory(
        asset_id=diagram.asset_id,
        expected_kind=MediaAssetKind.DIAGRAM_SVG,
        expected_mime_type="image/svg+xml",
        expected_sha256=None,
        expected_byte_size=None,
    )
    return {
        "type": "diagram",
        "key": diagram.key,
        "title": diagram.title,
        "caption": diagram.caption,
        "media_path": media.media_path,
    }


_PROTOCOL_FIGURE_TITLES = {
    "sequence": "Séquence réseau observée",
    "state_machine": "Machine à états du protocole",
    "message_format": "Format de message observé",
}


def _figure_block(
    figure: PublicationSourceFigureV1,
    source: PublicationSourceV1,
    media_ref_factory: Callable[..., TypstMediaRef],
) -> dict[str, Any]:
    media = media_ref_factory(
        asset_id=figure.asset_id,
        expected_kind=MediaAssetKind.SOURCE_FIGURE,
        expected_mime_type=figure.mime_type,
        expected_sha256=figure.sha256,
        expected_byte_size=figure.byte_size,
    )
    return {
        "type": "figure",
        "key": figure.key,
        "caption": figure.caption,
        "source_note": _reader_facing_figure_source(figure, source),
        "attribution": (
            _reader_facing_editorial_text(figure.provenance)
            or _reader_facing_editorial_text(source.publisher)
            or _reader_facing_editorial_text(source.title)
        ),
        "sha256": figure.sha256,
        "sha256_spans": _breakable_typst_display_text(figure.sha256, interval=8),
        "byte_size": figure.byte_size,
        "media_path": media.media_path,
    }


_INTERNAL_FIGURE_LOCATOR_TEXT = re.compile(
    r"(?:/html\[|\b(?:html|body|div|span|img|svg|section|article)\[\d+\]|"
    r"\bxpath\b|\bdom(?:\s|[_:-])|\banchor\s*:|https?://|"
    r"\bhtml\s+image\s+\d+\b|\bblob(?:\s|[_-])?id\b|"
    r"\b[0-9a-f]{8}-(?:[0-9a-f]{4}-){3}[0-9a-f]{12}\b)",
    re.IGNORECASE,
)


def _reader_facing_editorial_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    if not cleaned or _INTERNAL_FIGURE_LOCATOR_TEXT.search(cleaned):
        return None
    return cleaned


def _reader_facing_figure_source(
    figure: PublicationSourceFigureV1,
    source: PublicationSourceV1,
) -> str | None:
    """Project technical figure metadata into a short, reader-facing source note."""
    parts: list[str] = []
    try:
        host = urlsplit(source.canonical_url).hostname
    except ValueError:
        host = None
    if host and not _INTERNAL_FIGURE_LOCATOR_TEXT.search(host):
        parts.append(host.removeprefix("www."))
    elif (title := _reader_facing_editorial_text(source.title)) is not None:
        parts.append(title)

    section = figure.locator.section
    if section is not None:
        section = _reader_facing_editorial_text(section)
        if section is not None:
            parts.append(section)

    if figure.locator.page is not None:
        parts.append(f"p. {figure.locator.page}")
    return " — ".join(parts) if parts else None


def _chart_block(
    chart: PublicationChartV1,
    media_ref_factory: Callable[..., TypstMediaRef],
) -> dict[str, Any]:
    media = media_ref_factory(
        asset_id=chart.asset_id,
        expected_kind=MediaAssetKind.CHART_SVG,
        expected_mime_type="image/svg+xml",
        expected_sha256=None,
        expected_byte_size=None,
    )
    return {
        "type": "chart",
        "key": chart.key,
        "kind": chart.kind.value,
        "title": chart.title,
        "caption": chart.caption,
        "media_path": media.media_path,
    }
