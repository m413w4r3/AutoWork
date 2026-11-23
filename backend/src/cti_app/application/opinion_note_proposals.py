"""Targeted Bridge drafting of analyst notes as non-canonical proposals.

ADR-0011 D2/D4 (M24-08B).  A proposal never invents its text: ``request``
sends the drafting question through the existing ``advisor.consult`` path and
the proposal is materialised only from the persisted ``advisor_response_received``
observation, byte for byte.  A Bridge outage leaves the request pending and
unacceptable.  Human acceptance lives in
:meth:`AnalystOpinionService.accept_note_proposal`; this service never writes a
canonical opinion version.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID, uuid4

from cti_app.application.investigation_article_anchors import (
    ArticleBaseUnavailable,
    resolve_current_publication_base,
    resolve_pinned_publication_base,
)
from cti_app.application.investigation_persistence import (
    VersionConflictError,
    same_note_proposal_request,
)
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.research_context_builder import ArticleSnapshotPort
from cti_app.domain.analyst_opinion import (
    MAX_NOTE_CONTENT_BLOCKS,
    AnalystObservationRelation,
    AnalystObservationState,
    AnalystOpinionV1,
    ArticleElementNoteBlockV1,
    CanonicalCodeNoteBlockV1,
    IocTableNoteBlockV1,
    NoteArticleElementKind,
    NoteContentBlockV1,
    OpinionBlockType,
    OpinionProtocolFigureV1,
    ProtocolFigureNoteBlockV1,
    investigation_opinion_id,
    note_content_blocks_to_payload,
)
from cti_app.domain.investigation import (
    EvidenceObservation,
    ToolInvocation,
    ToolInvocationActorKind,
    ToolInvocationStatus,
    is_advisory_observation,
)
from cti_app.domain.opinion_note_proposal import (
    PROSE_BLOCK_TYPES,
    NoteProposalConsultationType,
    NoteProposalStatus,
    NoteProposalV1,
    NoteTargetBlockType,
    anchor_target_block_type,
    note_draft_text_from_sentences,
)
from cti_app.domain.production_editorial_enrichment import EnrichmentPlacementKind
from cti_app.domain.protocol_model import ProtocolDiagramKind
from cti_app.domain.publication_document import (
    CanonicalPublicationDocument,
    publication_document_article_v5_body,
    publication_element_sha256,
    publication_note_body_anchors,
)
from cti_app.domain.semantic_annotation import lead_paragraph_anchor, section_paragraph_anchor

_ADVISOR_TOOL_NAME = "advisor.consult"
_ADVISOR_CALL_SOURCE = "advisor.consult"
_ADVISOR_CALL_OBJECT_TYPE = "advisor_call"
_ADVISOR_RESPONSE_SOURCE = "advisor.consult.response"
_ADVISOR_RESPONSE_OBJECT_TYPE = "advisor_response_received"
_ADVISOR_CALL_SCHEMA = "operator-advisor-call.v1"
_ADVISOR_RESPONSE_LINK_SCHEMA = "operator-advisor-response-link.v1"
_ACCEPTED_ADVISOR_STATUSES = frozenset({"succeeded", "partial"})
_MAX_QUESTION_CHARS = 8_000


class NoteProposalErrorCode(StrEnum):
    INVESTIGATION_NOT_FOUND = "investigation_not_found"
    NOTE_PROPOSAL_NOT_FOUND = "note_proposal_not_found"
    INVALID_ANCHOR = "invalid_note_anchor"
    INVALID_TARGET = "invalid_note_target"
    INVALID_CITATION = "invalid_note_citation"
    ADVISORY_NOT_EVIDENCE = "advisory_observation_not_evidence"
    STALE_PROPOSAL = "stale_note_proposal"
    PROVENANCE_INVALID = "bridge_provenance_invalid"
    RESPONSE_PENDING = "bridge_response_pending"
    RESPONSE_UNUSABLE = "bridge_response_unusable"
    IDEMPOTENCY_CONFLICT = "idempotency_conflict"
    INVALID_STATUS = "invalid_note_proposal_status"
    NOTE_CONTENT_BLOCK_INVALID = "invalid_note_content_block"
    NOTE_BLOCK_REF_STALE = "note_block_ref_stale"
    NOTE_CHART_WITHOUT_EVIDENCE = "note_chart_without_evidence"
    NOTE_FIGURE_SOURCE_MISSING = "note_figure_source_missing"
    NOTE_CODE_REF_STALE = "note_code_ref_stale"
    NOTE_IOC_REF_UNRESOLVED = "note_ioc_ref_unresolved"


class NoteProposalError(ValueError):
    """Typed refusal for a proposal request, ingestion or transition."""

    def __init__(
        self,
        code: NoteProposalErrorCode,
        *,
        status_code: int = 422,
        message: str | None = None,
    ) -> None:
        self.code = code
        self.status_code = status_code
        super().__init__(message or code.value)


@dataclass(frozen=True, slots=True)
class NoteArticleElementFactV1:
    """One rendered article element as resolved from the pinned publication."""

    kind: str
    key: str
    element_sha256: str
    anchor: str | None
    evidence_ref_count: int
    source_document_known: bool
    title: str | None
    caption: str | None


@dataclass(frozen=True, slots=True)
class NoteObservationFactV1:
    """The canonical identity of one observation referenced by a note block."""

    observation_id: UUID
    blob_sha256: str
    object_type: str
    object_id: str
    source: str
    observed_at: datetime
    in_investigation: bool
    advisory: bool


@dataclass(frozen=True, slots=True)
class NoteContentBlockContextV1:
    """Everything a pure block validation may inspect; no I/O happens below."""

    investigation_id: UUID
    anchor: str
    elements: tuple[NoteArticleElementFactV1, ...]
    observations: Mapping[UUID, NoteObservationFactV1]
    blobs: Mapping[UUID, bytes]
    protocol_figures: tuple[OpinionProtocolFigureV1, ...]


@dataclass(frozen=True, slots=True)
class ResolvedArticleElementBlockV1:
    kind: NoteArticleElementKind
    element_key: str
    element_sha256: str
    anchor: str
    title: str | None
    caption: str | None

    def to_payload(self) -> dict[str, object]:
        return {
            "block_type": "article_element",
            "kind": self.kind.value,
            "element_key": self.element_key,
            "element_sha256": self.element_sha256,
            "anchor": self.anchor,
            "title": self.title,
            "caption": self.caption,
        }


@dataclass(frozen=True, slots=True)
class ResolvedCanonicalCodeBlockV1:
    observation_id: UUID
    blob_sha256: str
    offset: int
    length: int
    excerpt_sha256: str
    language: str
    excerpt: str
    observation_source: str
    observation_object_type: str
    observation_object_id: str

    def to_payload(self) -> dict[str, object]:
        return {
            "block_type": "canonical_code",
            "observation_id": str(self.observation_id),
            "blob_sha256": self.blob_sha256,
            "offset": self.offset,
            "length": self.length,
            "excerpt_sha256": self.excerpt_sha256,
            "language": self.language,
            "excerpt": self.excerpt,
            "observation_source": self.observation_source,
            "observation_object_type": self.observation_object_type,
            "observation_object_id": self.observation_object_id,
        }


@dataclass(frozen=True, slots=True)
class ResolvedIocTableRowV1:
    """One deterministic table row: the canonical identity of one observation.

    The row never parses provider payloads or model text; the observed object
    and its provenance are exactly the fields persisted with the observation.
    """

    observation_id: UUID
    object_type: str
    object_id: str
    source: str
    observed_at: str
    blob_sha256: str

    def to_payload(self) -> dict[str, object]:
        return {
            "observation_id": str(self.observation_id),
            "object_type": self.object_type,
            "object_id": self.object_id,
            "source": self.source,
            "observed_at": self.observed_at,
            "blob_sha256": self.blob_sha256,
        }


@dataclass(frozen=True, slots=True)
class ResolvedIocTableBlockV1:
    observation_ids: tuple[UUID, ...]
    title: str
    rows: tuple[ResolvedIocTableRowV1, ...]

    def to_payload(self) -> dict[str, object]:
        return {
            "block_type": "ioc_table",
            "observation_ids": [str(item) for item in self.observation_ids],
            "title": self.title,
            "rows": [row.to_payload() for row in self.rows],
        }


@dataclass(frozen=True, slots=True)
class ResolvedProtocolFigureBlockV1:
    kind: ProtocolDiagramKind
    asset_sha256: str
    caption: str

    def to_payload(self) -> dict[str, object]:
        return {
            "block_type": "protocol_figure",
            "kind": self.kind.value,
            "asset_sha256": self.asset_sha256,
            "caption": self.caption,
        }


ResolvedNoteContentBlockV1 = (
    ResolvedArticleElementBlockV1
    | ResolvedCanonicalCodeBlockV1
    | ResolvedIocTableBlockV1
    | ResolvedProtocolFigureBlockV1
)


def _resolved_element_anchor(
    resolved: Mapping[tuple[str, str], object],
    *,
    kind: str,
    key: str,
) -> str | None:
    placement = resolved.get((kind, key))
    if placement is None:
        return None
    paragraph_kind = getattr(placement, "paragraph_kind", None)
    paragraph_index = getattr(placement, "paragraph_index", None)
    section_index = getattr(placement, "section_index", None)
    if paragraph_kind == "lead" and isinstance(paragraph_index, int):
        return lead_paragraph_anchor(paragraph_index + 1)
    if not isinstance(paragraph_index, int):
        return None
    return section_paragraph_anchor(section_index or 0, paragraph_index + 1)


def note_article_element_facts(
    document: CanonicalPublicationDocument,
) -> tuple[NoteArticleElementFactV1, ...]:
    """Project the rendered M23 elements of one pinned publication base."""
    body = publication_document_article_v5_body(document)
    if body is None:
        return ()
    source_ids = {source.source_document_id for source in body.document.sources}
    resolved = {
        (item.element_kind, item.element_key): item
        for item in getattr(document, "resolved_placements", ())
    }
    families: tuple[tuple[str, tuple[Any, ...]], ...] = (
        ("table", body.document.tables),
        ("chart", body.document.charts),
        ("diagram", body.document.diagrams),
        ("figure", body.document.figures),
    )
    facts: list[NoteArticleElementFactV1] = []
    for kind, elements in families:
        for element in elements:
            key = element.key
            anchor: str | None = None
            placement = element.placement
            if placement.kind is EnrichmentPlacementKind.AFTER_PARAGRAPH:
                anchor = _resolved_element_anchor(resolved, kind=kind, key=key)
            if kind == "figure":
                evidence_ref_count = 0
                source_document_known = element.source_document_id in source_ids
            else:
                evidence_ref_count = len(getattr(element, "evidence_refs", ()))
                source_document_known = True
            facts.append(
                NoteArticleElementFactV1(
                    kind=kind,
                    key=key,
                    element_sha256=publication_element_sha256(body.document, kind=kind, key=key),
                    anchor=anchor,
                    evidence_ref_count=evidence_ref_count,
                    source_document_known=source_document_known,
                    title=getattr(element, "title", None),
                    caption=getattr(element, "caption", None),
                )
            )
    return tuple(facts)


def validate_note_content_blocks(
    blocks: Sequence[NoteContentBlockV1],
    context: NoteContentBlockContextV1,
) -> tuple[ResolvedNoteContentBlockV1, ...]:
    """Validate every block reference against its canonical context, purely.

    The server never accepts text from the requester: code bytes are sliced
    from the referenced observation blob and the IOC rows are the canonical
    observation identities.  A stale or foreign reference is refused with the
    typed code of its rule.
    """
    if len(blocks) > MAX_NOTE_CONTENT_BLOCKS:
        raise NoteProposalError(
            NoteProposalErrorCode.NOTE_CONTENT_BLOCK_INVALID,
            message="A note is limited to 32 content blocks",
        )
    resolved_blocks: list[ResolvedNoteContentBlockV1] = []
    for block in blocks:
        if isinstance(block, ArticleElementNoteBlockV1):
            element = next(
                (
                    item
                    for item in context.elements
                    if item.kind == block.kind.value and item.key == block.element_key
                ),
                None,
            )
            if element is None or element.element_sha256 != block.element_sha256:
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_BLOCK_REF_STALE,
                    message="The referenced article element changed or disappeared",
                )
            if (
                element.anchor is None
                or block.anchor != context.anchor
                or element.anchor != block.anchor
            ):
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_BLOCK_REF_STALE,
                    message="The referenced article element is not rendered at the note anchor",
                )
            if block.kind is NoteArticleElementKind.CHART and element.evidence_ref_count == 0:
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_CHART_WITHOUT_EVIDENCE,
                    message="A note chart requires evidence on every plotted point",
                )
            if block.kind is NoteArticleElementKind.FIGURE and not element.source_document_known:
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_FIGURE_SOURCE_MISSING,
                    message="A note figure requires a source document known to the article",
                )
            resolved_blocks.append(
                ResolvedArticleElementBlockV1(
                    kind=block.kind,
                    element_key=block.element_key,
                    element_sha256=block.element_sha256,
                    anchor=block.anchor,
                    title=element.title,
                    caption=element.caption,
                )
            )
            continue
        if isinstance(block, CanonicalCodeNoteBlockV1):
            observation = context.observations.get(block.observation_id)
            if (
                observation is None
                or not observation.in_investigation
                or observation.advisory
                or observation.blob_sha256 != block.blob_sha256
            ):
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_CODE_REF_STALE,
                    message=(
                        "The referenced observation is not a canonical source of this investigation"
                    ),
                )
            raw = context.blobs.get(block.observation_id)
            if raw is None or hashlib.sha256(raw).hexdigest() != block.blob_sha256:
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_CODE_REF_STALE,
                    message="The observation blob does not match its recorded hash",
                )
            end = block.offset + block.length
            excerpt = raw[block.offset : end]
            if (
                end > len(raw)
                or len(excerpt) != block.length
                or hashlib.sha256(excerpt).hexdigest() != block.excerpt_sha256
            ):
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_CODE_REF_STALE,
                    message="The code excerpt does not match the observation bytes",
                )
            try:
                excerpt_text = excerpt.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_CODE_REF_STALE,
                    message="The code excerpt is not UTF-8 text",
                ) from exc
            resolved_blocks.append(
                ResolvedCanonicalCodeBlockV1(
                    observation_id=block.observation_id,
                    blob_sha256=block.blob_sha256,
                    offset=block.offset,
                    length=block.length,
                    excerpt_sha256=block.excerpt_sha256,
                    language=block.language,
                    excerpt=excerpt_text,
                    observation_source=observation.source,
                    observation_object_type=observation.object_type,
                    observation_object_id=observation.object_id,
                )
            )
            continue
        if isinstance(block, IocTableNoteBlockV1):
            rows: list[ResolvedIocTableRowV1] = []
            for observation_id in block.observation_ids:
                observation = context.observations.get(observation_id)
                if observation is None or not observation.in_investigation or observation.advisory:
                    raise NoteProposalError(
                        NoteProposalErrorCode.NOTE_IOC_REF_UNRESOLVED,
                        message=(
                            "An IOC table observation is not canonical evidence of this "
                            "investigation"
                        ),
                    )
                rows.append(
                    ResolvedIocTableRowV1(
                        observation_id=observation.observation_id,
                        object_type=observation.object_type,
                        object_id=observation.object_id,
                        source=observation.source,
                        observed_at=observation.observed_at.isoformat(),
                        blob_sha256=observation.blob_sha256,
                    )
                )
            resolved_blocks.append(
                ResolvedIocTableBlockV1(
                    observation_ids=block.observation_ids,
                    title=block.title,
                    rows=tuple(rows),
                )
            )
            continue
        if isinstance(block, ProtocolFigureNoteBlockV1):
            figure = next(
                (item for item in context.protocol_figures if item.kind is block.kind), None
            )
            if figure is None or figure.asset_sha256 != block.asset_sha256:
                raise NoteProposalError(
                    NoteProposalErrorCode.NOTE_BLOCK_REF_STALE,
                    message="The protocol figure is not carried by this opinion",
                )
            resolved_blocks.append(
                ResolvedProtocolFigureBlockV1(
                    kind=block.kind,
                    asset_sha256=block.asset_sha256,
                    caption=figure.caption,
                )
            )
            continue
        raise NoteProposalError(
            NoteProposalErrorCode.NOTE_CONTENT_BLOCK_INVALID,
            message="The note content block type is unsupported",
        )
    return tuple(resolved_blocks)


async def load_note_content_block_context(
    *,
    uow: UnitOfWork,
    artifact_store: ProductionArtifactStore,
    investigation_id: UUID,
    document: CanonicalPublicationDocument,
    anchor: str,
    protocol_figures: Sequence[OpinionProtocolFigureV1],
    blocks: Sequence[NoteContentBlockV1],
) -> NoteContentBlockContextV1:
    """Read exactly the observations and blobs the requested blocks cite."""
    observation_ids: set[UUID] = set()
    code_observation_ids: set[UUID] = set()
    for block in blocks:
        if isinstance(block, IocTableNoteBlockV1):
            observation_ids.update(block.observation_ids)
        elif isinstance(block, CanonicalCodeNoteBlockV1):
            observation_ids.add(block.observation_id)
            code_observation_ids.add(block.observation_id)
    observations: dict[UUID, NoteObservationFactV1] = {}
    stored_observations: dict[UUID, EvidenceObservation] = {}
    for observation_id in observation_ids:
        observation = await uow.evidence_observations.get(observation_id)
        if observation is None:
            continue
        stored_observations[observation_id] = observation
        observations[observation_id] = NoteObservationFactV1(
            observation_id=observation.id,
            blob_sha256=observation.sha256,
            object_type=observation.object_type,
            object_id=observation.object_id,
            source=observation.source,
            observed_at=observation.observed_at,
            in_investigation=observation.investigation_id == investigation_id,
            advisory=is_advisory_observation(observation),
        )
    blobs: dict[UUID, bytes] = {}
    for observation_id in code_observation_ids:
        fact = observations.get(observation_id)
        stored = stored_observations.get(observation_id)
        if fact is None or stored is None or not fact.in_investigation or fact.advisory:
            continue
        try:
            blobs[observation_id] = await artifact_store.read_bytes(stored.blob_id, max_bytes=None)
        except (KeyError, OSError, TypeError, ValueError):
            continue
    return NoteContentBlockContextV1(
        investigation_id=investigation_id,
        anchor=anchor,
        elements=note_article_element_facts(document),
        observations=observations,
        blobs=blobs,
        protocol_figures=tuple(protocol_figures),
    )


async def resolve_note_content_blocks(
    *,
    uow: UnitOfWork,
    artifact_store: ProductionArtifactStore,
    investigation_id: UUID,
    document: CanonicalPublicationDocument,
    anchor: str,
    protocol_figures: Sequence[OpinionProtocolFigureV1],
    blocks: Sequence[NoteContentBlockV1],
) -> tuple[ResolvedNoteContentBlockV1, ...]:
    """Resolve and verify every note content block against canonical state."""
    context = await load_note_content_block_context(
        uow=uow,
        artifact_store=artifact_store,
        investigation_id=investigation_id,
        document=document,
        anchor=anchor,
        protocol_figures=protocol_figures,
        blocks=blocks,
    )
    return validate_note_content_blocks(blocks, context)


@dataclass(frozen=True, slots=True)
class AdvisorDraftingRequestV1:
    """One targeted drafting question routed through ``advisor.consult``."""

    investigation_id: UUID
    consultation_type: NoteProposalConsultationType
    anchor: str
    bridge_block_type: OpinionBlockType
    question: str
    evidence_refs: tuple[UUID, ...]
    finding_refs: tuple[UUID, ...]
    expected_working_state_version: int
    expected_working_state_sha256: str | None
    actor_id: str
    requester_actor_kind: ToolInvocationActorKind
    idempotency_key: str
    purpose: str
    context_task_id: UUID | None = None


@dataclass(frozen=True, slots=True)
class AdvisorDraftingOutcomeV1:
    invocation_id: UUID
    invocation_status: ToolInvocationStatus
    response_observation_ids: tuple[UUID, ...]
    idempotent_replay: bool


class AdvisorDraftingPort(Protocol):
    async def request_draft(
        self, uow: UnitOfWork, request: AdvisorDraftingRequestV1
    ) -> AdvisorDraftingOutcomeV1: ...


@dataclass(frozen=True, slots=True)
class NoteProposalRequestResultV1:
    proposal: NoteProposalV1
    invocation_id: UUID
    invocation_status: str
    idempotent_replay: bool
    materialised: bool


@dataclass(frozen=True, slots=True)
class NoteProposalDiffV1:
    """The review-screen projection: current note text versus the stored proposal."""

    proposal: NoteProposalV1
    current_opinion_version: int | None
    current_opinion_sha256: str | None
    current_note_id: UUID | None
    current_note_text: str | None
    anchor_excerpt: str | None
    stale: bool
    accept_ready: bool
    limits: tuple[str, ...]
    resolved_content_blocks: tuple[ResolvedNoteContentBlockV1, ...] = ()
    content_block_error: str | None = None

    @property
    def content_blocks_valid(self) -> bool:
        return self.content_block_error is None

    @property
    def text_changed(self) -> bool:
        return self.current_note_text != self.proposal.proposed_text

    def to_payload(self) -> dict[str, object]:
        return {
            "proposal": self.proposal.to_payload(),
            "current_opinion_version": self.current_opinion_version,
            "current_opinion_sha256": self.current_opinion_sha256,
            "current_note_id": (
                str(self.current_note_id) if self.current_note_id is not None else None
            ),
            "current_note_text": self.current_note_text,
            "proposed_text": self.proposal.proposed_text,
            "text_changed": self.text_changed,
            "anchor": self.proposal.anchor,
            "target_block_type": self.proposal.target_block_type.value,
            "anchor_excerpt": self.anchor_excerpt,
            "cited_finding_refs": [str(item) for item in self.proposal.cited_finding_refs],
            "cited_observation_refs": [str(item) for item in self.proposal.cited_observation_refs],
            "content_blocks": note_content_blocks_to_payload(self.proposal.content_blocks),
            "resolved_content_blocks": [
                block.to_payload() for block in self.resolved_content_blocks
            ],
            "content_blocks_valid": self.content_blocks_valid,
            "content_block_error": self.content_block_error,
            "limits": list(self.limits),
            "stale": self.stale,
            "accept_ready": self.accept_ready,
        }


@dataclass(frozen=True, slots=True)
class _MaterialisedAnswer:
    proposed_text: str
    proposed_text_sha256: str
    bridge_invocation_id: str
    bridge_output_sha256: str
    response_observation_id: UUID


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _uuid_list(payload: object) -> tuple[UUID, ...]:
    if not isinstance(payload, (list, tuple)):
        return ()
    result: list[UUID] = []
    for item in payload:
        try:
            result.append(item if isinstance(item, UUID) else UUID(str(item)))
        except (TypeError, ValueError):
            continue
    return tuple(dict.fromkeys(result))


def default_drafting_question(
    *,
    anchor: str,
    bridge_block_type: OpinionBlockType,
    finding_refs: Sequence[UUID],
    observation_refs: Sequence[UUID],
) -> str:
    """Build the bounded drafting question; cited ids stay explicit for the dossier."""
    lines = [
        f"Rédige le texte exact d'une note de l'analyste ancrée en {anchor} "
        f"(bloc {bridge_block_type.value}, forme analyst_note, unité U1).",
        "Fonde chaque affirmation sur les références citées ci-dessous et n'invente "
        "aucune mesure, aucun octet et aucune relation absente du dossier.",
        "Retourne uniquement le texte de la note, sans préambule ni commentaire.",
    ]
    if finding_refs:
        lines.append("Findings cités : " + ", ".join(f"finding {item}" for item in finding_refs))
    if observation_refs:
        lines.append(
            "Observations citées : " + ", ".join(f"observation {item}" for item in observation_refs)
        )
    return "\n".join(lines)[:_MAX_QUESTION_CHARS]


def default_review_question(*, anchor: str, bridge_block_type: OpinionBlockType) -> str:
    return (
        "Vérifie la note de l'analyste existante à l'ancre "
        f"{anchor} (bloc {bridge_block_type.value}, forme analyst_note). "
        "Signale les affirmations non soutenues, les confusions entre observation et "
        "inférence, et les limites manquantes. Ne réécris pas le texte."
    )


def advisor_consult_arguments(request: AdvisorDraftingRequestV1) -> dict[str, Any]:
    """The exact ``advisor.consult`` arguments; no text field is ever accepted."""
    arguments: dict[str, Any] = {
        "consultation_type": request.consultation_type.value,
        "question": request.question,
        "form": "analyst_note",
        "anchor": request.anchor,
        "block_type": request.bridge_block_type.value,
    }
    if request.consultation_type is NoteProposalConsultationType.DRAFT_TEXT:
        arguments["evidence_refs"] = [str(item) for item in request.evidence_refs]
    else:
        arguments["evidence_refs"] = [str(item) for item in request.evidence_refs]
    return arguments


def _block_text(block: Mapping[str, Any]) -> str | None:
    sentences = block.get("sentences")
    if not isinstance(sentences, list) or not sentences:
        return None
    texts: list[str] = []
    for item in sentences:
        if not isinstance(item, Mapping):
            return None
        text = item.get("text")
        if not isinstance(text, str):
            return None
        texts.append(text)
    return note_draft_text_from_sentences(texts)


class OpinionNoteProposalService:
    """List, diff, request, ingest, reject and cancel non-canonical note proposals."""

    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        article_snapshots: ArticleSnapshotPort | None = None,
    ) -> None:
        self._uow_factory = uow_factory
        self._artifact_store = artifact_store
        self._article_snapshots = article_snapshots

    async def list(
        self,
        investigation_id: UUID,
        *,
        status: NoteProposalStatus | None = None,
    ) -> tuple[NoteProposalV1, ...]:
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            return await uow.investigation_note_proposals.list_for_investigation(
                investigation_id, status=status
            )

    async def get(self, investigation_id: UUID, proposal_id: UUID) -> NoteProposalDiffV1:
        async with self._uow_factory() as uow:
            investigation = await self._require_investigation(uow, investigation_id)
            proposal = await self._require_proposal(uow, investigation_id, proposal_id)
            head = await uow.analyst_opinions.get(proposal.opinion_id)
            current_note = (
                next(
                    (item for item in head.observations if item.anchor == proposal.anchor),
                    None,
                )
                if head is not None
                else None
            )
            stale = proposal.status is NoteProposalStatus.PROPOSED and not self._base_matches(
                proposal, head
            )
            resolved_blocks, block_error = await self._resolve_blocks_for_display(
                uow, investigation, proposal, head
            )
            limits: tuple[str, ...] = ()
            if proposal.bridge_response_observation_id is not None:
                response_observation = await uow.evidence_observations.get(
                    proposal.bridge_response_observation_id
                )
                if response_observation is not None:
                    limits = response_observation.limitations
            return NoteProposalDiffV1(
                proposal=proposal,
                current_opinion_version=head.version if head is not None else None,
                current_opinion_sha256=head.content_sha256 if head is not None else None,
                current_note_id=current_note.id if current_note is not None else None,
                current_note_text=current_note.text if current_note is not None else None,
                anchor_excerpt=await self._anchor_excerpt(uow, investigation, proposal),
                stale=stale,
                accept_ready=(
                    proposal.status is NoteProposalStatus.PROPOSED
                    and proposal.materialised
                    and not stale
                    and block_error is None
                ),
                limits=limits,
                resolved_content_blocks=resolved_blocks,
                content_block_error=block_error,
            )

    async def request(
        self,
        investigation_id: UUID,
        *,
        anchor: str,
        target_block_type: NoteTargetBlockType,
        bridge_block_type: OpinionBlockType,
        consultation_type: NoteProposalConsultationType,
        note_state: AnalystObservationState,
        note_relation: AnalystObservationRelation,
        finding_refs: Sequence[UUID] = (),
        observation_refs: Sequence[UUID] = (),
        content_blocks: Sequence[NoteContentBlockV1] = (),
        base_opinion_version: int | None,
        base_opinion_sha256: str | None,
        task_id: UUID | None,
        question: str | None,
        expected_working_state_version: int,
        expected_working_state_sha256: str | None,
        actor_id: str,
        requester_actor_kind: ToolInvocationActorKind,
        idempotency_key: str,
        advisor: AdvisorDraftingPort,
        now: datetime | None = None,
    ) -> NoteProposalRequestResultV1:
        timestamp = now or datetime.now(UTC)
        if not idempotency_key.strip():
            raise NoteProposalError(
                NoteProposalErrorCode.IDEMPOTENCY_CONFLICT,
                status_code=422,
                message="A note proposal request requires an idempotency key",
            )
        if expected_working_state_version < 1:
            raise NoteProposalError(NoteProposalErrorCode.INVALID_STATUS, status_code=422)
        if bridge_block_type not in PROSE_BLOCK_TYPES:
            raise NoteProposalError(
                NoteProposalErrorCode.INVALID_TARGET,
                message="A note proposal must target a publishable opinion block",
            )
        self._require_anchor_target(anchor, target_block_type)
        self._require_base_pair(base_opinion_version, base_opinion_sha256)
        blocks = tuple(content_blocks)
        if len(blocks) > MAX_NOTE_CONTENT_BLOCKS:
            raise NoteProposalError(
                NoteProposalErrorCode.NOTE_CONTENT_BLOCK_INVALID,
                message="A note is limited to 32 content blocks",
            )
        if consultation_type is NoteProposalConsultationType.REVIEW_TEXT and blocks:
            raise NoteProposalError(
                NoteProposalErrorCode.NOTE_CONTENT_BLOCK_INVALID,
                message="A review proposal cannot add note content blocks",
            )
        cited_findings = tuple(dict.fromkeys(finding_refs))
        cited_observations = tuple(dict.fromkeys(observation_refs))
        async with self._uow_factory() as uow:
            investigation = await self._require_investigation(uow, investigation_id)
            opinion_id = investigation_opinion_id(investigation_id)
            existing = await uow.investigation_note_proposals.get_by_idempotency(
                investigation_id, idempotency_key
            )
            if existing is not None:
                # A replay compares the body against the stored request identity.
                # The pinned base pair and the reviewed note are derived at first
                # request time, so they come from the stored row, never from the
                # (possibly moved) current head.
                candidate = self._build_proposal(
                    investigation_id=investigation_id,
                    opinion_id=opinion_id,
                    anchor=anchor,
                    target_block_type=target_block_type,
                    bridge_block_type=bridge_block_type,
                    consultation_type=consultation_type,
                    base_note_id=existing.base_note_id,
                    note_state=note_state,
                    note_relation=note_relation,
                    cited_findings=cited_findings,
                    cited_observations=cited_observations,
                    base_opinion_version=existing.base_opinion_version,
                    base_opinion_sha256=existing.base_opinion_sha256,
                    base_publication_artifact_id=existing.base_publication_artifact_id,
                    base_publication_version=existing.base_publication_version,
                    base_publication_sha256=existing.base_publication_sha256,
                    anchor_before_sha256=existing.anchor_before_sha256,
                    content_blocks=blocks,
                    task_id=task_id,
                    idempotency_key=idempotency_key,
                    now=timestamp,
                )
                if not same_note_proposal_request(existing, candidate):
                    raise NoteProposalError(
                        NoteProposalErrorCode.IDEMPOTENCY_CONFLICT,
                        status_code=409,
                        message="The idempotency key records a different proposal request",
                    )
                invocation = (
                    await uow.tool_invocations.get(existing.tool_invocation_id)
                    if existing.tool_invocation_id is not None
                    else None
                )
                return NoteProposalRequestResultV1(
                    proposal=existing,
                    invocation_id=(invocation.id if invocation is not None else existing.id),
                    invocation_status=(
                        invocation.status.value if invocation is not None else "unknown"
                    ),
                    idempotent_replay=True,
                    materialised=existing.materialised,
                )

            await self._validate_citations(
                uow, investigation_id, cited_findings, cited_observations
            )
            head = await uow.analyst_opinions.get(opinion_id)
            self._require_base_matches_head(base_opinion_version, base_opinion_sha256, head)
            pinned_base_version, pinned_base_sha256 = (
                (head.version, head.content_sha256)
                if base_opinion_version is None and head is not None
                else (base_opinion_version, base_opinion_sha256)
            )
            # The article base is pinned now and validated against exactly this
            # base: a later publication revision makes the proposal unacceptable
            # (``stale_base``) instead of silently moving the note.
            try:
                base = (
                    await resolve_pinned_publication_base(
                        uow,
                        self._artifact_store,
                        artifact_id=head.base_publication_artifact_id,
                        sha256=head.base_publication_sha256,
                    )
                    if head is not None
                    else await resolve_current_publication_base(
                        uow, self._artifact_store, investigation
                    )
                )
            except ArticleBaseUnavailable as exc:
                raise NoteProposalError(
                    NoteProposalErrorCode.INVALID_ANCHOR,
                    message="The article anchors are unavailable",
                ) from exc
            anchor_text = publication_note_body_anchors(base.document).get(anchor)
            if anchor_text is None:
                raise NoteProposalError(
                    NoteProposalErrorCode.INVALID_ANCHOR,
                    message="The anchor does not exist in the pinned publication base",
                )
            anchor_before_sha256 = hashlib.sha256(anchor_text.encode("utf-8")).hexdigest()
            # Blocks are verified against exactly this pinned base and the
            # current canonical observations before Bridge is ever called.
            if blocks:
                await resolve_note_content_blocks(
                    uow=uow,
                    artifact_store=self._artifact_store,
                    investigation_id=investigation_id,
                    document=base.document,
                    anchor=anchor,
                    protocol_figures=head.protocol_figures if head is not None else (),
                    blocks=blocks,
                )
            base_note_id: UUID | None = None
            if consultation_type is NoteProposalConsultationType.REVIEW_TEXT:
                if head is None:
                    raise NoteProposalError(
                        NoteProposalErrorCode.PROVENANCE_INVALID,
                        message="A review proposal requires an existing analyst note",
                    )
                base_note = next(
                    (item for item in head.observations if item.anchor == anchor), None
                )
                if base_note is None:
                    raise NoteProposalError(
                        NoteProposalErrorCode.INVALID_ANCHOR,
                        message="A review proposal requires an existing note at its anchor",
                    )
                if base_note.bridge_draft_ref is None:
                    raise NoteProposalError(
                        NoteProposalErrorCode.PROVENANCE_INVALID,
                        message="A review proposal requires a Bridge-drafted note",
                    )
                base_note_id = base_note.id

            draft_question = (
                question.strip()
                if isinstance(question, str) and question.strip()
                else default_drafting_question(
                    anchor=anchor,
                    bridge_block_type=bridge_block_type,
                    finding_refs=cited_findings,
                    observation_refs=cited_observations,
                )
                if consultation_type is NoteProposalConsultationType.DRAFT_TEXT
                else default_review_question(anchor=anchor, bridge_block_type=bridge_block_type)
            )
            outcome = await advisor.request_draft(
                uow,
                AdvisorDraftingRequestV1(
                    investigation_id=investigation_id,
                    consultation_type=consultation_type,
                    anchor=anchor,
                    bridge_block_type=bridge_block_type,
                    question=draft_question[:_MAX_QUESTION_CHARS],
                    evidence_refs=cited_observations,
                    finding_refs=cited_findings,
                    expected_working_state_version=expected_working_state_version,
                    expected_working_state_sha256=expected_working_state_sha256,
                    actor_id=actor_id,
                    requester_actor_kind=requester_actor_kind,
                    idempotency_key=idempotency_key,
                    purpose=("Demande ciblée de rédaction d'une note de l'analyste (M24)."),
                    # F4-D B3: the human request is linked to its task context;
                    # the proposal's own task_id stays a proposal field.
                    context_task_id=task_id,
                ),
            )
            proposal = self._build_proposal(
                investigation_id=investigation_id,
                opinion_id=opinion_id,
                anchor=anchor,
                target_block_type=target_block_type,
                bridge_block_type=bridge_block_type,
                consultation_type=consultation_type,
                base_note_id=base_note_id,
                note_state=note_state,
                note_relation=note_relation,
                cited_findings=cited_findings,
                cited_observations=cited_observations,
                base_opinion_version=pinned_base_version,
                base_opinion_sha256=pinned_base_sha256,
                base_publication_artifact_id=base.artifact.id,
                base_publication_version=base.artifact.version,
                base_publication_sha256=base.sha256,
                anchor_before_sha256=anchor_before_sha256,
                content_blocks=blocks,
                task_id=task_id,
                idempotency_key=idempotency_key,
                now=timestamp,
                tool_invocation_id=outcome.invocation_id,
            )
            try:
                stored, created = await uow.investigation_note_proposals.append_if_absent(proposal)
            except VersionConflictError as exc:
                raise NoteProposalError(
                    NoteProposalErrorCode.IDEMPOTENCY_CONFLICT,
                    status_code=409,
                    message=str(exc),
                ) from exc
            if not created:
                invocation = await uow.tool_invocations.get(outcome.invocation_id)
                return NoteProposalRequestResultV1(
                    proposal=stored,
                    invocation_id=outcome.invocation_id,
                    invocation_status=(
                        invocation.status.value if invocation is not None else "unknown"
                    ),
                    idempotent_replay=True,
                    materialised=stored.materialised,
                )
            materialised = await self._try_materialise(uow, stored, head=head)
            if materialised is not None:
                stored = materialised
            await uow.commit()
            return NoteProposalRequestResultV1(
                proposal=stored,
                invocation_id=outcome.invocation_id,
                invocation_status=outcome.invocation_status.value,
                idempotent_replay=outcome.idempotent_replay,
                materialised=stored.materialised,
            )

    async def ingest_response(
        self,
        investigation_id: UUID,
        proposal_id: UUID,
        *,
        expected_status_version: int,
        actor: str,
        now: datetime | None = None,
    ) -> NoteProposalV1:
        """Materialise the proposal from the persisted Bridge answer, exactly."""
        del actor  # ingestion is a system write; the actor is audited by the caller
        timestamp = now or datetime.now(UTC)
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            proposal = await self._require_proposal(uow, investigation_id, proposal_id)
            self._require_proposed(proposal, expected_status_version)
            if proposal.materialised:
                return proposal
            head = await uow.analyst_opinions.get(proposal.opinion_id)
            if not self._base_matches(proposal, head):
                await self._mark_stale(uow, proposal, timestamp)
                await uow.commit()
                raise NoteProposalError(
                    NoteProposalErrorCode.STALE_PROPOSAL,
                    status_code=409,
                    message="The opinion head moved after the drafting request",
                )
            answer = await self._materialised_answer(uow, proposal, head=head)
            updated = await uow.investigation_note_proposals.update_if_version(
                proposal.with_materialisation(
                    proposed_text=answer.proposed_text,
                    proposed_text_sha256=answer.proposed_text_sha256,
                    bridge_invocation_id=answer.bridge_invocation_id,
                    bridge_output_sha256=answer.bridge_output_sha256,
                    bridge_response_observation_id=answer.response_observation_id,
                    now=timestamp,
                ),
                expected_status_version=proposal.status_version,
            )
            if updated is None:
                raise NoteProposalError(NoteProposalErrorCode.STALE_PROPOSAL, status_code=409)
            await uow.commit()
            return updated

    async def reject(
        self,
        investigation_id: UUID,
        proposal_id: UUID,
        *,
        expected_status_version: int,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> NoteProposalV1:
        del actor
        if not reason.strip():
            raise NoteProposalError(
                NoteProposalErrorCode.INVALID_STATUS,
                message="A rejection requires a reason",
            )
        return await self._transition(
            investigation_id,
            proposal_id,
            expected_status_version=expected_status_version,
            status=NoteProposalStatus.REJECTED,
            now=now,
        )

    async def cancel(
        self,
        investigation_id: UUID,
        proposal_id: UUID,
        *,
        expected_status_version: int,
        actor: str,
        reason: str,
        now: datetime | None = None,
    ) -> NoteProposalV1:
        del actor
        if not reason.strip():
            raise NoteProposalError(
                NoteProposalErrorCode.INVALID_STATUS,
                message="A cancellation requires a reason",
            )
        return await self._transition(
            investigation_id,
            proposal_id,
            expected_status_version=expected_status_version,
            status=NoteProposalStatus.CANCELLED,
            now=now,
        )

    async def _transition(
        self,
        investigation_id: UUID,
        proposal_id: UUID,
        *,
        expected_status_version: int,
        status: NoteProposalStatus,
        now: datetime | None,
    ) -> NoteProposalV1:
        timestamp = now or datetime.now(UTC)
        async with self._uow_factory() as uow:
            await self._require_investigation(uow, investigation_id)
            proposal = await self._require_proposal(uow, investigation_id, proposal_id)
            self._require_proposed(proposal, expected_status_version)
            updated = await uow.investigation_note_proposals.update_if_version(
                proposal.with_status(status, now=timestamp),
                expected_status_version=proposal.status_version,
            )
            if updated is None:
                raise NoteProposalError(NoteProposalErrorCode.STALE_PROPOSAL, status_code=409)
            await uow.commit()
            return updated

    async def _try_materialise(
        self,
        uow: UnitOfWork,
        proposal: NoteProposalV1,
        *,
        head: AnalystOpinionV1 | None,
        now: datetime | None = None,
    ) -> NoteProposalV1 | None:
        try:
            answer = await self._materialised_answer(uow, proposal, head=head)
        except NoteProposalError:
            return None
        updated = await uow.investigation_note_proposals.update_if_version(
            proposal.with_materialisation(
                proposed_text=answer.proposed_text,
                proposed_text_sha256=answer.proposed_text_sha256,
                bridge_invocation_id=answer.bridge_invocation_id,
                bridge_output_sha256=answer.bridge_output_sha256,
                bridge_response_observation_id=answer.response_observation_id,
                now=now or datetime.now(UTC),
            ),
            expected_status_version=proposal.status_version,
        )
        if updated is None:
            raise NoteProposalError(NoteProposalErrorCode.STALE_PROPOSAL, status_code=409)
        return updated

    async def _materialised_answer(
        self,
        uow: UnitOfWork,
        proposal: NoteProposalV1,
        *,
        head: AnalystOpinionV1 | None,
    ) -> _MaterialisedAnswer:
        if proposal.tool_invocation_id is None:
            raise NoteProposalError(
                NoteProposalErrorCode.RESPONSE_PENDING,
                status_code=202,
                message="The drafting request has no advisor invocation yet",
            )
        invocation = await uow.tool_invocations.get(proposal.tool_invocation_id)
        if (
            invocation is None
            or invocation.investigation_id != proposal.investigation_id
            or invocation.tool_name != _ADVISOR_TOOL_NAME
        ):
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The proposal does not reference a persisted advisor consultation",
            )
        responses = await self._response_observations(uow, invocation)
        if not responses:
            raise NoteProposalError(
                NoteProposalErrorCode.RESPONSE_PENDING,
                status_code=202,
                message="Bridge has not returned a persisted answer yet",
            )
        calls = await self._call_observations(uow, invocation)
        if len(calls) != 1:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The advisor consultation must have exactly one persisted call",
            )
        _call, call_payload = calls[0]
        if call_payload.get("consultation_type") != proposal.consultation_type.value:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The persisted consultation type does not match the proposal",
            )
        response, response_payload = await self._verified_response_link(uow, responses[-1])
        output_sha256 = call_payload.get("output_sha256")
        if not isinstance(output_sha256, str):
            raise NoteProposalError(NoteProposalErrorCode.PROVENANCE_INVALID)
        if response_payload.get("raw_response_sha256") != output_sha256:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The archived Bridge answer hash does not match the consultation",
            )
        bridge_invocation_id = call_payload.get("bridge_invocation_id")
        if not isinstance(bridge_invocation_id, str) or not self._bridge_call_has_real_provenance(
            call_payload
        ):
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The persisted consultation does not carry real Bridge provenance",
            )
        if proposal.consultation_type is NoteProposalConsultationType.DRAFT_TEXT:
            proposed_text = self._draft_text_for_target(call_payload, proposal)
        else:
            proposed_text = await self._reviewed_text_for_target(proposal, call_payload, head=head)
        return _MaterialisedAnswer(
            proposed_text=proposed_text,
            proposed_text_sha256=_sha256_text(proposed_text),
            bridge_invocation_id=bridge_invocation_id,
            bridge_output_sha256=output_sha256,
            response_observation_id=response.id,
        )

    async def _response_observations(
        self, uow: UnitOfWork, invocation: ToolInvocation
    ) -> tuple[EvidenceObservation, ...]:
        result: list[EvidenceObservation] = []
        for observation_id in invocation.response_refs:
            observation = await uow.evidence_observations.get(observation_id)
            if (
                observation is not None
                and observation.investigation_id == invocation.investigation_id
                and observation.source == _ADVISOR_RESPONSE_SOURCE
                and observation.object_type == _ADVISOR_RESPONSE_OBJECT_TYPE
            ):
                result.append(observation)
        return tuple(sorted(result, key=lambda item: (item.observed_at, str(item.id))))

    async def _call_observations(
        self, uow: UnitOfWork, invocation: ToolInvocation
    ) -> tuple[tuple[EvidenceObservation, Mapping[str, Any]], ...]:
        result: list[tuple[EvidenceObservation, Mapping[str, Any]]] = []
        for observation_id in invocation.response_refs:
            observation = await uow.evidence_observations.get(observation_id)
            if (
                observation is None
                or observation.investigation_id != invocation.investigation_id
                or observation.source != _ADVISOR_CALL_SOURCE
                or observation.object_type != _ADVISOR_CALL_OBJECT_TYPE
            ):
                continue
            payload = await self._verified_json(observation)
            if not isinstance(payload, Mapping) or payload.get("schema") != _ADVISOR_CALL_SCHEMA:
                raise NoteProposalError(
                    NoteProposalErrorCode.PROVENANCE_INVALID,
                    status_code=422,
                    message="The persisted advisor call payload is unsupported",
                )
            result.append((observation, payload))
        return tuple(result)

    async def _verified_response_link(
        self, uow: UnitOfWork, observation: EvidenceObservation
    ) -> tuple[EvidenceObservation, Mapping[str, Any]]:
        payload = await self._verified_json(observation)
        if (
            not isinstance(payload, Mapping)
            or payload.get("schema") != _ADVISOR_RESPONSE_LINK_SCHEMA
            or payload.get("stage") != "response_received"
        ):
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The persisted Bridge response link is unsupported",
            )
        raw_sha256 = payload.get("raw_response_sha256")
        raw_blob_id = payload.get("raw_response_blob_id")
        if not isinstance(raw_sha256, str) or not isinstance(raw_blob_id, str):
            raise NoteProposalError(NoteProposalErrorCode.PROVENANCE_INVALID, status_code=422)
        try:
            raw_bytes = await self._artifact_store.read_bytes(UUID(raw_blob_id), max_bytes=None)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise NoteProposalError(
                NoteProposalErrorCode.RESPONSE_UNUSABLE,
                status_code=422,
                message="The archived Bridge answer is no longer readable",
            ) from exc
        if _sha256_bytes(raw_bytes) != raw_sha256:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The archived Bridge answer does not match its recorded hash",
            )
        return observation, payload

    async def _verified_json(self, observation: EvidenceObservation) -> object:
        try:
            raw = await self._artifact_store.read_bytes(observation.blob_id, max_bytes=None)
        except (KeyError, OSError, TypeError, ValueError) as exc:
            raise NoteProposalError(
                NoteProposalErrorCode.RESPONSE_UNUSABLE,
                status_code=422,
                message="The persisted advisor observation is no longer readable",
            ) from exc
        if _sha256_bytes(raw) != observation.sha256:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The persisted advisor observation does not match its hash",
            )
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The persisted advisor observation is not valid JSON",
            ) from exc

    @staticmethod
    def _bridge_call_has_real_provenance(call: Mapping[str, Any]) -> bool:
        """Mirror the ADR-0011 D4 provenance predicate for a persisted call."""
        invocation_id = call.get("bridge_invocation_id")
        output_sha256 = call.get("output_sha256")
        output = call.get("output")
        provenance = output.get("bridge_provenance") if isinstance(output, Mapping) else None
        if (
            call.get("advisor_status") not in _ACCEPTED_ADVISOR_STATUSES
            or call.get("bridge_invocation_id_kind") != "conversation_turn"
            or not isinstance(invocation_id, str)
            or not isinstance(output_sha256, str)
            or len(output_sha256) != 64
            or any(character not in "0123456789abcdef" for character in output_sha256)
            or not isinstance(output, Mapping)
            or not isinstance(provenance, Mapping)
            or provenance.get("bridge_invocation_id") != invocation_id
            or provenance.get("bridge_invocation_id_kind") != "conversation_turn"
            or provenance.get("output_sha256") != output_sha256
            or output.get("output_sha256") != output_sha256
            or not isinstance(call.get("input_refs"), list)
            or provenance.get("input_refs") != call.get("input_refs")
        ):
            return False
        try:
            return str(UUID(invocation_id)) == invocation_id
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _draft_text_for_target(call: Mapping[str, Any], proposal: NoteProposalV1) -> str:
        output = call.get("output")
        if not isinstance(output, Mapping):
            raise NoteProposalError(NoteProposalErrorCode.PROVENANCE_INVALID)
        usable_output = output.get("usable") is True
        matched: set[str] = set()
        for block in output.get("blocks", ()):
            if not isinstance(block, Mapping):
                continue
            if (
                block.get("block_type") != proposal.bridge_block_type.value
                or block.get("form") != "analyst_note"
                or block.get("anchor") != proposal.anchor
            ):
                continue
            block_usable = block.get("usable") is True or (
                "usable" not in block
                and usable_output
                and call.get("advisor_status") == "succeeded"
            )
            if not block_usable:
                continue
            text = _block_text(block)
            if text is None or not text.strip():
                continue
            matched.add(text)
        if not matched:
            raise NoteProposalError(
                NoteProposalErrorCode.RESPONSE_UNUSABLE,
                status_code=422,
                message="The Bridge answer contains no usable note draft for this anchor",
            )
        if len(matched) > 1:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The Bridge answer contains ambiguous note drafts for this anchor",
            )
        return matched.pop()

    async def _reviewed_text_for_target(
        self,
        proposal: NoteProposalV1,
        call: Mapping[str, Any],
        *,
        head: AnalystOpinionV1 | None,
    ) -> str:
        if head is None or proposal.base_note_id is None:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                status_code=422,
                message="The reviewed note is no longer available",
            )
        note = next((item for item in head.observations if item.id == proposal.base_note_id), None)
        if note is None or note.anchor != proposal.anchor:
            raise NoteProposalError(
                NoteProposalErrorCode.STALE_PROPOSAL,
                status_code=409,
                message="The reviewed note is no longer in the current opinion",
            )
        reviewed_sha256 = _sha256_text(note.text)
        units = call.get("review_units")
        review_units = units if isinstance(units, list) else []
        matching_units = [
            unit
            for unit in review_units
            if isinstance(unit, Mapping)
            and unit.get("reviewed_text_sha256") == reviewed_sha256
            and unit.get("form") == "analyst_note"
            and unit.get("block_type") == proposal.bridge_block_type.value
            and unit.get("anchor") == proposal.anchor
        ]
        output = call.get("output")
        reviews = output.get("reviews", ()) if isinstance(output, Mapping) else ()
        usable = False
        if isinstance(reviews, (list, tuple)):
            for unit in matching_units:
                for review in reviews:
                    if not (
                        isinstance(review, Mapping)
                        and review.get("unit_id") == unit.get("unit_id")
                        and isinstance(review.get("remarks"), list)
                        and review.get("remarks")
                    ):
                        continue
                    if review.get("usable") is True or (
                        "usable" not in review
                        and isinstance(output, Mapping)
                        and output.get("usable") is True
                        and call.get("advisor_status") == "succeeded"
                    ):
                        usable = True
        if not usable:
            raise NoteProposalError(
                NoteProposalErrorCode.RESPONSE_UNUSABLE,
                status_code=422,
                message="The Bridge answer contains no usable review for this note",
            )
        return note.text

    @staticmethod
    def _base_matches(proposal: NoteProposalV1, head: AnalystOpinionV1 | None) -> bool:
        if head is None:
            return proposal.base_opinion_version is None and proposal.base_opinion_sha256 is None
        return (
            proposal.base_opinion_version == head.version
            and proposal.base_opinion_sha256 == head.content_sha256
        )

    async def _mark_stale(self, uow: UnitOfWork, proposal: NoteProposalV1, now: datetime) -> None:
        updated = await uow.investigation_note_proposals.update_if_version(
            proposal.with_status(NoteProposalStatus.STALE, now=now),
            expected_status_version=proposal.status_version,
        )
        if updated is None:
            raise NoteProposalError(NoteProposalErrorCode.STALE_PROPOSAL, status_code=409)

    @staticmethod
    def _require_anchor_target(anchor: str, target_block_type: NoteTargetBlockType) -> None:
        try:
            expected = anchor_target_block_type(anchor)
        except ValueError as exc:
            raise NoteProposalError(NoteProposalErrorCode.INVALID_ANCHOR, message=str(exc)) from exc
        if target_block_type is not expected:
            raise NoteProposalError(
                NoteProposalErrorCode.INVALID_TARGET,
                message="The target block type does not match the anchor",
            )

    @staticmethod
    def _require_base_pair(
        base_opinion_version: int | None, base_opinion_sha256: str | None
    ) -> None:
        if (base_opinion_version is None) != (base_opinion_sha256 is None):
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                message="The base opinion version and hash are a pair",
            )
        if base_opinion_version is not None and base_opinion_version < 1:
            raise NoteProposalError(
                NoteProposalErrorCode.PROVENANCE_INVALID,
                message="The base opinion version must be >= 1",
            )

    @staticmethod
    def _require_base_matches_head(
        base_opinion_version: int | None,
        base_opinion_sha256: str | None,
        head: AnalystOpinionV1 | None,
    ) -> None:
        if base_opinion_version is None:
            return
        if (
            head is None
            or head.version != base_opinion_version
            or head.content_sha256 != base_opinion_sha256
        ):
            raise NoteProposalError(
                NoteProposalErrorCode.STALE_PROPOSAL,
                status_code=409,
                message="The requested base opinion is not the current head",
            )

    @staticmethod
    def _require_proposed(proposal: NoteProposalV1, expected_status_version: int) -> None:
        if proposal.status is not NoteProposalStatus.PROPOSED:
            raise NoteProposalError(
                NoteProposalErrorCode.INVALID_STATUS,
                status_code=409,
                message=f"The proposal is already {proposal.status.value}",
            )
        if proposal.status_version != expected_status_version:
            raise NoteProposalError(
                NoteProposalErrorCode.STALE_PROPOSAL,
                status_code=409,
                message="The proposal status version changed",
            )

    async def _require_investigation(self, uow: UnitOfWork, investigation_id: UUID) -> Any:
        investigation = await uow.analyst_investigations.get(investigation_id)
        if investigation is None:
            raise NoteProposalError(NoteProposalErrorCode.INVESTIGATION_NOT_FOUND, status_code=404)
        return investigation

    async def _require_proposal(
        self, uow: UnitOfWork, investigation_id: UUID, proposal_id: UUID
    ) -> NoteProposalV1:
        proposal = await uow.investigation_note_proposals.get(proposal_id)
        if proposal is None or proposal.investigation_id != investigation_id:
            raise NoteProposalError(NoteProposalErrorCode.NOTE_PROPOSAL_NOT_FOUND, status_code=404)
        return proposal

    async def _validate_citations(
        self,
        uow: UnitOfWork,
        investigation_id: UUID,
        finding_refs: tuple[UUID, ...],
        observation_refs: tuple[UUID, ...],
    ) -> None:
        findings = await uow.investigation_findings.list_for_investigation(investigation_id)
        superseded = {
            item.supersedes for item in findings if getattr(item, "supersedes", None) is not None
        }
        current = {
            item.id: item
            for item in findings
            if item.investigation_id == investigation_id and item.id not in superseded
        }
        observations = {
            item.id: item
            for item in await uow.evidence_observations.list_for_investigation(investigation_id)
            if item.investigation_id == investigation_id
        }
        missing_findings = [item for item in finding_refs if item not in current]
        if missing_findings:
            raise NoteProposalError(
                NoteProposalErrorCode.INVALID_CITATION,
                message="A cited finding is absent from the investigation",
            )
        for item in observation_refs:
            observation = observations.get(item)
            if observation is None:
                raise NoteProposalError(
                    NoteProposalErrorCode.INVALID_CITATION,
                    message="A cited observation is absent from the investigation",
                )
            if is_advisory_observation(observation):
                raise NoteProposalError(
                    NoteProposalErrorCode.ADVISORY_NOT_EVIDENCE,
                    message="Bridge advisory material cannot be cited as note evidence",
                )

    async def _resolve_blocks_for_display(
        self,
        uow: UnitOfWork,
        investigation: Any,
        proposal: NoteProposalV1,
        head: AnalystOpinionV1 | None,
    ) -> tuple[tuple[ResolvedNoteContentBlockV1, ...], str | None]:
        """Resolve the review-screen preview; a refusal is reported, never hidden."""
        if not proposal.content_blocks:
            return (), None
        try:
            base = (
                await resolve_pinned_publication_base(
                    uow,
                    self._artifact_store,
                    artifact_id=proposal.base_publication_artifact_id,
                    sha256=proposal.base_publication_sha256,
                )
                if proposal.base_publication_artifact_id is not None
                and proposal.base_publication_sha256 is not None
                else await resolve_current_publication_base(
                    uow, self._artifact_store, investigation
                )
            )
        except ArticleBaseUnavailable:
            return (), NoteProposalErrorCode.NOTE_BLOCK_REF_STALE.value
        try:
            resolved = await resolve_note_content_blocks(
                uow=uow,
                artifact_store=self._artifact_store,
                investigation_id=proposal.investigation_id,
                document=base.document,
                anchor=proposal.anchor,
                protocol_figures=head.protocol_figures if head is not None else (),
                blocks=proposal.content_blocks,
            )
        except NoteProposalError as exc:
            return (), exc.code.value
        return resolved, None

    async def _anchor_excerpt(
        self, uow: UnitOfWork, investigation: Any, proposal: NoteProposalV1
    ) -> str | None:
        if (
            proposal.base_publication_artifact_id is not None
            and proposal.base_publication_sha256 is not None
        ):
            try:
                base = await resolve_pinned_publication_base(
                    uow,
                    self._artifact_store,
                    artifact_id=proposal.base_publication_artifact_id,
                    sha256=proposal.base_publication_sha256,
                )
                text = publication_note_body_anchors(base.document).get(proposal.anchor)
            except ArticleBaseUnavailable:
                return None
            if text is None:
                return None
            return " ".join(text.split())[:160].rstrip() or None
        # Proposals created before the identity pin have no base to read; keep
        # their historical excerpt from the current article snapshot.
        if self._article_snapshots is None:
            return None
        try:
            snapshot = await self._article_snapshots.resolve(investigation.synthesis_artifact_id)
        except Exception:
            return None
        for item in snapshot.article.publication_anchors:
            if item.anchor == proposal.anchor:
                return item.excerpt
        return None

    @staticmethod
    def _build_proposal(
        *,
        investigation_id: UUID,
        opinion_id: UUID,
        anchor: str,
        target_block_type: NoteTargetBlockType,
        bridge_block_type: OpinionBlockType,
        consultation_type: NoteProposalConsultationType,
        base_note_id: UUID | None,
        note_state: AnalystObservationState,
        note_relation: AnalystObservationRelation,
        cited_findings: tuple[UUID, ...],
        cited_observations: tuple[UUID, ...],
        base_opinion_version: int | None,
        base_opinion_sha256: str | None,
        base_publication_artifact_id: UUID | None,
        base_publication_version: int | None,
        base_publication_sha256: str | None,
        anchor_before_sha256: str | None,
        content_blocks: tuple[NoteContentBlockV1, ...],
        task_id: UUID | None,
        idempotency_key: str,
        now: datetime,
        tool_invocation_id: UUID | None = None,
    ) -> NoteProposalV1:
        return NoteProposalV1(
            id=uuid4(),
            investigation_id=investigation_id,
            opinion_id=opinion_id,
            anchor=anchor,
            target_block_type=target_block_type,
            bridge_block_type=bridge_block_type,
            consultation_type=consultation_type,
            base_note_id=base_note_id,
            note_state=note_state,
            note_relation=note_relation,
            cited_finding_refs=cited_findings,
            cited_observation_refs=cited_observations,
            base_opinion_version=base_opinion_version,
            base_opinion_sha256=base_opinion_sha256,
            base_publication_artifact_id=base_publication_artifact_id,
            base_publication_version=base_publication_version,
            base_publication_sha256=base_publication_sha256,
            anchor_before_sha256=anchor_before_sha256,
            content_blocks=content_blocks,
            task_id=task_id,
            idempotency_key=idempotency_key,
            status=NoteProposalStatus.PROPOSED,
            status_version=1,
            tool_invocation_id=tool_invocation_id,
            created_at=now,
            updated_at=now,
        )


__all__ = [
    "AdvisorDraftingOutcomeV1",
    "AdvisorDraftingPort",
    "AdvisorDraftingRequestV1",
    "NoteArticleElementFactV1",
    "NoteContentBlockContextV1",
    "NoteObservationFactV1",
    "NoteProposalDiffV1",
    "NoteProposalError",
    "NoteProposalErrorCode",
    "NoteProposalRequestResultV1",
    "OpinionNoteProposalService",
    "ResolvedArticleElementBlockV1",
    "ResolvedCanonicalCodeBlockV1",
    "ResolvedIocTableBlockV1",
    "ResolvedIocTableRowV1",
    "ResolvedNoteContentBlockV1",
    "ResolvedProtocolFigureBlockV1",
    "advisor_consult_arguments",
    "default_drafting_question",
    "default_review_question",
    "load_note_content_block_context",
    "note_article_element_facts",
    "resolve_note_content_blocks",
    "validate_note_content_blocks",
]
