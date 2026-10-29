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
from cti_app.application.production_normalization import (
    display_indicator_value,
    normalize_indicator_value,
)
from cti_app.application.protocol_diagram_projection import (
    PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION,
)
from cti_app.application.publication_secret_guard import guard_publication_render_data
from cti_app.application.typst_bundle import hash_bundle_contents, resolve_bundle_files
from cti_app.domain.analyst_opinion import (
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystOpinionStatus,
    AnalystOpinionV1,
    OpinionBlockType,
    OpinionProtocolFigureV1,
    normalize_opinion_text,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationFinding,
    NoveltyClass,
)
from cti_app.domain.investigation_graph import InvestigationGraph, InvestigationGraphNodeKind
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

# v31 adds template-driven editorial layout metadata and opinion pipe tables.
# Canonical V5/V6 publication data remains unchanged.
_RENDER_DATA_SCHEMA_VERSION = "typst-publication-model-v31-editorial-layout-metadata"
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
_IOC_DISPOSITION_REASON_LABELS = {
    "contextual_indicator": "indicateur contextuel",
    "legitimate_service": "service légitime",
    "subject_link_not_demonstrated": "lien au sujet non démontré",
    "subject_relation_demonstrated": "lien établi",
    "unsupported_artifact_type": "type non publié",
    "unsupported_hash_format": "format de hash invalide",
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
        if should_break(token):
            pieces = []
            start = 0
            while start < len(token):
                end = min(start + interval, len(token))
                if break_at_technical_separators and end < len(token):
                    separators = [
                        index + 1
                        for index in range(start, end)
                        if token[index] in _TECHNICAL_BREAK_SEPARATORS
                    ]
                    if separators:
                        end = separators[-1]
                    else:
                        lookahead_end = min(start + interval + 4, len(token))
                        next_separator = next(
                            (
                                index + 1
                                for index in range(end, lookahead_end)
                                if token[index] in _TECHNICAL_BREAK_SEPARATORS
                            ),
                            None,
                        )
                        if next_separator is not None:
                            end = next_separator
                pieces.append(token[start:end])
                start = end
        else:
            pieces = [token]
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
    PublicationDetectionReason.EXPLICIT_TEST_REQUIRED: "test explicite requis",
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
        reason = _DETECTION_REASON_LABELS[detection.validation_reason]
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

    section_types = ["references"]
    timeline_figure = (
        build_publication_timeline_figure(publication_v5)
        if isinstance(publication_v5, PublicationDocumentV5)
        and not (
            draft_preview is not None
            and draft_preview.synthesis is not None
            and draft_preview.synthesis.timeline
        )
        else None
    )
    if draft_preview is not None and draft_preview.synthesis is not None:
        has_chronology = bool(draft_preview.synthesis.timeline) or timeline_figure is not None
        has_limitations = bool(draft_preview.synthesis.uncertainties)
    else:
        has_chronology = bool(document.timeline)
        has_chronology = has_chronology or timeline_figure is not None
        has_limitations = False
    if has_chronology:
        section_types.append("chronology")
    section_types.append("synthesis")

    opinion = (
        publication_v6.analyst_opinion
        if publication_v6 is not None
        else draft_preview.analyst_opinion
        if draft_preview is not None
        else None
    )
    rendered_opinion = publication_v6.rendered_opinion if publication_v6 is not None else None
    attached_figure_ids = {item.asset_id for item in opinion.protocol_figures} if opinion else set()
    body_anchor_names = {
        *(lead_paragraph_anchor(index) for index, _ in enumerate(document.lead, start=1))
    }
    lead_fingerprints = {" ".join(item.text.casefold().split()) for item in document.lead}
    for section_index, section in enumerate(document.sections):
        for paragraph_index, paragraph in enumerate(section.paragraphs, start=1):
            if " ".join(paragraph.text.casefold().split()) not in lead_fingerprints:
                body_anchor_names.add(section_paragraph_anchor(section_index, paragraph_index))
    has_unanchored_observations = bool(
        opinion is not None
        and any(item.anchor not in body_anchor_names for item in opinion.observations)
    )
    has_renderable_opinion = bool(opinion and opinion.renderable_blocks)
    has_rendered_opinion_content = bool(
        rendered_opinion
        and (
            rendered_opinion.blocks
            or rendered_opinion.unresolved_points
            or any(
                item.asset_id in attached_figure_ids for item in rendered_opinion.protocol_figures
            )
        )
    )
    has_opinion_figures = bool(
        opinion
        and (
            any(
                item.asset_id in attached_figure_ids
                and item.compiler_policy_version == PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION
                for item in opinion.protocol_figures
            )
            or opinion.unresolved_points
        )
    )
    has_approved_observation_chain = bool(
        publication_v6
        and opinion
        and opinion.status is AnalystOpinionStatus.APPROVED
        and opinion.observations
    )
    if has_limitations:
        section_types.append("limitations")
    if (
        has_unanchored_observations
        or has_renderable_opinion
        or has_rendered_opinion_content
        or has_opinion_figures
        or has_approved_observation_chain
        or (draft_preview is not None and opinion is not None)
    ):
        section_types.append("analyst_opinion")
    original_indicator_groups = (
        publication_v5.original_indicators
        if isinstance(publication_v5, PublicationDocumentV5)
        else ()
    )
    draft_iocs = _draft_opinion_iocs(draft_preview) if draft_preview is not None else ()
    ioc_details = (
        publication_v5.ioc_details if isinstance(publication_v5, PublicationDocumentV5) else ()
    )
    has_iocs = (
        any(group.indicators for group in document.indicators)
        or any(group.indicators for group in original_indicator_groups)
        or ioc_details
        or draft_iocs
    )
    has_opinion_provenance = bool(
        opinion
        and (
            opinion.protocol_figures
            or any(block.finding_refs or block.evidence_refs for block in opinion.blocks)
            or any(note.finding_refs or note.evidence_refs for note in opinion.observations)
        )
    )
    if publication_v6 is not None and publication_v6.detection is not None:
        section_types.append("detection")
    if has_iocs:
        section_types.append("ioc")
    if has_iocs or has_opinion_provenance:
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
    findings: tuple[InvestigationFinding, ...] = ()
    evidence_observations: tuple[EvidenceObservation, ...] = ()

    def __getattr__(self, name: str) -> Any:
        return getattr(self.base_publication, name)

    def __post_init__(self) -> None:
        object.__setattr__(self, "findings", tuple(self.findings))
        object.__setattr__(self, "evidence_observations", tuple(self.evidence_observations))
        if any(not isinstance(item, InvestigationFinding) for item in self.findings):
            raise ValueError("Draft preview findings have an invalid type")
        if any(not isinstance(item, EvidenceObservation) for item in self.evidence_observations):
            raise ValueError("Draft preview observations have an invalid type")
        if self.analyst_opinion.status is not AnalystOpinionStatus.DRAFT:
            raise ValueError("Only draft opinions can use the draft preview projection")
        if any(
            item.investigation_id != self.analyst_opinion.investigation_id for item in self.findings
        ) or any(
            item.investigation_id != self.analyst_opinion.investigation_id
            for item in self.evidence_observations
        ):
            raise ValueError("Draft preview evidence belongs to another investigation")
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
class _DraftIOC:
    artifact_type: ArtifactType
    value: str
    normalized_value: str
    status: str
    context: str
    finding_refs: tuple[UUID, ...]
    observation_refs: tuple[UUID, ...]
    qualification: str


_SUBJECT_REF_SCOPE_PREFIX = "Subject reference: "
_SAMPLE_HASH_PATTERN = re.compile(r"^(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})$", re.IGNORECASE)
_DEFANGED_URL_PATTERN = re.compile(r"\bhxxps?://[^\s\"'<>]+", re.IGNORECASE)
_DEFANGED_IP_PATTERN = re.compile(r"(?<!\d)(?:\d{1,3}\[\.\]){3}\d{1,3}(?!\d)")
_DEFANGED_DOMAIN_PATTERN = re.compile(r"(?<![\w.-])(?:[A-Za-z0-9-]+\[\.\])+[A-Za-z0-9-]+(?![\w.-])")
_OPINION_TECHNICAL_TOKEN = re.compile(
    r"(?<![0-9a-f])(?:[0-9a-f]{64}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-"
    r"[0-9a-f]{4}-[0-9a-f]{12})(?![0-9a-f])",
    re.IGNORECASE,
)
_OPINION_INLINE_CODE = re.compile(r"`([^`\n]+)`")
_PIPE_TABLE_SEPARATOR_CELL = re.compile(r":?-{3,}:?")
_PIPE_TABLE_SHA256 = re.compile(r"(?<![0-9a-f])[0-9a-f]{64}(?![0-9a-f])", re.IGNORECASE)


def _opinion_text_spans(value: str) -> list[dict[str, Any]]:
    """Mark inline code, hashes, and UUIDs for non-justified Typst rendering."""
    spans: list[dict[str, Any]] = []

    def add_plain_text(plain_text: str) -> None:
        cursor = 0
        for match in _OPINION_TECHNICAL_TOKEN.finditer(plain_text):
            if match.start() > cursor:
                spans.append({"text": plain_text[cursor : match.start()], "technical": False})
            spans.append(
                {
                    **_breakable_typst_display_text(
                        match.group(0), interval=8, break_at_technical_separators=True
                    ),
                    "technical": True,
                }
            )
            cursor = match.end()
        if cursor < len(plain_text):
            spans.append({"text": plain_text[cursor:], "technical": False})

    cursor = 0
    for match in _OPINION_INLINE_CODE.finditer(value):
        add_plain_text(value[cursor : match.start()])
        spans.append(
            {
                **_breakable_typst_display_text(
                    match.group(1), interval=8, break_at_technical_separators=True
                ),
                "technical": True,
            }
        )
        cursor = match.end()
    add_plain_text(value[cursor:])
    if not spans:
        spans.append({"text": "", "technical": False})
    return spans


def _pipe_table_cell(text: str) -> dict[str, Any] | None:
    """Project a strict inline-code subset without turning cell data into markup."""
    pieces = text.split("`")
    if len(pieces) % 2 == 0:
        return None
    segments: list[dict[str, Any]] = []
    for index, piece in enumerate(pieces):
        is_code = index % 2 == 1
        if is_code and not piece:
            return None
        if piece:
            segments.append(
                {
                    "text": piece,
                    "code": is_code,
                    "display": _breakable_typst_display_text(
                        piece,
                        interval=12,
                        break_at_technical_separators=True,
                    ),
                }
            )
    return {"segments": segments, "text": text}


def _opinion_pipe_table(value: str) -> dict[str, Any] | None:
    """Parse a bounded GitHub-style pipe table; malformed input stays plain text."""
    original_lines = value.splitlines(keepends=True)
    lines = [line.removesuffix("\n").removesuffix("\r") for line in original_lines]
    runs: list[tuple[int, int]] = []
    run_start: int | None = None
    for index, line in enumerate(lines):
        is_pipe_line = line.startswith("|") and line.endswith("|")
        if is_pipe_line and run_start is None:
            run_start = index
        elif not is_pipe_line and run_start is not None:
            runs.append((run_start, index))
            run_start = None
    if run_start is not None:
        runs.append((run_start, len(lines)))
    if len(runs) != 1:
        return None
    start, end = runs[0]
    table_lines = lines[start:end]
    if len(table_lines) < 2:
        return None

    rows = [[cell.strip() for cell in line[1:-1].split("|")] for line in table_lines]
    column_count = len(rows[0])
    if not 1 <= column_count <= 8 or any(len(row) != column_count for row in rows):
        return None
    if len(rows) - 2 > 200:
        return None

    alignment: list[str] = []
    for cell in rows[1]:
        if not _PIPE_TABLE_SEPARATOR_CELL.fullmatch(cell):
            return None
        has_left = cell.startswith(":")
        has_right = cell.endswith(":")
        alignment.append("center" if has_left and has_right else "right" if has_right else "left")

    projected_rows: list[list[dict[str, Any]]] = []
    for row in (rows[0], *rows[2:]):
        projected_row = [_pipe_table_cell(cell) for cell in row]
        if any(cell is None for cell in projected_row):
            return None
        projected_rows.append([cell for cell in projected_row if cell is not None])

    hash_columns = {
        column_index
        for column_index in range(column_count)
        if any(_PIPE_TABLE_SHA256.search(row[column_index]) for row in rows)
    }
    has_sha256 = bool(hash_columns)
    column_weights = [
        5.0
        if index in hash_columns
        else max(0.75, min(3.0, max(len(row[index]) for row in rows) / 15))
        for index in range(column_count)
    ]
    return {
        "column_count": column_count,
        "column_weights": column_weights,
        "alignment": alignment,
        "header": projected_rows[0],
        "rows": projected_rows[1:],
        "wide": column_count >= 4 or has_sha256,
        "prefix": "".join(original_lines[:start]),
        "suffix": "".join(original_lines[end:]),
    }


def _bounded_ioc_context(value: str, limit: int = 320) -> str:
    cleaned = re.sub(r"^\[UNTRUSTED ARTICLE OR TOOL DATA\]\s*", "", value.strip())
    cleaned = re.sub(r"\s*\[END UNTRUSTED DATA\]\s*$", "", cleaned)
    cleaned = " ".join(cleaned.split())
    if len(cleaned) <= limit:
        return cleaned
    prefix = cleaned[: limit - 1].rsplit(" ", 1)[0]
    return (prefix or cleaned[: limit - 1]).rstrip(" ,;:") + "…"


def _supported_subject_hashes(
    finding: InvestigationFinding,
    observations_by_id: Mapping[UUID, EvidenceObservation],
) -> tuple[str, ...]:
    first_line = finding.scope.partition("\n")[0]
    subject_ref = (
        first_line[len(_SUBJECT_REF_SCOPE_PREFIX) :].strip()
        if first_line.startswith(_SUBJECT_REF_SCOPE_PREFIX)
        else ""
    )
    cited = [observations_by_id.get(reference) for reference in finding.observation_refs]
    if not cited or any(item is None for item in cited):
        return ()
    persisted = [item for item in cited if item is not None]
    cited_file_hashes = {
        item.object_id.casefold()
        for item in persisted
        if item.object_type in {"file", "sample_sha256"}
        and _SAMPLE_HASH_PATTERN.fullmatch(item.object_id)
    }
    if subject_ref:
        normalized_subject_ref = subject_ref.casefold()
        if normalized_subject_ref not in cited_file_hashes:
            return ()
    else:
        # Older findings may carry the subject only on one persisted file
        # observation; refuse to choose when those observations disagree.
        if len(cited_file_hashes) != 1:
            return ()
        normalized_subject_ref = next(iter(cited_file_hashes))

    # A composite finding can cite multiple decoded files. Include another
    # subject only when its hash is present in the statement AND matches a
    # persisted file identity among the finding's observation refs. The prose
    # can therefore select evidence-backed subjects but cannot create an IOC.
    statement_hashes = {
        match.group(0).casefold()
        for match in re.finditer(
            r"(?<![0-9a-f])(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})(?![0-9a-f])",
            finding.statement,
            re.IGNORECASE,
        )
    }
    related_subjects = cited_file_hashes.intersection(statement_hashes)
    return tuple(sorted({normalized_subject_ref, *related_subjects}))


def _defanged_note_indicators(text: str) -> tuple[tuple[ArtifactType, str, str], ...]:
    candidates: list[tuple[int, int, ArtifactType, str]] = []
    occupied: list[tuple[int, int]] = []
    for pattern, artifact_type in (
        (_DEFANGED_URL_PATTERN, ArtifactType.URL),
        (_DEFANGED_IP_PATTERN, ArtifactType.IP),
        (_DEFANGED_DOMAIN_PATTERN, ArtifactType.DOMAIN),
    ):
        for match in pattern.finditer(text):
            if any(match.start() < end and match.end() > start for start, end in occupied):
                continue
            raw = match.group(0).rstrip(".,;:)]}")
            if "[.]" not in raw and "hxxp" not in raw.casefold():
                continue
            candidates.append((match.start(), match.end(), artifact_type, raw))
            occupied.append((match.start(), match.end()))

    projected: dict[tuple[ArtifactType, str], tuple[ArtifactType, str, str]] = {}
    for _start, _end, artifact_type, raw in candidates:
        refanged = raw.replace("hxxps://", "https://").replace("hxxp://", "http://")
        refanged = refanged.replace("[.]", ".").replace("(.)", ".").replace("[:]", ":")
        try:
            normalized = normalize_indicator_value(refanged, artifact_type)
            display = display_indicator_value(normalized, artifact_type, defanged=True)
        except ValueError:
            continue
        projected[(artifact_type, normalized.casefold())] = (artifact_type, display, normalized)
    return tuple(
        projected[key] for key in sorted(projected, key=lambda item: (item[0].value, item[1]))
    )


def _draft_opinion_iocs(value: TypstDraftPreviewDocument) -> tuple[_DraftIOC, ...]:
    opinion = value.analyst_opinion
    observations_by_id = {
        item.id: item
        for item in value.evidence_observations
        if item.investigation_id == opinion.investigation_id
    }
    projected: dict[tuple[ArtifactType, str, str], _DraftIOC] = {}

    for finding in value.findings:
        if (
            finding.kind is not FindingKind.NEW_SAMPLE
            or finding.novelty is not NoveltyClass.NEW_TO_ARTICLE
            or not finding.observation_refs
            or any(reference not in observations_by_id for reference in finding.observation_refs)
        ):
            continue
        subject_hashes = _supported_subject_hashes(finding, observations_by_id)
        if not subject_hashes:
            continue
        if finding.assessment is FindingAssessment.REFUTED:
            continue
        if finding.assessment is FindingAssessment.SUPPORTED:
            status = "originals"
            qualification = "Finding new_to_article étayé par les observations citées."
        else:
            status = "candidates"
            qualification = "Candidat non confirmé : le finding n'est pas supported."
        for subject_hash in subject_hashes:
            item = _DraftIOC(
                artifact_type=ArtifactType.HASH,
                value=subject_hash,
                normalized_value=subject_hash,
                status=status,
                context=_bounded_ioc_context(finding.statement),
                finding_refs=(finding.id,),
                observation_refs=finding.observation_refs,
                qualification=qualification,
            )
            projected[(item.artifact_type, item.normalized_value.casefold(), status)] = item

    valid_anchors = publication_document_text_anchors(
        value.base_publication.document,
        references=value.base_publication.references,
    )
    for note in opinion.observations:
        if (
            note.anchor not in valid_anchors
            or not note.evidence_refs
            or any(reference not in observations_by_id for reference in note.evidence_refs)
            or note.relation is AnalystObservationRelation.REFUTES
        ):
            continue
        status = (
            "originals"
            if note.state is AnalystObservationState.OBSERVED
            and note.relation is AnalystObservationRelation.CONFIRMS
            else "candidates"
        )
        for artifact_type, display_value, normalized_value in _defanged_note_indicators(note.text):
            item = _DraftIOC(
                artifact_type=artifact_type,
                value=display_value,
                normalized_value=normalized_value,
                status=status,
                context=_bounded_ioc_context(note.text),
                finding_refs=note.finding_refs,
                observation_refs=note.evidence_refs,
                qualification=(
                    "Indicateur observé dans une note ancrée et confirmée."
                    if status == "originals"
                    else "Candidat non confirmé extrait d'une note ancrée."
                ),
            )
            key = (artifact_type, normalized_value.casefold(), status)
            previous = projected.get(key)
            if previous is not None:
                item = _DraftIOC(
                    artifact_type=item.artifact_type,
                    value=item.value,
                    normalized_value=item.normalized_value,
                    status=item.status,
                    context=" / ".join(dict.fromkeys((previous.context, item.context))),
                    finding_refs=tuple(dict.fromkeys(previous.finding_refs + item.finding_refs)),
                    observation_refs=tuple(
                        dict.fromkeys(previous.observation_refs + item.observation_refs)
                    ),
                    qualification=previous.qualification,
                )
            projected[key] = item
    return tuple(
        projected[key]
        for key in sorted(projected, key=lambda item: (item[0].value, item[1], item[2]))
    )


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
        document_label: str | None = None,
        target_artifact_identities: Mapping[str, Mapping[str, Any]] | None = None,
        infrastructure_graph_media_ref: TypstMediaRef | None = None,
    ) -> TypstRenderSource:
        """Project a publication using its exact service-supplied nature and identity label."""
        if not isinstance(document_label, str) or not document_label.strip():
            raise TemplateBundleInvalidError("Publication render requires document_label")
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

        normalized_document_label = document_label.strip()
        render_data: dict[str, Any] = {
            "schema_version": _RENDER_DATA_SCHEMA_VERSION,
            "document_label": normalized_document_label,
            "document_label_display": _breakable_typst_display_text(
                normalized_document_label,
                interval=8,
                break_at_technical_separators=True,
            ),
            "document_header": _publication_document_header(document),
            "language": document.publication_language,
            "title": model.title,
            "overview": {
                "status_label": model.draft_watermark or _publication_document_header(document),
                "source_count": len(document.sources),
            },
            "brand_logo_available": any(
                file.relative_path == "UTILS/chap.png" for file in template_bundle.files
            ),
            "content_sections": model.content_sections,
        }
        if article_title_target_id is not None:
            render_data["article_title_target_id"] = article_title_target_id
        if target_descriptors or unmapped:
            render_data["mapping_instrumentation_version"] = MAPPING_RENDERER_VERSION
        if model.draft_watermark is not None:
            render_data["draft_watermark"] = model.draft_watermark
        guard_publication_render_data(render_data)
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


def _publication_document_header(
    document: CanonicalPublicationDocument | TypstDraftPreviewDocument,
) -> str:
    if isinstance(document, TypstDraftPreviewDocument):
        return f"Avis — aperçu non approuvé v{document.analyst_opinion.version}"
    if isinstance(document, PublicationDocumentV6) and document.analyst_opinion is not None:
        return f"Avis — publication approuvée v{document.analyst_opinion.version}"
    return "Publication"


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
    draft_iocs = _draft_opinion_iocs(draft_preview) if draft_preview is not None else ()
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
        reference_text = str(reference_id)
        key = (kind, reference_id)
        reference = opinion_reference_by_id.get(key)
        if reference is not None:
            return reference
        opinion_reference_numbers[kind] += 1
        short_label = f"[{kind[0].upper()}{opinion_reference_numbers[kind]}]"
        target_id = f"trace-{kind[0]}-{opinion_reference_numbers[kind]:03d}"
        reference = {
            "label": short_label,
            "target_id": target_id,
            "href": f"autowork://investigations/{investigation_id}/{uri_kind}/{reference_id}",
            "reference_id": reference_text,
            "kind": kind,
        }
        opinion_reference_by_id[key] = reference
        opinion_provenance_entries.append(
            {
                "label": short_label,
                "target_id": target_id,
                "kind_label": noun,
                "reference_id": reference_text,
                "reference_id_spans": _breakable_typst_display_text(reference_text, interval=8),
                "href": reference["href"],
            }
        )
        return reference

    opinion_provenance_entries: list[dict[str, Any]] = []
    opinion_reference_by_id: dict[tuple[str, UUID], dict[str, str]] = {}
    opinion_reference_numbers = {"finding": 0, "evidence": 0}

    def collect_opinion_references(
        _context_label: str,
        kind: str,
        reference_ids: tuple[UUID, ...],
    ) -> list[dict[str, str]]:
        return [opinion_citation(kind, reference_id) for reference_id in reference_ids]

    lead_fingerprints = {
        " ".join(lead_paragraph.text.casefold().split()) for lead_paragraph in document.lead
    }
    body_anchor_texts = {
        lead_paragraph_anchor(index): lead_paragraph.text
        for index, lead_paragraph in enumerate(document.lead, start=1)
    }
    for section_index, section in enumerate(document.sections):
        for paragraph_index, paragraph in enumerate(section.paragraphs, start=1):
            if " ".join(paragraph.text.casefold().split()) not in lead_fingerprints:
                body_anchor_texts[section_paragraph_anchor(section_index, paragraph_index)] = (
                    paragraph.text
                )

    notes_by_anchor: dict[str, list[dict[str, Any]]] = {}
    unanchored_observations: list[dict[str, Any]] = []
    if opinion is not None:
        for observation in opinion.observations:
            if not observation.text.strip():
                continue
            note: dict[str, Any] = {
                "type": "analyst_observation",
                "text": observation.text,
                "text_spans": _opinion_text_spans(observation.text),
                "finding_refs": collect_opinion_references(
                    f"Note ancrée {observation.anchor}", "finding", observation.finding_refs
                ),
                "evidence_refs": collect_opinion_references(
                    f"Note ancrée {observation.anchor}", "evidence", observation.evidence_refs
                ),
            }
            anchor_text = body_anchor_texts.get(observation.anchor)
            if anchor_text is None:
                note["unanchored"] = True
                unanchored_observations.append(note)
                continue
            note["anchor_target"] = (
                "publication-paragraph-"
                + hashlib.sha256(observation.anchor.encode("utf-8")).hexdigest()[:20]
            )
            notes_by_anchor.setdefault(observation.anchor, []).append(note)

    def append_paragraph(text: str, anchor: str) -> None:
        anchor_target = (
            "publication-paragraph-" + hashlib.sha256(anchor.encode("utf-8")).hexdigest()[:20]
            if anchor in body_anchor_texts
            else None
        )
        body_blocks.append(
            {
                "type": "paragraph",
                "text": text,
                **({"anchor_target": anchor_target} if anchor_target is not None else {}),
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
            "source_document_ids": [
                str(reference.source_document_id)
                for reference in publication_timeline_entry.evidence_refs
            ],
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
            "_source_id": reference.source_document_id,
        }
        if source.kind.value == "technical_resource":
            reference_row["role_label"] = "Contexte technique"
        spans = semantic_spans(f"reference:{index:04d}", reference.text)
        if spans is not None:
            reference_row["semantic_spans"] = spans
        source_references.append(reference_row)

    legacy_reference_rows = tuple(source_references)

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

    # Merge canonical indicators and source detail rows by type-aware, refanged
    # value.  The compact inventory cites the reference list while the latter
    # carries the complete evidence/provenance once per distinct source record.
    ioc_by_key: dict[tuple[ArtifactType, str], dict[str, Any]] = {}

    def canonical_ioc_key(value: str, artifact_type: ArtifactType) -> str:
        try:
            return normalize_indicator_value(value, artifact_type)
        except ValueError:
            # Preserve legacy/test literals rather than fabricating a normalized IP.
            return value.strip()

    omitted_invalid_urls: set[str] = set()

    def defanged_ioc_value(
        value: str, normalized_value: str, artifact_type: ArtifactType
    ) -> str | None:
        if artifact_type is ArtifactType.URL:
            try:
                canonical_url = normalize_indicator_value(normalized_value, artifact_type)
            except ValueError:
                omitted_invalid_urls.add(value)
                return None
            parsed_url = urlsplit(canonical_url)
            if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
                omitted_invalid_urls.add(value)
                return None
            return display_indicator_value(canonical_url, artifact_type, defanged=True)
        try:
            canonical = normalize_indicator_value(normalized_value, artifact_type)
            normalized_source_value = normalize_indicator_value(value, artifact_type)
            if normalized_source_value != canonical:
                return value.strip()
            return display_indicator_value(canonical, artifact_type, defanged=True)
        except ValueError:
            return value.strip()

    def ensure_ioc_row(
        artifact_type: ArtifactType,
        value: str,
        normalized_value: str,
    ) -> dict[str, Any] | None:
        canonical = canonical_ioc_key(normalized_value, artifact_type)
        key = (artifact_type, canonical.casefold())
        row = ioc_by_key.get(key)
        if row is None:
            displayed_value = defanged_ioc_value(value, normalized_value, artifact_type)
            if displayed_value is None:
                return None
            row = {
                "type": artifact_type.value,
                "value": displayed_value,
                "value_spans": _table_cell_typst_spans(displayed_value),
                "_source_ids": set(),
                "roles": set(),
                "statuses": set(),
                "provenance": [],
                "_provenance_keys": set(),
                "_draft_contexts": [],
                "_draft_references": [],
            }
            ioc_by_key[key] = row
        return row

    def source_for(source_id: UUID) -> Any:
        source = sources_by_id.get(source_id)
        if source is None:
            raise ValueError(f"Publication IOC identifies an unknown source: {source_id}")
        return source

    detail_source_keys = {
        (
            item.artifact_type,
            canonical_ioc_key(item.normalized_value, item.artifact_type).casefold(),
            source_id,
        )
        for item in ioc_details
        for source_id in item.source_document_ids
    }

    def add_indicator_group(groups: tuple[Any, ...], status_label: str) -> None:
        for group in groups:
            for indicator in group.indicators:
                row = ensure_ioc_row(
                    indicator.artifact_type, indicator.value, indicator.normalized_value
                )
                if row is None:
                    continue
                row["_source_ids"].update(indicator.source_document_ids)
                row["statuses"].add(status_label)
                for source_id in indicator.source_document_ids:
                    key = (
                        indicator.artifact_type,
                        canonical_ioc_key(
                            indicator.normalized_value, indicator.artifact_type
                        ).casefold(),
                        source_id,
                    )
                    if key in detail_source_keys:
                        continue
                    row["roles"].add(source_for(source_id).role.value)
                    row["provenance"].append(
                        {
                            "context": "",
                            "evidence_quote": "",
                            "reliability": "non précisée",
                            "status_label": status_label,
                            "status": status_label,
                            "status_reason": "",
                            "relevance_classification": None,
                            "relevance_reason_code": None,
                            "first_observed": None,
                            "last_observed": None,
                            "source_document_ids": (source_id,),
                            "source_role": source_for(source_id).role.value,
                            "value": row["value"],
                        }
                    )

    add_indicator_group(document.indicators, "Retenu")
    add_indicator_group(original_indicator_groups, "Lien non démontré")
    for item in ioc_details:
        row = ensure_ioc_row(item.artifact_type, item.value, item.normalized_value)
        if row is None:
            continue
        row["_source_ids"].update(item.source_document_ids)
        row["roles"].add(item.source_role.value)
        status_label = _IOC_DISPOSITION_LABELS[item.disposition]
        row["statuses"].add(status_label)
        for source_id in item.source_document_ids:
            source_for(source_id)
        provenance = {
            "context": item.context,
            "evidence_quote": item.evidence_quote,
            "reliability": item.evidence_basis.value,
            "status_label": status_label,
            "status": item.disposition.value,
            "status_reason": item.disposition_reason,
            "relevance_classification": (
                item.relevance_classification.value
                if item.relevance_classification is not None
                else None
            ),
            "relevance_reason_code": (
                item.relevance_reason_code.value if item.relevance_reason_code is not None else None
            ),
            "first_observed": (
                item.first_observed.isoformat() if item.first_observed is not None else None
            ),
            "last_observed": (
                item.last_observed.isoformat() if item.last_observed is not None else None
            ),
            "source_document_ids": item.source_document_ids,
            "source_role": item.source_role.value,
            "value": row["value"],
        }
        for source_id in item.source_document_ids:
            key = (
                source_id,
                tuple(
                    (field, str(value))
                    for field, value in sorted(provenance.items())
                    if field not in {"source_document_ids", "value"}
                ),
            )
            if key in row["_provenance_keys"]:
                continue
            row["_provenance_keys"].add(key)
            row["provenance"].append(provenance)

    # Keep every evidence source addressable from citations, including technical
    # sources and references that have no editorial summary paragraph.
    reference_row_by_source_id: dict[UUID, dict[str, Any]] = {}
    reference_row_by_url: dict[str, dict[str, Any]] = {}
    for reference_row in source_references:
        reference_source_id = reference_row.get("_source_id")
        if isinstance(reference_source_id, UUID):
            reference_row_by_source_id.setdefault(reference_source_id, reference_row)
        for source_url in reference_row.get("source_urls", ()):
            reference_row_by_url.setdefault(source_url, reference_row)
    for source_id, source in sorted(sources_by_id.items(), key=lambda item: str(item[0])):
        if source_id in reference_row_by_source_id:
            continue
        existing_reference = reference_row_by_url.get(source.canonical_url)
        if existing_reference is not None:
            reference_row_by_source_id[source_id] = existing_reference
            continue
        reference_row = {
            "display_date": (
                _display_date(None, source.published_at)
                if source.published_at is not None
                else "Date de publication non précisée"
            ),
            "text": (
                source.title or source.canonical_url
                if source.kind.value == "technical_resource"
                else source.canonical_url
            ),
            "source_urls": [source.canonical_url],
            "_source_id": source_id,
        }
        if source.kind.value == "technical_resource":
            reference_row["role_label"] = "Contexte technique"
        source_references.append(reference_row)
        reference_row_by_source_id[source_id] = reference_row
        reference_row_by_url[source.canonical_url] = reference_row

    reference_target_by_source_id: dict[UUID, str] = {}
    reference_number_by_source_id: dict[UUID, int] = {}
    for index, reference_row in enumerate(source_references, start=1):
        reference_row["number"] = index
        reference_source_id = reference_row.get("_source_id")
        if isinstance(reference_source_id, UUID):
            target_id = f"publication-source-{reference_source_id.hex}-{index:04d}"
            reference_row["target_id"] = target_id
            for source_id, mapped_row in reference_row_by_source_id.items():
                if mapped_row is reference_row:
                    reference_target_by_source_id.setdefault(source_id, target_id)
                    reference_number_by_source_id.setdefault(source_id, index)

    def source_links_for_ids(source_ids: Any) -> list[dict[str, Any]]:
        return [
            {
                "number": reference_number_by_source_id[source_id],
                "target_id": reference_target_by_source_id[source_id],
            }
            for source_id in dict.fromkeys(UUID(str(item)) for item in source_ids)
            if source_id in reference_number_by_source_id
        ]

    def add_source_links(event: dict[str, Any]) -> None:
        event["source_refs"] = source_links_for_ids(event.pop("source_document_ids", ()))

    for event in timeline:
        add_source_links(event)

    def abbreviated(value: str, limit: int) -> str:
        cleaned = " ".join(value.split())
        if len(cleaned) <= limit:
            return cleaned
        prefix = cleaned[: limit - 1].rsplit(" ", 1)[0]
        return (prefix or cleaned[: limit - 1]).rstrip(" ,;:") + "…"

    def compact_ioc_provenance(row: dict[str, Any]) -> str:
        provenance_rows = row["provenance"]
        decisions: dict[tuple[str, str], set[str]] = {}
        contexts: set[str] = set()
        evidence: set[str] = set()
        reliabilities: set[str] = set()
        relevance: set[str] = set()
        observations: set[str] = set()
        for item in provenance_rows:
            decision_key = (item["status_label"], item.get("status_reason") or "")
            decisions.setdefault(decision_key, set()).add(item["source_role"])
            if item.get("context"):
                contexts.add(abbreviated(item["context"], 30))
            if item.get("evidence_quote"):
                evidence.add(abbreviated(item["evidence_quote"], 28))
            if item.get("reliability"):
                reliabilities.add(item["reliability"])
            if item.get("relevance_classification"):
                value = str(item["relevance_classification"])
                if item.get("relevance_reason_code"):
                    value += "/" + str(item["relevance_reason_code"])
                relevance.add(value)
            observed = "-".join(
                value for value in (item.get("first_observed"), item.get("last_observed")) if value
            )
            if observed:
                observations.add(observed)

        parts = ["TLP non précisé"]
        for (status, reason), roles in sorted(decisions.items()):
            role_label = "/".join(
                sorted(roles, key=lambda role: (role != "primary", role.casefold()))
            )
            decision = f"{status} ({role_label}"
            if reason:
                reason_label = _IOC_DISPOSITION_REASON_LABELS.get(reason, reason.replace("_", " "))
                decision += f"; {reason_label}"
            parts.append(decision + ")")
        if contexts:
            parts.append("ctx: " + " / ".join(sorted(contexts, key=str.casefold)))
        if evidence:
            parts.append("preuve: " + " / ".join(sorted(evidence, key=str.casefold)))
        if reliabilities:
            parts.append("fiab: " + "/".join(sorted(reliabilities, key=str.casefold)))
        if relevance:
            parts.append("pert.: " + "/".join(sorted(relevance, key=str.casefold)))
        if observations:
            parts.append("obs.: " + "/".join(sorted(observations)))
        return " · ".join(parts)

    ioc_lists_by_status: dict[str, dict[tuple[ArtifactType, str], dict[str, Any]]] = {
        "retained": {},
        "originals": {},
        "candidates": {},
    }
    ioc_group_priority = {"retained": 0, "originals": 1, "candidates": 2}
    ioc_group_owner: dict[tuple[ArtifactType, str], str] = {}
    ioc_origins: dict[tuple[ArtifactType, str], set[str]] = {}

    def record_ioc_origin(artifact_type: ArtifactType, normalized_value: str, origin: str) -> None:
        key = (
            artifact_type,
            canonical_ioc_key(normalized_value, artifact_type).casefold(),
        )
        ioc_origins.setdefault(key, set()).add(origin)

    def ioc_type_heading(artifact_type: ArtifactType, value: str) -> str:
        if artifact_type is ArtifactType.IP:
            try:
                return "IPv4" if ipaddress.ip_address(value).version == 4 else "IPv6"
            except ValueError:
                return "Adresses IP — format à vérifier"
        return {
            ArtifactType.DOMAIN: "Domaines",
            ArtifactType.URL: "URLs",
            ArtifactType.HASH: "Hashes",
            ArtifactType.EMAIL: "Adresses e-mail",
            ArtifactType.FILENAME: "Noms de fichiers",
            ArtifactType.FILEPATH: "Chemins de fichiers",
        }.get(artifact_type, "Autres indicateurs")

    def matching_ioc_details(
        artifact_type: ArtifactType, normalized_value: str, source_id: UUID
    ) -> list[Any]:
        key = canonical_ioc_key(normalized_value, artifact_type).casefold()
        return [
            detail
            for detail in ioc_details
            if detail.artifact_type is artifact_type
            and canonical_ioc_key(detail.normalized_value, artifact_type).casefold() == key
            and source_id in detail.source_document_ids
        ]

    def add_ioc_list_item(
        status: str,
        *,
        artifact_type: ArtifactType,
        value: str,
        normalized_value: str,
        source_id: UUID,
        details: list[Any],
        qualification: str,
    ) -> None:
        display_value = defanged_ioc_value(value, normalized_value, artifact_type)
        if display_value is None:
            return
        canonical = canonical_ioc_key(normalized_value, artifact_type).casefold()
        key = (artifact_type, canonical)
        owner = ioc_group_owner.get(key)
        if owner is None or ioc_group_priority[status] < ioc_group_priority[owner]:
            if owner is not None:
                existing = ioc_lists_by_status[owner].pop(key, None)
                if existing is not None:
                    ioc_lists_by_status[status][key] = existing
            owner = status
            ioc_group_owner[key] = owner
        grouped = ioc_lists_by_status[owner]
        item = grouped.get(key)
        if item is None:
            item = {
                "artifact_type": artifact_type.value,
                "type_label": ioc_type_heading(artifact_type, normalized_value),
                "value": _breakable_typst_display_text(
                    display_value, interval=8, break_at_technical_separators=True
                ),
                "_source_ids": set(),
                "_evidence": {},
                "_draft_contexts": [],
                "_draft_references": [],
                "qualifications": set(),
                "indicator_status_label": {
                    "retained": "Documenté dans l'article",
                    "originals": "IOC original du run",
                    "candidates": "Piste non confirmée",
                }[owner],
            }
            grouped[key] = item
        else:
            item["indicator_status_label"] = {
                "retained": "Documenté dans l'article",
                "originals": "IOC original du run",
                "candidates": "Piste non confirmée",
            }[owner]
        item["_source_ids"].add(source_id)
        item["qualifications"].add(qualification)
        for detail in details:
            if not detail.evidence_quote.strip():
                continue
            evidence_key = (detail.context, detail.evidence_quote)
            item["_evidence"].setdefault(source_id, {})[evidence_key] = detail

    for group in document.indicators:
        for indicator in group.indicators:
            record_ioc_origin(indicator.artifact_type, indicator.normalized_value, "article")
            for source_id in indicator.source_document_ids:
                add_ioc_list_item(
                    "retained",
                    artifact_type=indicator.artifact_type,
                    value=indicator.value,
                    normalized_value=indicator.normalized_value,
                    source_id=source_id,
                    details=matching_ioc_details(
                        indicator.artifact_type, indicator.normalized_value, source_id
                    ),
                    qualification="Indicateur retenu dans la publication.",
                )

    for group in original_indicator_groups:
        for indicator in group.indicators:
            record_ioc_origin(indicator.artifact_type, indicator.normalized_value, "original")
            for source_id in indicator.source_document_ids:
                details = matching_ioc_details(
                    indicator.artifact_type, indicator.normalized_value, source_id
                )
                verified = [
                    detail
                    for detail in details
                    if detail.disposition is PublicationIOCDisposition.RETAINED
                    and detail.disposition_reason == "subject_relation_demonstrated"
                    and detail.evidence_quote.strip()
                ]
                add_ioc_list_item(
                    "originals",
                    artifact_type=indicator.artifact_type,
                    value=indicator.value,
                    normalized_value=indicator.normalized_value,
                    source_id=source_id,
                    details=verified or details,
                    qualification=(
                        "Association au sujet étayée par l'extrait cité."
                        if verified
                        else "Association au sujet non démontrée dans la source."
                    ),
                )

    for detail in ioc_details:
        if detail.disposition is not PublicationIOCDisposition.UNCERTAIN:
            continue
        record_ioc_origin(detail.artifact_type, detail.normalized_value, "candidate")
        for source_id in detail.source_document_ids:
            add_ioc_list_item(
                "candidates",
                artifact_type=detail.artifact_type,
                value=detail.value,
                normalized_value=detail.normalized_value,
                source_id=source_id,
                details=[detail],
                qualification="Candidat non confirmé : "
                + detail.disposition_reason.replace("_", " ")
                + ".",
            )

    for draft_ioc in draft_iocs:
        status = draft_ioc.status
        canonical = canonical_ioc_key(draft_ioc.normalized_value, draft_ioc.artifact_type)
        list_key = (draft_ioc.artifact_type, canonical.casefold())
        record_ioc_origin(
            draft_ioc.artifact_type,
            draft_ioc.normalized_value,
            "original" if status == "originals" else "candidate",
        )
        display_value = defanged_ioc_value(
            draft_ioc.value, draft_ioc.normalized_value, draft_ioc.artifact_type
        )
        if display_value is None:
            continue
        owner = ioc_group_owner.get(list_key)
        if owner is None or ioc_group_priority[status] < ioc_group_priority[owner]:
            if owner is not None:
                existing = ioc_lists_by_status[owner].pop(list_key, None)
                if existing is not None:
                    ioc_lists_by_status[status][list_key] = existing
            owner = status
            ioc_group_owner[list_key] = owner
        grouped = ioc_lists_by_status[owner]
        draft_ioc_item = grouped.get(list_key)
        if draft_ioc_item is None:
            draft_ioc_item = {
                "artifact_type": draft_ioc.artifact_type.value,
                "type_label": ioc_type_heading(draft_ioc.artifact_type, draft_ioc.normalized_value),
                "value": _breakable_typst_display_text(
                    display_value, interval=8, break_at_technical_separators=True
                ),
                "_source_ids": set(),
                "_evidence": {},
                "_draft_contexts": [],
                "_draft_references": [],
                "qualifications": set(),
                "indicator_status_label": {
                    "retained": "Documenté dans l'article",
                    "originals": "IOC original du run",
                    "candidates": "Piste non confirmée",
                }[owner],
            }
            grouped[list_key] = draft_ioc_item
        else:
            draft_ioc_item["indicator_status_label"] = {
                "retained": "Documenté dans l'article",
                "originals": "IOC original du run",
                "candidates": "Piste non confirmée",
            }[owner]
        draft_ioc_item["_draft_contexts"].append(draft_ioc.context)
        provenance_context = (
            f"IOC original {display_value}"
            if draft_ioc.status == "originals"
            else f"IOC candidat {display_value}"
        )
        draft_ioc_item["_draft_references"].extend(
            collect_opinion_references(
                provenance_context,
                "finding",
                draft_ioc.finding_refs,
            )
        )
        draft_ioc_item["_draft_references"].extend(
            collect_opinion_references(
                provenance_context,
                "evidence",
                draft_ioc.observation_refs,
            )
        )
        draft_ioc_item["qualifications"].add(draft_ioc.qualification)

        row = ensure_ioc_row(draft_ioc.artifact_type, display_value, draft_ioc.normalized_value)
        if row is not None:
            row["statuses"].add(
                "IOC original — finding étayé" if status == "originals" else "Candidat non confirmé"
            )
            row["_draft_contexts"].append(draft_ioc.context)
            reference_count = len(draft_ioc.finding_refs) + len(draft_ioc.observation_refs)
            row["_draft_references"].extend(draft_ioc_item["_draft_references"][-reference_count:])

    def project_ioc_list_groups(status: str) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, Any]]] = {}
        items = sorted(
            ioc_lists_by_status[status].values(),
            key=lambda item: (item["type_label"], item["value"]["text"].casefold()),
        )
        for item in items:
            source_ids = sorted(item.pop("_source_ids"), key=lambda value: str(value))
            item["source_refs"] = [
                {
                    "number": reference_number_by_source_id[source_id],
                    "target_id": reference_target_by_source_id[source_id],
                }
                for source_id in source_ids
                if source_id in reference_number_by_source_id
            ]
            evidence_rows = []
            evidence_by_source = item.pop("_evidence")
            for source_id in source_ids:
                for evidence_detail in evidence_by_source.get(source_id, {}).values():
                    evidence_rows.append(
                        {
                            "source_number": reference_number_by_source_id.get(source_id),
                            "source_target": reference_target_by_source_id.get(source_id),
                            "context": abbreviated(evidence_detail.context, 180),
                            "quote": abbreviated(evidence_detail.evidence_quote, 56),
                            "quote_sha256": _breakable_typst_display_text(
                                hashlib.sha256(
                                    evidence_detail.evidence_quote.encode("utf-8")
                                ).hexdigest(),
                                interval=8,
                                break_at_technical_separators=True,
                            ),
                        }
                    )
            item["evidence"] = evidence_rows
            item["context"] = " / ".join(dict.fromkeys(item.pop("_draft_contexts")))
            item["draft_refs"] = list(
                {
                    (reference["kind"], reference["reference_id"]): reference
                    for reference in item.pop("_draft_references")
                }.values()
            )
            item["qualification"] = "; ".join(sorted(item.pop("qualifications"), key=str.casefold))
            item["relationship_status_label"] = item["qualification"]
            grouped.setdefault(item["type_label"], []).append(item)
        return [{"heading": heading, "items": values} for heading, values in grouped.items()]

    ioc_body = {
        "source_indicators": project_ioc_list_groups("retained"),
        "original_indicators": project_ioc_list_groups("originals"),
        "candidate_indicators": project_ioc_list_groups("candidates"),
        "omitted_invalid_url_count": len(omitted_invalid_urls),
    }

    ioc_rows = []
    for ioc_key, row in sorted(
        ioc_by_key.items(),
        key=lambda item: (item[1]["type"], item[1]["value"].casefold()),
    ):
        context_summary = compact_ioc_provenance(row)
        hash_context_summary = None
        if row["type"] == ArtifactType.HASH.value:
            context_summary = abbreviated(context_summary, 120)
            contexts = tuple(
                dict.fromkeys(item["context"] for item in row["provenance"] if item.get("context"))
            )
            hash_context_summary = abbreviated(" / ".join(contexts), 36)
        origins = ioc_origins.get(ioc_key, set())
        indicator_status_label = (
            "Documenté par l'article"
            if "article" in origins
            else "IOC original du run"
            if "original" in origins
            else "Piste non confirmée"
        )
        draft_contexts = tuple(dict.fromkeys(row.pop("_draft_contexts")))
        draft_references = row.pop("_draft_references")
        if draft_contexts:
            context_summary += " · Contexte du finding : " + " / ".join(draft_contexts)
        source_ids = sorted(row.pop("_source_ids"), key=str)
        row.pop("_provenance_keys")
        row.pop("provenance")
        statuses = sorted(row.pop("statuses"), key=str.casefold)
        source_links = []
        source_refs = []
        for source_id in source_ids:
            source = source_for(source_id)
            citation_number = reference_number_by_source_id.get(source_id)
            source_links.append(
                {
                    "label": source.title or source.publisher or source.canonical_url,
                    "url": source.canonical_url,
                }
            )
            if citation_number is not None:
                source_refs.append(
                    {
                        "number": citation_number,
                        "url": source.canonical_url,
                        "target_id": reference_target_by_source_id[source_id],
                    }
                )
        row.update(
            {
                "source_role": ", ".join(
                    sorted(row["roles"], key=lambda role: str(role).casefold())
                ),
                "roles": sorted(row["roles"], key=lambda role: str(role).casefold()),
                "status": ", ".join(statuses),
                "status_label": ", ".join(statuses),
                "indicator_status_label": indicator_status_label,
                "indicator_status_short_label": (
                    "Article"
                    if "article" in origins
                    else "Original"
                    if "original" in origins
                    else "Piste"
                ),
                "relationship_status_label": ", ".join(statuses),
                "relationship_status_short_label": (
                    statuses[0] if len(statuses) == 1 else "Plusieurs statuts"
                ),
                "status_reason": "",
                "source_urls": source_links,
                "source_refs": source_refs,
                "context_summary": context_summary,
                "hash_context_summary": hash_context_summary,
                "draft_refs": list(
                    {
                        (reference["kind"], reference["reference_id"]): reference
                        for reference in draft_references
                    }.values()
                ),
            }
        )
        ioc_rows.append(row)

    ioc_group_titles = {
        "network": "Réseau — domaines, adresses IP, URL et courriels",
        "hashes": "Fichiers — empreintes cryptographiques",
        "files": "Fichiers — noms et chemins",
        "other": "Autres identifiants techniques",
    }
    grouped_ioc_rows: dict[str, list[dict[str, Any]]] = {key: [] for key in ioc_group_titles}
    for row in ioc_rows:
        artifact_type = ArtifactType(row["type"])
        group_key = (
            "network"
            if artifact_type
            in {ArtifactType.DOMAIN, ArtifactType.IP, ArtifactType.URL, ArtifactType.EMAIL}
            else "hashes"
            if artifact_type is ArtifactType.HASH
            else "files"
            if artifact_type in {ArtifactType.FILENAME, ArtifactType.FILEPATH}
            else "other"
        )
        row["type_label"] = artifact_type.value
        row["group_label"] = ioc_group_titles[group_key]
        row["source_numbers"] = ", ".join(f"[{ref['number']}]" for ref in row["source_refs"])
        grouped_ioc_rows[group_key].append(row)
    ioc_row_groups = [
        {"heading": ioc_group_titles[key], "rows": grouped_ioc_rows[key]}
        for key in ioc_group_titles
        if grouped_ioc_rows[key]
    ]
    for reference_row in source_references:
        reference_row.pop("_source_id", None)
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

    reference_section: dict[str, Any] = {
        "type": "references",
        "entries": source_references,
        # Kept as an additive alias for already-published V5/V6 payload readers.
        "timeline": list(legacy_reference_rows),
        "blocks": reference_blocks,
        "sources": sources,
    }
    content_sections: list[dict[str, Any]] = [reference_section]
    timeline_figure = (
        build_publication_timeline_figure(publication_v5)
        if isinstance(publication_v5, PublicationDocumentV5)
        and not (
            draft_preview is not None
            and draft_preview.synthesis is not None
            and draft_preview.synthesis.timeline
        )
        else None
    )
    expected_section_types = publication_content_section_types(value)
    reference_section["show_heading"] = bool(
        reference_entries or "chronology" in expected_section_types
    )
    if timeline_figure is not None:
        timeline_figure = {
            "figure_id": timeline_figure["figure_id"],
            "figure_number": figure_number + 1,
            "title": "Événements datés",
            "caption": "Dates reprises des événements cités dans les sources.",
            "publication_sha256": timeline_figure["publication_sha256"],
            "events": [
                {
                    "date": event["date"],
                    "text": event["text"],
                    "source_refs": source_links_for_ids(
                        source["source_document_id"] for source in event["sources"]
                    ),
                }
                for event in timeline_figure["events"]
            ],
        }

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
                    "source_document_ids": sorted(
                        {
                            str(reference.source_document_id)
                            for reference in synthesis_timeline_entry.evidence_refs
                            if reference.source_document_id in sources_by_id
                        }
                    ),
                }
            )
            add_source_links(chronology[-1])
    elif isinstance(publication_v5, PublicationDocumentV5) or isinstance(
        value, PublicationDocumentV4
    ):
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
    content_sections.append({"type": "synthesis", "blocks": body_blocks})
    investigation_chain_figure = (
        build_investigation_chain_figure(publication_v6) if publication_v6 is not None else None
    )
    if investigation_chain_figure is not None:
        investigation_chain_figure = {
            "figure_id": investigation_chain_figure["figure_id"],
            "title": "Références de l'avis",
            "caption": "Les appels du texte renvoient aux identifiants canoniques de l'annexe.",
            "opinion_id": investigation_chain_figure["opinion_id"],
            "opinion_version": investigation_chain_figure["opinion_version"],
            "opinion_sha256": investigation_chain_figure["opinion_sha256"],
            "base_publication_sha256": investigation_chain_figure["base_publication_sha256"],
            "rows": [
                {
                    "finding_refs": [
                        opinion_citation("finding", UUID(reference["id"]))
                        for reference in row["finding_refs"]
                    ],
                    "evidence_refs": [
                        opinion_citation("evidence", UUID(reference["id"]))
                        for reference in row["evidence_refs"]
                    ],
                }
                for row in investigation_chain_figure["rows"]
            ],
        }
    infrastructure_graph_figure = (
        build_investigation_graph_figure(publication_v5, infrastructure_graph)
        if isinstance(publication_v5, PublicationDocumentV5)
        else None
    )
    if infrastructure_graph_figure is not None:
        graph_nodes_by_id = {
            node["node_id"]: node for node in infrastructure_graph_figure.get("nodes", ())
        }
        for edge in infrastructure_graph_figure["edges"]:
            for side in ("source", "target"):
                node = graph_nodes_by_id.get(edge[f"{side}_node_id"])
                if (
                    node is not None
                    and node.get("kind") == InvestigationGraphNodeKind.FILE_SHA256.value
                ):
                    edge[f"{side}_value"] = "Empreinte de fichier"
            edge["source_value_spans"] = _breakable_typst_display_text(
                edge["source_value"], interval=32, break_at_technical_separators=True
            )
            edge["target_value_spans"] = _breakable_typst_display_text(
                edge["target_value"], interval=32, break_at_technical_separators=True
            )
            edge["finding_refs"] = [
                opinion_citation("finding", UUID(reference_id))
                for reference_id in edge["finding_refs"]
            ]
            edge["evidence_refs"] = [
                opinion_citation("evidence", UUID(reference["observation_id"]))
                for reference in edge["evidence_refs"]
                if reference.get("observation_id") is not None
            ]
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
    limitations: list[dict[str, Any]] = (
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
    limitation_source_urls = sorted(
        {url for limitation_item in limitations for url in limitation_item["source_urls"]}
    )
    limitation_source_numbers = {
        url: index for index, url in enumerate(limitation_source_urls, start=1)
    }
    for limitation_item in limitations:
        limitation_item["source_refs"] = [
            {"number": limitation_source_numbers[url], "url": url}
            for url in limitation_item["source_urls"]
        ]
    if "limitations" in expected_section_types:
        content_sections.append(
            {
                "type": "limitations",
                "heading": "Limites des preuves",
                "items": limitations,
                "source_list": [
                    {"number": index, "url": url}
                    for index, url in enumerate(limitation_source_urls, start=1)
                ],
            }
        )
    projected_opinion_section: dict[str, Any] | None = None
    opinion_figure_provenance: list[dict[str, Any]] = []
    attached_figure_ids = {item.asset_id for item in opinion.protocol_figures} if opinion else set()
    if "analyst_opinion" in expected_section_types:
        qualification_labels = {
            "fact": "Fait étayé",
            "inference": "Inférence de l'analyste",
            "hypothesis": "Hypothèse",
        }
        display_blocks: list[tuple[str, str, str, float, tuple[UUID, ...], tuple[UUID, ...]]] = []
        opinion_figures: tuple[OpinionProtocolFigureV1, ...] = ()
        unresolved_points: tuple[str, ...] = ()
        if opinion is None:
            pass
        elif rendered_opinion is not None:
            for rendered_block in rendered_opinion.blocks:
                display_blocks.append(
                    (
                        rendered_block.heading,
                        normalize_opinion_text(rendered_block.block_type, rendered_block.text),
                        rendered_block.qualification.value,
                        rendered_block.certainty,
                        rendered_block.finding_refs,
                        rendered_block.evidence_refs,
                    )
                )
            opinion_figures = tuple(
                item
                for item in rendered_opinion.protocol_figures
                if item.asset_id in attached_figure_ids
            )
            unresolved_points = rendered_opinion.unresolved_points
        else:
            for opinion_block in opinion.renderable_blocks:
                display_blocks.append(
                    (
                        _OPINION_BLOCK_HEADINGS[opinion_block.block_type],
                        normalize_opinion_text(opinion_block.block_type, opinion_block.text),
                        opinion_block.qualification.value,
                        opinion_block.certainty,
                        opinion_block.finding_refs,
                        opinion_block.evidence_refs,
                    )
                )
            opinion_figures = opinion.protocol_figures
            unresolved_points = opinion.unresolved_points
        visible_opinion_figures = tuple(
            item
            for item in opinion_figures
            if item.compiler_policy_version == PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION
        )
        opinion_figure_provenance.extend(
            {
                "kind_label": _PROTOCOL_FIGURE_TITLES[item.kind.value],
                "asset_id": str(item.asset_id),
                "model_sha256": item.model_sha256,
                "spec_sha256": item.spec_sha256,
                "asset_sha256": item.asset_sha256,
                "preview_sha256": item.preview_sha256,
                "pin_sha256": item.pin_sha256,
                "compiler_name": item.compiler_name,
                "compiler_version": item.compiler_version,
                "compiler_policy_version": item.compiler_policy_version,
                "field_evidence": [evidence.to_payload() for evidence in item.field_evidence],
            }
            for item in opinion_figures
        )
        next_figure_number = figure_number + (1 if timeline_figure is not None else 0)
        if investigation_chain_figure is not None:
            investigation_chain_figure["figure_number"] = (
                next_figure_number + len(visible_opinion_figures) + 1
            )
        if infrastructure_graph_figure is not None:
            infrastructure_graph_figure["figure_number"] = (
                next_figure_number
                + len(visible_opinion_figures)
                + (1 if investigation_chain_figure is not None else 0)
                + 1
            )
        projected_opinion_blocks: list[dict[str, Any]] = []
        for index, (
            heading,
            text,
            qualification,
            _certainty,
            finding_refs,
            evidence_refs,
        ) in enumerate(display_blocks):
            context_label = f"Bloc 1.{index + 1} — {heading}"
            projected_opinion_blocks.append(
                {
                    "number": f"1.{index + 1}",
                    "heading": heading,
                    "text": text,
                    "text_spans": _opinion_text_spans(text),
                    "pipe_table": _opinion_pipe_table(text),
                    "qualification": qualification,
                    "qualification_label": qualification_labels[qualification],
                    "finding_refs": collect_opinion_references(
                        context_label, "finding", finding_refs
                    ),
                    "evidence_refs": collect_opinion_references(
                        context_label, "evidence", evidence_refs
                    ),
                }
            )

        projected_opinion_section = {
            "type": "analyst_opinion",
            "heading": "Avis de l'analyste",
            "blocks": projected_opinion_blocks,
            "unresolved_points": list(unresolved_points),
            "unanchored_observations": unanchored_observations,
            "has_anchored_observations": any(notes_by_anchor.values()),
        }
        if investigation_chain_figure is not None:
            projected_opinion_section["investigation_chain_figure"] = investigation_chain_figure
        if infrastructure_graph_figure is not None:
            projected_opinion_section["infrastructure_graph_figure"] = infrastructure_graph_figure
        if visible_opinion_figures:

            def protocol_figure_brief(
                protocol_figure: OpinionProtocolFigureV1,
            ) -> tuple[str, str, str | None]:
                caption_lines = [
                    line.strip() for line in protocol_figure.caption.splitlines() if line.strip()
                ]
                source_title = caption_lines[0].rstrip(".") if caption_lines else ""
                has_legacy_proof_metadata = any(
                    line.startswith("Limits: ") for line in caption_lines
                ) and any(line.startswith("Proof levels: ") for line in caption_lines)
                title_prefix = _PROTOCOL_FIGURE_TITLES[protocol_figure.kind.value]

                if (
                    protocol_figure.compiler_policy_version
                    != PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION
                ):
                    source_title = source_title.partition(". ")[0].rstrip(".").strip()
                    title = f"{title_prefix} — {source_title}" if source_title else title_prefix
                    return title, "", None

                if not has_legacy_proof_metadata:
                    return title_prefix, "\n".join(caption_lines), None

                proof_line = next(
                    line.removeprefix("Proof levels: ")
                    for line in caption_lines
                    if line.startswith("Proof levels: ")
                )
                raw_limit_line = next(
                    line.removeprefix("Limits: ")
                    for line in caption_lines
                    if line.startswith("Limits: ")
                )
                limit_parts = [part.strip() for part in raw_limit_line.split(";")]
                limit_index = (
                    1
                    if len(limit_parts) > 1
                    and (
                        "ARTICLE_DOCUMENTED" in proof_line or proof_line == "INSUFFICIENT_EVIDENCE"
                    )
                    else 0
                )
                limit_line = limit_parts[min(limit_index, len(limit_parts) - 1)]
                proof_levels = proof_line.partition(" — ")[0]
                proof_label = (
                    "Niveaux de preuve : " if "," in proof_levels else "Niveau de preuve : "
                ) + proof_line
                title = f"{title_prefix} — {source_title}"
                return title, f"Limites : {limit_line}.", proof_label

            def project_protocol_figure(
                index: int, protocol_figure: OpinionProtocolFigureV1
            ) -> dict[str, Any]:
                title, caption, status_label = protocol_figure_brief(protocol_figure)
                return {
                    "type": "diagram",
                    "key": f"protocol-{protocol_figure.kind.value}",
                    "title": title,
                    "caption": caption,
                    "status_label": status_label,
                    "show_title": True,
                    "legacy_renderer": (
                        protocol_figure.compiler_policy_version
                        != PROTOCOL_DIAGRAM_FIGURE_POLICY_VERSION
                    ),
                    "status_legend": (
                        "Observation étayée : vert · inférence : orange · "
                        "non résolu : gris · contredit : rouge."
                    ),
                    "media_path": media_ref(
                        asset_id=protocol_figure.asset_id,
                        expected_kind=MediaAssetKind.DIAGRAM_SVG,
                        expected_mime_type="image/svg+xml",
                        expected_sha256=protocol_figure.asset_sha256,
                        expected_byte_size=None,
                    ).media_path,
                    # Keep canonical provenance fields in render-data; the template
                    # does not print them in the reader-facing figure.
                    "model_sha256": protocol_figure.model_sha256,
                    "spec_sha256": protocol_figure.spec_sha256,
                    "asset_sha256": protocol_figure.asset_sha256,
                    "preview_sha256": protocol_figure.preview_sha256,
                    "pin_sha256": protocol_figure.pin_sha256,
                    "compiler_name": protocol_figure.compiler_name,
                    "compiler_version": protocol_figure.compiler_version,
                    "compiler_policy_version": protocol_figure.compiler_policy_version,
                    "field_evidence": [
                        item.to_payload() for item in protocol_figure.field_evidence
                    ],
                    "figure_number": next_figure_number + index,
                }

            projected_opinion_section["protocol_figures"] = [
                project_protocol_figure(index, protocol_figure)
                for index, protocol_figure in enumerate(visible_opinion_figures, start=1)
            ]
        if not (
            projected_opinion_blocks
            or unresolved_points
            or unanchored_observations
            or investigation_chain_figure is not None
            or infrastructure_graph_figure is not None
            or visible_opinion_figures
        ):
            projected_opinion_section = None
    if projected_opinion_section is not None:
        content_sections.append(projected_opinion_section)
    implant_material_rows: list[dict[str, Any]] = []
    implant_materials_section = opinion.implant_materials_section if opinion is not None else None
    if opinion is not None and opinion.implant_materials:
        material_labels = {
            "rc4_key": "Clé RC4",
            "aes_key": "Clé AES",
            "passphrase": "Phrase de passe",
            "xor_scheme": "Schéma XOR",
            "attacker_github_c2_token": "Jeton GitHub de l'attaquant (C2)",
            "other": "Autre matériel d'implant",
        }
        for material in opinion.implant_materials:
            implant_material_rows.append(
                {
                    "type": material.type.value,
                    "type_label": material_labels[material.type.value],
                    "value": material.value,
                    "role": material.role,
                    "sample_location": material.sample_location,
                    "sample_sha256": material.sample_sha256,
                    "evidence_ref": opinion_citation("evidence", material.evidence_observation_id),
                }
            )
        content_sections.append(
            {
                "type": "technical_materials",
                "heading": "Matériel cryptographique et identifiants de l'implant",
                "attacker_identifier_note": (
                    "Les identifiants C2 de l'attaquant sont des observables passifs; "
                    "ils ne servent jamais à l'authentification."
                ),
                "editorial_text": (
                    implant_materials_section.text
                    if implant_materials_section is not None
                    else None
                ),
                "items": implant_material_rows,
            }
        )
    if detection is not None and "detection" in expected_section_types:
        content_sections.append(_publication_detection_section(detection))
    if "ioc" in expected_section_types:
        content_sections.append({"type": "ioc", **ioc_body})
    if (
        "technical_annex" in expected_section_types
        or opinion_provenance_entries
        or opinion_figure_provenance
    ):
        technical_annex = {
            "type": "technical_annex",
            "indicators": indicators,
            "original_indicators": original_indicators,
            "original_indicator_note": original_indicator_note,
            "ioc_rows": ioc_rows,
            "ioc_row_groups": ioc_row_groups,
            "opinion_refs": opinion_provenance_entries,
            "implant_identifier_rows": [
                item for item in implant_material_rows if item["type"] == "attacker_github_c2_token"
            ],
        }
        if opinion_figure_provenance:
            technical_annex["protocol_figure_provenance"] = opinion_figure_provenance
        content_sections.append(technical_annex)
    # Legacy projection consumers index every section's block list directly.
    # Empty defaults are additive; renderers still decide visibility by type.
    for projected_section in content_sections:
        projected_section.setdefault("blocks", [])

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
    block: dict[str, Any] = {
        "type": "table",
        "key": table.key,
        "title": table.title,
        "caption": table.caption,
        "columns": [column.label for column in table.columns],
        "column_weights": _table_column_weights(table),
        "rows": [list(row.cells) for row in table.rows],
    }
    if len(table.columns) > 2:
        block["card_display_cells"] = [
            [
                _component_path_display_text(cell) if "/" in cell or "\\" in cell else None
                for cell in row.cells
            ]
            for row in table.rows
        ]
    return block


def _component_path_display_text(value: str) -> dict[str, Any]:
    """Allow wrapping between path components while keeping each name intact."""
    chunks = re.split(r"(?<=[/\\])", value)
    if len(chunks) < 2:
        return {"text": value, "break_chunks": []}
    return {
        "text": value,
        "break_chunks": chunks,
        "break_after": [True] * (len(chunks) - 1),
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
    "sequence": "Séquence réseau du protocole",
    "state_machine": "Machine à états du protocole",
    "message_format": "Format de message du protocole",
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
        "evidence_classification": "documented_not_observed",
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
