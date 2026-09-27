"""Canonical, archive-backed EXTRACTION service (AW-011).

The EXTRACTION stage never looks a source up: it consumes the frozen
``ProductionReferenceCorpusV1`` of REFERENCES, resolves every eligible source by
its exact ``source_document_id``, reads the archived decoded blob, verifies its
SHA-256 against the corpus before any model call, applies the tier policy
(CORE -> FULL, SUPPORTING/TECHNICAL -> IOC_RULES), reuses durable source
checkpoints, asks ``ModelGateway`` for the source-local structured Q2 output and
aggregates only evidence-gated proposals into ``ProductionExtractionV1``.

The module is deliberately free of provider knowledge: the gateway chooses the
authorized adapter for the requested capability. The canonical prompt is a pure
function of the archived capture and the profile, so a checkpoint stays valid
across runs and Subjects.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import UUID

from pydantic import BaseModel

from cti_app.application.extraction import _html_encoding, parse_document
from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelExecution,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
)
from cti_app.application.persistence import ProductionUnitOfWorkFactory
from cti_app.application.production_artifact_store import (
    ProductionArtifactStore,
    ProductionReuseStorageUnavailableError,
)
from cti_app.application.production_artifact_verification import (
    _SEMANTIC_TYPE_BY_FACT_CATEGORY,
    ARTIFACT_VERIFIER_VERSION,
    _artifact_fields,
)
from cti_app.application.production_normalization import normalize_indicator_value
from cti_app.application.production_parsers import (
    Q2_EXTRACTION_CONTRACT_VERSION,
    Q2_MARKDOWN_PARSER_VERSION,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    Q2SourceOutput,
    SemanticType,
    TechnicalExtraction,
    parse_q2_proposals_markdown,
    project_q2_source_output,
    q2_source_output_from_json,
    q2_source_output_to_json,
    technical_extraction_from_json,
)
from cti_app.application.production_prompts import (
    CANONICAL_EXTRACTION_PROMPT_VERSION,
    CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE,
    CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
    ProductionPromptTemplates,
)
from cti_app.application.production_q2_batch import (
    ArchiveQ2BatchSource,
    Q2BatchResponse,
    archive_q2_batch_identity,
    attribute_q2_batch_response,
    make_archive_q2_batch,
    partition_archive_q2_batch_sources,
)
from cti_app.application.production_references import (
    has_usable_core_source,
    production_reference_corpus_from_json,
    production_reference_corpus_to_json,
)
from cti_app.application.production_source_evidence import (
    SOURCE_EVIDENCE_VERSION,
    SourceEvidenceDocument,
    _artifact_comparison_view,
    _text_comparison_view,
    source_evidence_document_from_html,
    verify_ioc_rules_output_against_source,
    verify_q2_output_against_source,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState, DetectedMimeType, SourceCollection
from cti_app.domain.discovery import SourceRole
from cti_app.domain.entities import SourceDocument
from cti_app.domain.model_runs import ModelRunStatus
from cti_app.domain.production import (
    DetectionRule,
    ExtractionProfile,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionInputSnapshot,
    ProductionRun,
    SourceExtraction,
    SourceExtractionStatus,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    PRODUCTION_EXTRACTION_SCHEMA_VERSION,
    ExtractionEventV1,
    ExtractionFactV1,
    ExtractionIndicatorStatus,
    ExtractionIndicatorV1,
    ExtractionReuseState,
    ExtractionRuleV1,
    ProductionExtractionOmissionReason,
    ProductionExtractionOmissionV1,
    ProductionExtractionV1,
    ProductionSourceExtractionV1,
    extraction_profile_for_tier,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.publication import ArtifactType

#: Canonical service contract version. It participates in the run-level input
#: hash but never in a per-source checkpoint identity.
PRODUCTION_EXTRACTION_SERVICE_VERSION = "production-extraction-service-v1"

#: Model and routing policies of the archive-backed path. The service asks for a
#: capability; the gateway decides which authorized adapter answers it.
EXTRACTION_MODEL_POLICY_VERSION = "archive-source-extraction-v1"
EXTRACTION_ROUTING_POLICY_VERSION = "bulk-extraction-routing-v1"

#: Deterministic archived-text transform. Chunking is a pure function of the
#: archived content, the transformers' versions, the maximum size and the
#: overlap, so it never depends on the provider that answers.
SOURCE_TEXT_CONTRACT_VERSION = "archived-source-text-v1"
SOURCE_TEXT_CHUNKER_VERSION = "archived-source-chunker-v1"
SOURCE_CHUNK_MAX_CHARS = 24_000
SOURCE_CHUNK_OVERLAP_CHARS = 400

#: Bounded model input. A capture larger than the chunk size is chunked
#: deterministically instead of being truncated.
MAX_ARCHIVED_SOURCE_BYTES = 25 * 1024 * 1024
MAX_CHUNKS_PER_SOURCE = 64
BATCH_SOURCE_MAX_CHARS = 8_000
BATCH_TOTAL_MAX_CHARS = 24_000
MAX_EVIDENCE_QUOTE_CHARS = 2_000

_SOURCE_EXTRACTION_PROMPT_TEMPLATE_ID = "production-extraction-archive-source"
_SOURCE_EXTRACTION_BATCH_PROMPT_TEMPLATE_ID = "production-extraction-archive-batch"


class ExtractionControlCode(StrEnum):
    """Control invariants of the canonical extraction boundary."""

    REFERENCE_CORPUS_MISSING = "extraction_reference_corpus_missing"
    REFERENCE_CORPUS_INVALID = "extraction_reference_corpus_invalid"
    REFERENCE_SUBJECT_MISMATCH = "extraction_reference_subject_mismatch"
    REFERENCE_SNAPSHOT_MISMATCH = "extraction_reference_snapshot_mismatch"
    SOURCE_DOCUMENT_MISSING = "extraction_source_document_missing"
    SOURCE_CONTENT_MISMATCH = "extraction_source_content_mismatch"


class ExtractionExecutionStatus(StrEnum):
    """Outcome of one canonical extraction execution."""

    SUCCEEDED = "succeeded"
    #: A provider submission may have been accepted and is not reconciled.
    NEEDS_REVIEW = "needs_review"
    #: A CORE source could not produce its full extraction.
    BLOCKED = "blocked"


class ProductionExtractionControlError(RuntimeError):
    """One control invariant failed; no fallback path may be attempted."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details: dict[str, Any] = dict(details or {})


class SourceExtractionFailure(RuntimeError):
    """One source-local failure. CORE blocks the stage; others are omitted."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details: dict[str, Any] = dict(details or {})


class ExtractionSubmissionAmbiguity(RuntimeError):
    """A provider submission may have been accepted and cannot be replayed."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.details: dict[str, Any] = dict(details or {})


class ExtractionArtifactStore(Protocol):
    """The payload storage the canonical extraction boundary needs."""

    async def read_json(self, blob_id: UUID) -> dict[str, Any]: ...

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...

    async def store_source_extraction_payloads(
        self, *, raw: str, canonical: dict[str, Any]
    ) -> tuple[UUID | None, UUID]: ...


@dataclass(frozen=True, slots=True)
class ProductionExtractionExecution:
    """The outcome of one canonical extraction execution."""

    status: ExtractionExecutionStatus
    extraction: ProductionExtractionV1 | None = None
    error_code: str | None = None
    error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is ExtractionExecutionStatus.SUCCEEDED


# --- Planning --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedExtractionSource:
    """One eligible corpus source, with the profile its tier imposes."""

    source_document_id: UUID
    source_collection_id: UUID | None
    canonical_url: str
    content_sha256: str
    tier: ProductionReferenceTier
    kind: ProductionReferenceKind
    role: SourceRole
    profile: ExtractionProfile
    title: str | None
    publisher: str | None
    collection_state: CollectionState
    position: int = 0


@dataclass(frozen=True, slots=True)
class ExtractionPlan:
    """The deterministic plan of one canonical extraction run."""

    subject_id: UUID
    production_input_hash: str
    references_corpus_hash: str
    profile_policy_version: str
    input_hash: str
    sources: tuple[PlannedExtractionSource, ...]
    omitted_sources: tuple[ProductionExtractionOmissionV1, ...]
    warnings: tuple[str, ...]

    @property
    def full_sources(self) -> tuple[PlannedExtractionSource, ...]:
        return tuple(source for source in self.sources if source.profile is ExtractionProfile.FULL)

    @property
    def ioc_rules_sources(self) -> tuple[PlannedExtractionSource, ...]:
        return tuple(
            source for source in self.sources if source.profile is ExtractionProfile.IOC_RULES
        )


def source_text_contract_version() -> str:
    """The versioned identity of the deterministic archived-text transform."""

    return (
        f"{SOURCE_TEXT_CONTRACT_VERSION}+{SOURCE_TEXT_CHUNKER_VERSION}"
        f"+max:{SOURCE_CHUNK_MAX_CHARS}+overlap:{SOURCE_CHUNK_OVERLAP_CHARS}"
    )


def references_corpus_hash(corpus: ProductionReferenceCorpusV1) -> str:
    """Return the content-addressed identity of one canonical corpus."""

    encoded = ProductionArtifactStore.canonical_json_bytes(
        production_reference_corpus_to_json(corpus)
    )
    return hashlib.sha256(encoded).hexdigest()


def build_extraction_plan(corpus: ProductionReferenceCorpusV1) -> ExtractionPlan:
    """Plan one extraction run exclusively from the canonical REFERENCES corpus.

    The tier of the corpus is the only authority: ``SourceRole`` never decides
    FULL vs IOC_RULES. The plan is canonicalized by tier, URL and document
    identity, so loading the same corpus in another source order yields the same
    plan and the same input hash.
    """

    if not isinstance(corpus, ProductionReferenceCorpusV1):
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_CORPUS_INVALID,
            "Extraction requires a canonical ProductionReferenceCorpusV1",
        )
    if not has_usable_core_source(corpus):
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_CORPUS_INVALID,
            "Extraction requires at least one extractable CORE reference",
        )

    planned: list[PlannedExtractionSource] = []
    omitted: list[ProductionExtractionOmissionV1] = []
    warnings: list[str] = list(corpus.warnings)
    for source in corpus.sources:
        if (
            not source.eligible_for_extraction
            or source.source_document_id is None
            or source.content_sha256 is None
        ):
            omitted.append(
                ProductionExtractionOmissionV1(
                    canonical_url=source.canonical_url,
                    tier=source.tier,
                    collection_state=source.collection_state,
                    reason=ProductionExtractionOmissionReason.REFERENCE_NOT_ELIGIBLE,
                )
            )
            warnings.append(f"reference_not_eligible:{source.tier.value}:{source.canonical_url}")
            continue
        planned.append(
            PlannedExtractionSource(
                source_document_id=source.source_document_id,
                source_collection_id=source.source_collection_id,
                canonical_url=source.canonical_url,
                content_sha256=source.content_sha256,
                tier=source.tier,
                kind=source.kind,
                role=source.role,
                profile=extraction_profile_for_tier(source.tier),
                title=source.title,
                publisher=source.publisher,
                collection_state=source.collection_state,
            )
        )

    ordered = tuple(
        sorted(
            planned,
            key=lambda source: (
                _tier_rank(source.tier),
                source.canonical_url,
                str(source.source_document_id),
            ),
        )
    )
    ordered = tuple(replace(source, position=index) for index, source in enumerate(ordered))
    corpus_hash = references_corpus_hash(corpus)
    return ExtractionPlan(
        subject_id=corpus.subject_id,
        production_input_hash=corpus.production_input_hash,
        references_corpus_hash=corpus_hash,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        input_hash=extraction_input_hash(
            references_corpus_hash=corpus_hash,
            sources=ordered,
        ),
        sources=ordered,
        omitted_sources=tuple(omitted),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def extraction_input_hash(
    *,
    references_corpus_hash: str,
    sources: Sequence[PlannedExtractionSource],
) -> str:
    """Return the deterministic functional identity of one extraction run.

    Execution identities (run id, job id, model run id, provider, conversation
    id) and timestamps are deliberately absent: two functionally identical runs
    get the same hash, and a corpus loaded in another order gets the same plan.
    """

    payload = {
        "service_version": PRODUCTION_EXTRACTION_SERVICE_VERSION,
        "references_corpus_hash": references_corpus_hash,
        "profile_policy_version": EXTRACTION_PROFILE_POLICY_VERSION,
        "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
        "source_text_contract_version": source_text_contract_version(),
        "full_prompt_version": CANONICAL_EXTRACTION_PROMPT_VERSION,
        "ioc_rules_batch_prompt_version": CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
        "ioc_rules_prompt_version": CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE[
            ExtractionProfile.IOC_RULES
        ],
        "response_parser_version": Q2_MARKDOWN_PARSER_VERSION,
        "verifier_version": ARTIFACT_VERIFIER_VERSION,
        "source_evidence_version": SOURCE_EVIDENCE_VERSION,
        "model_policy_version": EXTRACTION_MODEL_POLICY_VERSION,
        "routing_policy_version": EXTRACTION_ROUTING_POLICY_VERSION,
        "sources": [
            {
                "source_document_id": str(source.source_document_id),
                "canonical_url": source.canonical_url,
                "content_sha256": source.content_sha256,
                "tier": source.tier.value,
                "profile": source.profile.value,
            }
            for source in sources
        ],
    }
    return _canonical_hash(payload)


def _tier_rank(tier: ProductionReferenceTier) -> int:
    return {
        ProductionReferenceTier.CORE: 0,
        ProductionReferenceTier.SUPPORTING: 1,
        ProductionReferenceTier.TECHNICAL: 2,
    }[tier]


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def source_checkpoint_identity(
    *,
    content_sha256: str,
    profile: ExtractionProfile,
    prompt_version: str,
) -> dict[str, str]:
    """The durable functional identity of one subject-independent checkpoint."""

    return {
        "source_content_sha256": content_sha256,
        "profile": profile.value,
        "contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
        "prompt_version": prompt_version,
        "parser_version": Q2_MARKDOWN_PARSER_VERSION,
        "verifier_version": ARTIFACT_VERIFIER_VERSION,
        "source_text_contract_version": source_text_contract_version(),
        "model_policy_version": EXTRACTION_MODEL_POLICY_VERSION,
        "routing_policy_version": EXTRACTION_ROUTING_POLICY_VERSION,
    }


def source_prompt_version(profile: ExtractionProfile) -> str:
    """The canonical single-source prompt version of one profile."""

    try:
        return CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE[profile]
    except KeyError as exc:
        raise ValueError(f"Unsupported extraction profile: {profile}") from exc


# --- Reference corpus loading ---------------------------------------------


async def load_reference_corpus(
    *,
    uow: Any,
    run: ProductionRun,
    snapshot: ProductionInputSnapshot | None,
    artifact_store: ExtractionArtifactStore,
) -> ProductionReferenceCorpusV1:
    """Load and validate the canonical corpus of a run's REFERENCES artifact.

    Only the canonical blob is read. An invalid, unverified or inconsistent
    corpus is an explicit control error: the legacy REFERENCES RAW is never a
    fallback input of Extraction.
    """

    artifact = await _current_references_artifact(uow, run)
    if artifact is None:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_CORPUS_MISSING,
            "The REFERENCES artifact of this run is missing",
            details={"run_id": str(run.id)},
        )
    if artifact.status is not ProductionArtifactStatus.VERIFIED:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_CORPUS_INVALID,
            "The REFERENCES artifact is not verified",
            details={"artifact_id": str(artifact.id), "status": artifact.status.value},
        )
    if artifact.canonical_blob_id is None:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_CORPUS_MISSING,
            "The REFERENCES artifact carries no canonical corpus",
            details={"artifact_id": str(artifact.id)},
        )
    try:
        payload = await artifact_store.read_json(artifact.canonical_blob_id)
    except ProductionReuseStorageUnavailableError:
        raise
    except Exception as exc:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_CORPUS_INVALID,
            "The canonical REFERENCES corpus is not readable",
            details={"artifact_id": str(artifact.id)},
        ) from exc
    try:
        corpus = production_reference_corpus_from_json(payload)
    except ValueError as exc:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_CORPUS_INVALID,
            "The canonical REFERENCES corpus is invalid",
            details={"artifact_id": str(artifact.id), "reason": str(exc)},
        ) from exc

    if corpus.subject_id != run.subject_id:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_SUBJECT_MISMATCH,
            "The canonical corpus belongs to another Subject",
            details={
                "corpus_subject_id": str(corpus.subject_id),
                "run_subject_id": str(run.subject_id),
            },
        )

    resolved_snapshot = snapshot
    if resolved_snapshot is None:
        snapshots = getattr(uow, "production_input_snapshots", None)
        getter = getattr(snapshots, "get_by_run", None)
        if callable(getter):
            resolved_snapshot = await getter(run.id)
    if resolved_snapshot is None:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_SNAPSHOT_MISMATCH,
            "The production input snapshot of this run is missing",
            details={"run_id": str(run.id)},
        )
    if corpus.production_input_hash != resolved_snapshot.input_hash:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_SNAPSHOT_MISMATCH,
            "The canonical corpus does not match the production input snapshot",
            details={
                "corpus_input_hash": corpus.production_input_hash,
                "snapshot_input_hash": resolved_snapshot.input_hash,
            },
        )
    return corpus


async def _current_references_artifact(uow: Any, run: ProductionRun) -> ProductionArtifact | None:
    artifacts = getattr(uow, "production_artifacts", None)
    getter = getattr(artifacts, "get_current", None)
    if not callable(getter):
        return None
    return cast(
        "ProductionArtifact | None",
        await getter(run.id, ProductionArtifactStage.REFERENCES.value),
    )


# --- Archive resolution ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class SourceAccessPolicy:
    """The diffusion policy governing one exact archived document."""

    tlp: TLP
    sensitivity: str
    external_llm_allowed: bool
    do_not_submit: bool

    @property
    def submission_allowed(self) -> bool:
        return self.external_llm_allowed and not self.do_not_submit


@dataclass(frozen=True, slots=True)
class SourceArchive:
    """One planned source resolved to its exact archived bytes."""

    planned: PlannedExtractionSource
    document: SourceDocument
    collection: SourceCollection | None
    policy: SourceAccessPolicy
    content: bytes


def resolve_source_policy(
    document: SourceDocument, collection: SourceCollection | None
) -> SourceAccessPolicy:
    """Combine the document and collection policies without ever loosening one."""

    tlp = document.tlp
    external_llm_allowed = document.external_llm_allowed
    do_not_submit = document.do_not_submit
    sensitivity = "internal"
    if collection is not None:
        ranks = list(TLP)
        tlp = max((document.tlp, collection.source_tlp), key=ranks.index)
        external_llm_allowed = external_llm_allowed and collection.external_llm_allowed
        do_not_submit = do_not_submit or collection.do_not_submit
        sensitivity = collection.sensitivity or sensitivity
    return SourceAccessPolicy(
        tlp=tlp,
        sensitivity=sensitivity,
        external_llm_allowed=external_llm_allowed,
        do_not_submit=do_not_submit,
    )


async def load_source_archive(
    *,
    uow: Any,
    planned: PlannedExtractionSource,
    corpus: ProductionReferenceCorpusV1,
    artifact_store: ExtractionArtifactStore,
) -> SourceArchive:
    """Resolve one eligible source strictly by its exact document identity.

    The document is read by ``source_document_id``; no URL lookup, no "latest
    document of this URL" and no network acquisition exists on this path. The
    archived decoded blob is verified against ``corpus.content_sha256`` before
    any model call.
    """

    documents = getattr(uow, "source_documents", None)
    getter = getattr(documents, "get", None)
    document = await getter(planned.source_document_id) if callable(getter) else None
    if document is None:
        raise ProductionExtractionControlError(
            ExtractionControlCode.SOURCE_DOCUMENT_MISSING,
            "The exact archived document of this corpus source is missing",
            details={
                "source_document_id": str(planned.source_document_id),
                "canonical_url": planned.canonical_url,
            },
        )
    if document.subject_id != corpus.subject_id:
        raise ProductionExtractionControlError(
            ExtractionControlCode.SOURCE_DOCUMENT_MISSING,
            "The exact archived document belongs to another Subject",
            details={
                "source_document_id": str(document.id),
                "document_subject_id": str(document.subject_id),
            },
        )

    collection: SourceCollection | None = None
    collections = getattr(uow, "source_collections", None)
    collection_getter = getattr(collections, "get", None)
    if document.source_collection_id is not None and callable(collection_getter):
        collection = await collection_getter(document.source_collection_id)
    if collection is not None:
        if (
            collection.subject_id != corpus.subject_id
            or collection.canonical_url != planned.canonical_url
            or collection.source_document_id not in (None, document.id)
        ):
            raise ProductionExtractionControlError(
                ExtractionControlCode.SOURCE_DOCUMENT_MISSING,
                "The archived document does not belong to the corpus source collection",
                details={
                    "source_document_id": str(document.id),
                    "collection_id": str(collection.id),
                },
            )

    decoded_blob_id = document.decoded_blob_id
    if decoded_blob_id is None:
        raise ProductionExtractionControlError(
            ExtractionControlCode.SOURCE_DOCUMENT_MISSING,
            "The exact archived document carries no decoded blob",
            details={"source_document_id": str(document.id)},
        )
    try:
        content = await artifact_store.read_bytes(
            decoded_blob_id, max_bytes=MAX_ARCHIVED_SOURCE_BYTES
        )
    except ProductionReuseStorageUnavailableError:
        raise
    except Exception as exc:
        raise ProductionExtractionControlError(
            ExtractionControlCode.SOURCE_CONTENT_MISMATCH,
            "The archived decoded blob is not readable",
            details={"source_document_id": str(document.id)},
        ) from exc

    actual_sha256 = hashlib.sha256(content).hexdigest()
    if actual_sha256 != planned.content_sha256:
        raise ProductionExtractionControlError(
            ExtractionControlCode.SOURCE_CONTENT_MISMATCH,
            "The archived document does not match the content hash of the corpus",
            details={
                "source_document_id": str(document.id),
                "expected_sha256": planned.content_sha256,
                "actual_sha256": actual_sha256,
            },
        )
    return SourceArchive(
        planned=planned,
        document=document,
        collection=collection,
        policy=resolve_source_policy(document, collection),
        content=content,
    )


# --- Deterministic archived text ------------------------------------------


def build_source_evidence_document(
    content: bytes, *, mime_type: str | None
) -> SourceEvidenceDocument:
    """Build the deterministic local representation of one archived capture."""

    detected = _detected_mime_type(mime_type)
    try:
        parsed = parse_document(content, detected)
    except Exception as exc:
        raise SourceExtractionFailure(
            "extraction_source_text_unreadable",
            "The archived source text is not readable",
        ) from exc
    if detected is DetectedMimeType.HTML:
        return source_evidence_document_from_html(
            parsed.text,
            content.decode(_html_encoding(content), errors="replace"),
        )
    return SourceEvidenceDocument(parsed_text=parsed.text)


def _detected_mime_type(mime_type: str | None) -> DetectedMimeType:
    if mime_type:
        try:
            return DetectedMimeType(mime_type)
        except ValueError:
            pass
    return DetectedMimeType.TEXT


def archived_source_chunks(text: str) -> tuple[str, ...]:
    """Chunk one archived text deterministically, never truncating it."""

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) <= SOURCE_CHUNK_MAX_CHARS:
        return (normalized,)
    max_chars = SOURCE_CHUNK_MAX_CHARS
    overlap = min(SOURCE_CHUNK_OVERLAP_CHARS, max(0, max_chars // 4))
    step = max(1, max_chars - overlap)
    chunks: list[str] = []
    current = ""
    for paragraph in normalized.split("\n\n"):
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= max_chars:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > max_chars:
            chunks.append(paragraph[:max_chars])
            paragraph = paragraph[step:]
        current = paragraph
    if current:
        chunks.append(current)
    return tuple(chunks)


def merge_q2_source_outputs(outputs: Sequence[Q2SourceOutput]) -> Q2SourceOutput:
    """Merge chunk-local outputs into one deterministic source-local result."""

    facts: list[Any] = []
    events: list[Any] = []
    artifacts: list[Any] = []
    rules: list[Any] = []
    uncertainties: list[str] = []
    seen_facts: set[tuple[str, str]] = set()
    seen_events: set[tuple[Any, Any, str]] = set()
    seen_artifacts: set[tuple[str, str]] = set()
    seen_rules: set[tuple[str, str]] = set()
    seen_uncertainties: set[str] = set()
    for output in outputs:
        for fact in output.facts:
            fact_key = (fact.category, fact.value.strip().casefold())
            if fact_key in seen_facts:
                continue
            seen_facts.add(fact_key)
            facts.append(fact)
        for event in output.events:
            event_key = (event.event_date, event.date_text, event.text.strip().casefold())
            if event_key in seen_events:
                continue
            seen_events.add(event_key)
            events.append(event)
        for artifact in output.artifacts:
            artifact_key = (artifact.artifact_type, artifact.value.strip().casefold())
            if artifact_key in seen_artifacts:
                continue
            seen_artifacts.add(artifact_key)
            artifacts.append(artifact)
        for rule in output.rules:
            body = rule.body.replace("\r\n", "\n").replace("\r", "\n")
            rule_key = (str(rule.rule_type), body)
            if rule_key in seen_rules:
                continue
            seen_rules.add(rule_key)
            rules.append(rule)
        for uncertainty in output.uncertainties:
            if uncertainty in seen_uncertainties:
                continue
            seen_uncertainties.add(uncertainty)
            uncertainties.append(uncertainty)
    return Q2SourceOutput(
        facts=facts,
        events=events,
        artifacts=artifacts,
        rules=rules,
        uncertainties=uncertainties,
    )


# --- Local evidence --------------------------------------------------------


def local_evidence_quote(
    document: SourceEvidenceDocument,
    needle: str,
    *,
    date_marker: str | None = None,
) -> str | None:
    """Return the smallest deterministic local area supporting one proposal."""

    value = needle.strip()
    if not value:
        return None
    for area in _evidence_areas(document):
        quote = _quote_from_area(area, value, date_marker=date_marker)
        if quote is not None:
            return quote
    return None


def _evidence_areas(document: SourceEvidenceDocument) -> tuple[str, ...]:
    areas = [span.text for span in document.spans if span.text]
    if document.decoded_source_view:
        areas.append(document.decoded_source_view)
    if not areas and document.parsed_text:
        areas.append(document.parsed_text)
    return tuple(areas)


def _quote_from_area(area: str, value: str, *, date_marker: str | None) -> str | None:
    for view, needle in (
        (area, value),
        (_artifact_comparison_view(area), _artifact_comparison_view(value)),
    ):
        for line in view.split("\n"):
            compact_line = " ".join(line.split())
            if not compact_line or " ".join(needle.split()) not in compact_line:
                continue
            if date_marker is not None and date_marker not in compact_line:
                continue
            return _bounded_quote(line.strip())
    compact_value = " ".join(value.split())
    compact_area = _text_comparison_view(area)
    if not compact_value or compact_value not in compact_area:
        return None
    if date_marker is not None and date_marker not in compact_area:
        return None
    start = compact_area.find(compact_value)
    window_start = max(0, start - 200)
    window_end = min(len(compact_area), start + len(compact_value) + 200)
    return _bounded_quote(compact_area[window_start:window_end])


def _bounded_quote(quote: str) -> str:
    return quote[:MAX_EVIDENCE_QUOTE_CHARS]


# --- Canonical item building ----------------------------------------------


def build_canonical_source_extraction(
    *,
    planned: PlannedExtractionSource,
    output: Q2SourceOutput,
    document: SourceEvidenceDocument,
    checkpoint_id: UUID | None,
    reuse_state: ExtractionReuseState,
) -> ProductionSourceExtractionV1:
    """Attach deterministic identities and local evidence to gated proposals."""

    document_ids = (planned.source_document_id,)
    return ProductionSourceExtractionV1(
        source_document_id=planned.source_document_id,
        canonical_url=planned.canonical_url,
        content_sha256=planned.content_sha256,
        tier=planned.tier,
        kind=planned.kind,
        role=planned.role,
        profile=planned.profile,
        checkpoint_id=checkpoint_id,
        reuse_state=reuse_state,
        facts=_canonical_facts(output, document=document, document_ids=document_ids),
        events=_canonical_events(output, document=document, document_ids=document_ids),
        indicators=_canonical_indicators(output, document=document, document_ids=document_ids),
        rules=_canonical_rules(output, document=document, document_ids=document_ids),
        uncertainties=tuple(output.uncertainties),
    )


def _canonical_facts(
    output: Q2SourceOutput,
    *,
    document: SourceEvidenceDocument,
    document_ids: tuple[UUID, ...],
) -> tuple[ExtractionFactV1, ...]:
    facts: list[ExtractionFactV1] = []
    seen: set[tuple[str, str]] = set()
    for fact in output.facts:
        key = (fact.category, fact.value.strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        facts.append(
            ExtractionFactV1(
                category=fact.category,
                value=fact.value,
                attack_id=fact.attack_id,
                context=fact.context,
                evidence_quote=local_evidence_quote(document, fact.value)
                or _bounded_quote(fact.value),
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=document_ids,
            )
        )
    return tuple(facts)


def _canonical_events(
    output: Q2SourceOutput,
    *,
    document: SourceEvidenceDocument,
    document_ids: tuple[UUID, ...],
) -> tuple[ExtractionEventV1, ...]:
    events: list[ExtractionEventV1] = []
    seen: set[tuple[Any, Any, str]] = set()
    for event in output.events:
        key = (event.event_date, event.date_text, event.text.strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        date_marker = event.event_date.isoformat() if event.event_date is not None else None
        events.append(
            ExtractionEventV1(
                event_date=event.event_date,
                date_text=event.date_text,
                text=event.text,
                context=event.context,
                evidence_quote=local_evidence_quote(document, event.text, date_marker=date_marker)
                or _bounded_quote(event.text),
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=document_ids,
            )
        )
    return tuple(events)


def _canonical_indicators(
    output: Q2SourceOutput,
    *,
    document: SourceEvidenceDocument,
    document_ids: tuple[UUID, ...],
) -> tuple[ExtractionIndicatorV1, ...]:
    indicators: list[ExtractionIndicatorV1] = []
    seen: set[tuple[str, str]] = set()
    for artifact in output.artifacts:
        try:
            artifact_type = ArtifactType(artifact.artifact_type)
        except ValueError:
            continue
        if artifact.indicator_status not in {"confirmed_ioc", "contextual"}:
            continue
        status = ExtractionIndicatorStatus(artifact.indicator_status)
        key = (artifact_type.value, artifact.value.strip().casefold())
        if key in seen:
            continue
        seen.add(key)
        indicators.append(
            ExtractionIndicatorV1(
                value=artifact.value,
                artifact_type=artifact_type,
                indicator_status=status,
                context=artifact.context,
                evidence_quote=local_evidence_quote(document, artifact.value)
                or _bounded_quote(artifact.value),
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=document_ids,
            )
        )
    return tuple(indicators)


def _canonical_rules(
    output: Q2SourceOutput,
    *,
    document: SourceEvidenceDocument,
    document_ids: tuple[UUID, ...],
) -> tuple[ExtractionRuleV1, ...]:
    rules: list[ExtractionRuleV1] = []
    seen: set[tuple[str, str]] = set()
    for rule in output.rules:
        body = rule.body.replace("\r\n", "\n").replace("\r", "\n")
        key = (str(rule.rule_type), body)
        if key in seen:
            continue
        seen.add(key)
        rules.append(
            ExtractionRuleV1(
                rule_type=rule.rule_type,
                name=rule.name,
                body=body,
                sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
                context=rule.context,
                evidence_quote=local_evidence_quote(document, body) or _bounded_quote(body),
                evidence_basis=ProductionEvidenceBasis.SOURCE_VERIFIED,
                source_document_ids=document_ids,
            )
        )
    return tuple(rules)


def gate_source_output(
    output: Q2SourceOutput,
    document: SourceEvidenceDocument,
    *,
    profile: ExtractionProfile,
) -> tuple[Q2SourceOutput, tuple[str, ...]]:
    """Run the deterministic local evidence gate of one profile."""

    if profile is ExtractionProfile.FULL:
        result = verify_q2_output_against_source(output, document)
    else:
        result = verify_ioc_rules_output_against_source(output, document)
    warnings = list(result.warnings)
    warnings.extend(
        f"extraction_proposal_rejected:{rejection.reason_code}" for rejection in result.rejections
    )
    return result.filtered_output, tuple(dict.fromkeys(warnings))


def build_production_extraction(
    *,
    plan: ExtractionPlan,
    sources: Sequence[ProductionSourceExtractionV1],
    warnings: Sequence[str],
) -> ProductionExtractionV1:
    """Aggregate gated source results into the canonical run-level contract."""

    return ProductionExtractionV1(
        schema_version=PRODUCTION_EXTRACTION_SCHEMA_VERSION,
        subject_id=plan.subject_id,
        production_input_hash=plan.production_input_hash,
        references_corpus_hash=plan.references_corpus_hash,
        profile_policy_version=plan.profile_policy_version,
        sources=tuple(sources),
        omitted_sources=plan.omitted_sources,
        warnings=tuple(dict.fromkeys(warnings)),
    )


def production_extraction_metadata(extraction: ProductionExtractionV1) -> dict[str, Any]:
    """The bounded counter projection an EXTRACTION artifact may store."""

    full_sources = sum(source.profile is ExtractionProfile.FULL for source in extraction.sources)
    return {
        "schema_version": extraction.schema_version,
        "service_version": PRODUCTION_EXTRACTION_SERVICE_VERSION,
        "contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
        "profile_policy_version": extraction.profile_policy_version,
        "source_count": len(extraction.sources),
        "full_source_count": full_sources,
        "ioc_rules_source_count": len(extraction.sources) - full_sources,
        "reused_source_count": sum(
            source.reuse_state is ExtractionReuseState.REUSED for source in extraction.sources
        ),
        "fresh_source_count": sum(
            source.reuse_state is ExtractionReuseState.FRESH for source in extraction.sources
        ),
        "duplicate_source_count": sum(
            source.reuse_state is ExtractionReuseState.CONTENT_DUPLICATE
            for source in extraction.sources
        ),
        "omitted_source_count": len(extraction.omitted_sources),
        "fact_count": sum(len(source.facts) for source in extraction.sources),
        "event_count": sum(len(source.events) for source in extraction.sources),
        "indicator_count": sum(len(source.indicators) for source in extraction.sources),
        "rule_count": sum(len(source.rules) for source in extraction.sources),
        "warning_count": len(extraction.warnings),
    }


# --- Execution --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class _ResolvedCheckpoint:
    output: Q2SourceOutput
    checkpoint_id: UUID | None
    prompt_version: str


@dataclass(frozen=True, slots=True)
class _SourceOutcome:
    planned: PlannedExtractionSource
    extraction: ProductionSourceExtractionV1
    #: The ungated source-local output whose checkpoint identity is reusable.
    checkpoint_output: Q2SourceOutput
    prompt_version: str


@dataclass(frozen=True, slots=True)
class _SingleUnit:
    source: PlannedExtractionSource


@dataclass(frozen=True, slots=True)
class _BatchUnit:
    sources: tuple[PlannedExtractionSource, ...]


@dataclass(frozen=True, slots=True)
class _UnitResult:
    outcomes: tuple[_SourceOutcome, ...] = ()
    failures: tuple[tuple[PlannedExtractionSource, SourceExtractionFailure], ...] = ()
    fallbacks: tuple[PlannedExtractionSource, ...] = ()
    warnings: tuple[str, ...] = ()
    ambiguity: ExtractionSubmissionAmbiguity | None = None


class ProductionExtractionService:
    """The canonical, archive-backed EXTRACTION service (AW-011)."""

    def __init__(
        self,
        *,
        uow_factory: ProductionUnitOfWorkFactory,
        model_gateway: ModelGateway,
        artifact_store: ExtractionArtifactStore,
        ioc_rules_batching: bool = True,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_gateway = model_gateway
        self._artifact_store = artifact_store
        self._ioc_rules_batching = ioc_rules_batching

    async def execute(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot | None = None,
    ) -> ProductionExtractionExecution:
        """Run the canonical extraction of one production run."""

        try:
            async with self._uow_factory() as uow:
                corpus = await load_reference_corpus(
                    uow=uow,
                    run=run,
                    snapshot=snapshot,
                    artifact_store=self._artifact_store,
                )
            plan = build_extraction_plan(corpus)
            archives = await self._load_archives(corpus=corpus, plan=plan)
        except ProductionExtractionControlError as control:
            # A control invariant failed: the legacy REFERENCES RAW is never a
            # fallback input and no model call is attempted.
            return ProductionExtractionExecution(
                status=ExtractionExecutionStatus.BLOCKED,
                error_code=control.code,
                error=str(control),
                details=control.details,
            )

        evidence: dict[UUID, SourceEvidenceDocument] = {}
        source_failures: dict[UUID, SourceExtractionFailure] = {}
        for planned in plan.sources:
            archive = archives[planned.source_document_id]
            try:
                evidence[planned.source_document_id] = build_source_evidence_document(
                    archive.content, mime_type=archive.document.detected_mime_type
                )
            except SourceExtractionFailure as failure:
                source_failures[planned.source_document_id] = failure
        blocked = self._core_blocked_by_failures(plan, source_failures)
        if blocked is not None:
            return blocked

        outcomes: dict[UUID, _SourceOutcome] = {}
        warnings_by_source: dict[UUID, list[str]] = {}
        stage_warnings: list[str] = []
        run_cache: dict[tuple[str, str], _ResolvedCheckpoint] = {}
        misses: list[PlannedExtractionSource] = []
        miss_representatives: dict[tuple[str, str], PlannedExtractionSource] = {}
        duplicate_of: dict[UUID, tuple[str, str]] = {}
        for planned in plan.sources:
            if planned.source_document_id in source_failures:
                continue
            cache_key = (planned.content_sha256, planned.profile.value)
            cached = run_cache.get(cache_key)
            if cached is not None:
                outcomes[planned.source_document_id] = self._materialize(
                    planned,
                    document=evidence[planned.source_document_id],
                    output=cached.output,
                    prompt_version=cached.prompt_version,
                    checkpoint_id=cached.checkpoint_id,
                    reuse_state=ExtractionReuseState.CONTENT_DUPLICATE,
                    warnings=warnings_by_source.setdefault(planned.source_document_id, []),
                )
                continue
            resolved = await self._resolve_checkpoint(planned)
            if resolved is None:
                representative = miss_representatives.get(cache_key)
                if representative is None:
                    miss_representatives[cache_key] = planned
                    misses.append(planned)
                else:
                    # Identical bytes under the same profile: one model
                    # computation answers every source, which still keeps its
                    # own canonical entry.
                    duplicate_of[planned.source_document_id] = cache_key
                continue
            run_cache[cache_key] = resolved
            outcomes[planned.source_document_id] = self._materialize(
                planned,
                document=evidence[planned.source_document_id],
                output=resolved.output,
                prompt_version=resolved.prompt_version,
                checkpoint_id=resolved.checkpoint_id,
                reuse_state=ExtractionReuseState.REUSED,
                warnings=warnings_by_source.setdefault(planned.source_document_id, []),
            )

        for unit in self._plan_units(misses, evidence):
            result = await self._execute_unit(unit, archives=archives, evidence=evidence)
            if result.ambiguity is not None:
                return ProductionExtractionExecution(
                    status=ExtractionExecutionStatus.NEEDS_REVIEW,
                    error_code=result.ambiguity.code,
                    error=str(result.ambiguity),
                    details=result.ambiguity.details,
                )
            stage_warnings.extend(result.warnings)
            for planned, source_failure in result.failures:
                source_failures[planned.source_document_id] = source_failure
            for outcome in result.outcomes:
                outcomes[outcome.planned.source_document_id] = outcome
                run_cache[(outcome.planned.content_sha256, outcome.planned.profile.value)] = (
                    _ResolvedCheckpoint(
                        output=outcome.checkpoint_output,
                        checkpoint_id=outcome.extraction.checkpoint_id,
                        prompt_version=outcome.prompt_version,
                    )
                )
            for fallback in result.fallbacks:
                fallback_result = await self._execute_single(
                    _SingleUnit(source=fallback), archives=archives, evidence=evidence
                )
                if fallback_result.ambiguity is not None:
                    return ProductionExtractionExecution(
                        status=ExtractionExecutionStatus.NEEDS_REVIEW,
                        error_code=fallback_result.ambiguity.code,
                        error=str(fallback_result.ambiguity),
                        details=fallback_result.ambiguity.details,
                    )
                stage_warnings.extend(fallback_result.warnings)
                for planned, source_failure in fallback_result.failures:
                    source_failures[planned.source_document_id] = source_failure
                for outcome in fallback_result.outcomes:
                    outcomes[outcome.planned.source_document_id] = outcome

        for planned in plan.sources:
            duplicate_key = duplicate_of.get(planned.source_document_id)
            if duplicate_key is None:
                continue
            cached = run_cache.get(duplicate_key)
            if cached is not None:
                if planned.source_document_id not in outcomes:
                    outcomes[planned.source_document_id] = self._materialize(
                        planned,
                        document=evidence[planned.source_document_id],
                        output=cached.output,
                        prompt_version=cached.prompt_version,
                        checkpoint_id=cached.checkpoint_id,
                        reuse_state=ExtractionReuseState.CONTENT_DUPLICATE,
                        warnings=warnings_by_source.setdefault(planned.source_document_id, []),
                    )
                continue
            representative = miss_representatives.get(duplicate_key)
            if representative is not None:
                representative_failure = source_failures.get(representative.source_document_id)
                if representative_failure is not None:
                    source_failures[planned.source_document_id] = representative_failure

        blocked = self._core_blocked_by_failures(plan, source_failures)
        if blocked is not None:
            return blocked
        missing_core = tuple(
            source for source in plan.full_sources if source.source_document_id not in outcomes
        )
        if missing_core:
            return ProductionExtractionExecution(
                status=ExtractionExecutionStatus.BLOCKED,
                error_code="extraction_core_source_failed",
                error="A CORE source has no verified FULL extraction",
                details={"sources": [str(source.source_document_id) for source in missing_core]},
            )

        warnings: list[str] = list(plan.warnings) + stage_warnings
        for planned in plan.sources:
            skipped = source_failures.get(planned.source_document_id)
            if skipped is not None and planned.tier is not ProductionReferenceTier.CORE:
                warnings.append(f"extraction_source_skipped:{planned.canonical_url}:{skipped.code}")
            warnings.extend(warnings_by_source.get(planned.source_document_id, ()))
        extraction = build_production_extraction(
            plan=plan,
            sources=tuple(
                outcomes[source.source_document_id].extraction
                for source in plan.sources
                if source.source_document_id in outcomes
            ),
            warnings=warnings,
        )
        return ProductionExtractionExecution(
            status=ExtractionExecutionStatus.SUCCEEDED,
            extraction=extraction,
            details=production_extraction_metadata(extraction),
        )

    # -- phases -------------------------------------------------------------

    async def _load_archives(
        self, *, corpus: ProductionReferenceCorpusV1, plan: ExtractionPlan
    ) -> dict[UUID, SourceArchive]:
        archives: dict[UUID, SourceArchive] = {}
        async with self._uow_factory() as uow:
            for planned in plan.sources:
                archives[planned.source_document_id] = await load_source_archive(
                    uow=uow,
                    planned=planned,
                    corpus=corpus,
                    artifact_store=self._artifact_store,
                )
        return archives

    def _core_blocked_by_failures(
        self,
        plan: ExtractionPlan,
        failures: Mapping[UUID, SourceExtractionFailure],
    ) -> ProductionExtractionExecution | None:
        blocked_sources = [
            source for source in plan.full_sources if source.source_document_id in failures
        ]
        if not blocked_sources:
            return None
        first = failures[blocked_sources[0].source_document_id]
        return ProductionExtractionExecution(
            status=ExtractionExecutionStatus.BLOCKED,
            error_code="extraction_core_source_failed",
            error=str(first),
            details={
                "source_failure_code": first.code,
                "sources": [str(source.source_document_id) for source in blocked_sources],
            },
        )

    def _plan_units(
        self,
        misses: Sequence[PlannedExtractionSource],
        evidence: Mapping[UUID, SourceEvidenceDocument],
    ) -> tuple[_SingleUnit | _BatchUnit, ...]:
        units: list[tuple[int, _SingleUnit | _BatchUnit]] = []
        batched: set[UUID] = set()
        if self._ioc_rules_batching:
            candidates = [
                source
                for source in misses
                if source.profile is ExtractionProfile.IOC_RULES
                and len(evidence[source.source_document_id].parsed_text) <= BATCH_SOURCE_MAX_CHARS
            ]
            entries = tuple(
                ArchiveQ2BatchSource(
                    source_document_id=source.source_document_id,
                    content_sha256=source.content_sha256,
                )
                for source in candidates
            )
            lengths = {
                source.source_document_id: len(evidence[source.source_document_id].parsed_text)
                for source in candidates
            }
            groups = partition_archive_q2_batch_sources(
                entries,
                text_lengths=lengths,
                max_total_chars=BATCH_TOTAL_MAX_CHARS,
            )
            by_id = {source.source_document_id: source for source in candidates}
            for group in groups:
                if len(group) < 2:
                    continue
                group_sources = tuple(by_id[entry.source_document_id] for entry in group)
                units.append(
                    (
                        min(source.position for source in group_sources),
                        _BatchUnit(sources=group_sources),
                    )
                )
                batched.update(entry.source_document_id for entry in group)
        for source in misses:
            if source.source_document_id in batched:
                continue
            units.append((source.position, _SingleUnit(source=source)))
        return tuple(unit for _, unit in sorted(units, key=lambda item: item[0]))

    # -- checkpoints --------------------------------------------------------

    async def _resolve_checkpoint(
        self, planned: PlannedExtractionSource
    ) -> _ResolvedCheckpoint | None:
        prompt_version = source_prompt_version(planned.profile)
        resolved = await self._read_checkpoint(
            planned, profile=planned.profile, prompt_version=prompt_version
        )
        if resolved is not None:
            return resolved
        if self._ioc_rules_batching and planned.profile is ExtractionProfile.IOC_RULES:
            resolved = await self._read_checkpoint(
                planned,
                profile=ExtractionProfile.IOC_RULES,
                prompt_version=CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
            )
            if resolved is not None:
                return resolved
        if planned.profile is ExtractionProfile.IOC_RULES:
            full = await self._read_checkpoint(
                planned,
                profile=ExtractionProfile.FULL,
                prompt_version=source_prompt_version(ExtractionProfile.FULL),
            )
            if full is not None:
                return _ResolvedCheckpoint(
                    output=project_q2_source_output(full.output, ExtractionProfile.IOC_RULES),
                    checkpoint_id=full.checkpoint_id,
                    prompt_version=full.prompt_version,
                )
        return None

    async def _read_checkpoint(
        self,
        planned: PlannedExtractionSource,
        *,
        profile: ExtractionProfile,
        prompt_version: str,
    ) -> _ResolvedCheckpoint | None:
        identity = source_checkpoint_identity(
            content_sha256=planned.content_sha256,
            profile=profile,
            prompt_version=prompt_version,
        )
        try:
            async with self._uow_factory() as uow:
                repository = getattr(uow, "source_extractions", None)
                getter = getattr(repository, "get_by_identity", None)
                if not callable(getter):
                    return None
                row = await getter(**identity)
                if (
                    row is None
                    or getattr(row, "status", None) is not SourceExtractionStatus.VERIFIED
                    or getattr(row, "canonical_blob_id", None) is None
                ):
                    return None
                payload = await self._artifact_store.read_json(row.canonical_blob_id)
                output = q2_source_output_from_json(payload)
        except ProductionReuseStorageUnavailableError:
            raise
        except Exception:
            return None
        return _ResolvedCheckpoint(
            output=output,
            checkpoint_id=row.id,
            prompt_version=prompt_version,
        )

    async def _persist_checkpoint(
        self,
        planned: PlannedExtractionSource,
        *,
        output: Q2SourceOutput,
        raw_text: str,
        model_run_id: UUID | None,
        prompt_version: str,
    ) -> UUID | None:
        identity = source_checkpoint_identity(
            content_sha256=planned.content_sha256,
            profile=planned.profile,
            prompt_version=prompt_version,
        )
        try:
            (
                raw_blob_id,
                canonical_blob_id,
            ) = await self._artifact_store.store_source_extraction_payloads(
                raw=raw_text,
                canonical=q2_source_output_to_json(output),
            )
            row = SourceExtraction(
                canonical_url=planned.canonical_url,
                source_content_sha256=planned.content_sha256,
                profile=planned.profile,
                contract_version=Q2_EXTRACTION_CONTRACT_VERSION,
                prompt_version=prompt_version,
                parser_version=Q2_MARKDOWN_PARSER_VERSION,
                verifier_version=ARTIFACT_VERIFIER_VERSION,
                source_text_contract_version=source_text_contract_version(),
                model_policy_version=EXTRACTION_MODEL_POLICY_VERSION,
                routing_policy_version=EXTRACTION_ROUTING_POLICY_VERSION,
                status=SourceExtractionStatus.VERIFIED,
                canonical_blob_id=canonical_blob_id,
                raw_blob_id=raw_blob_id,
                model_run_id=model_run_id,
            )
            async with self._uow_factory() as uow:
                repository = getattr(uow, "source_extractions", None)
                claim = getattr(repository, "claim", None)
                if not callable(claim):
                    return None
                claimed = await claim(row)
                if claimed:
                    commit = getattr(uow, "commit", None)
                    if callable(commit):
                        await commit()
                    return row.id
                getter = getattr(repository, "get_by_identity", None)
                if callable(getter):
                    existing = await getter(**identity)
                    return getattr(existing, "id", None)
                return None
        except Exception:
            return None

    # -- model work ---------------------------------------------------------

    def _materialize(
        self,
        planned: PlannedExtractionSource,
        *,
        document: SourceEvidenceDocument,
        output: Q2SourceOutput,
        prompt_version: str,
        checkpoint_id: UUID | None,
        reuse_state: ExtractionReuseState,
        warnings: list[str],
    ) -> _SourceOutcome:
        gated, gate_warnings = gate_source_output(output, document, profile=planned.profile)
        warnings.extend(gate_warnings)
        return _SourceOutcome(
            planned=planned,
            extraction=build_canonical_source_extraction(
                planned=planned,
                output=gated,
                document=document,
                checkpoint_id=checkpoint_id,
                reuse_state=reuse_state,
            ),
            checkpoint_output=output,
            prompt_version=prompt_version,
        )

    async def _execute_unit(
        self,
        unit: _SingleUnit | _BatchUnit,
        *,
        archives: Mapping[UUID, SourceArchive],
        evidence: Mapping[UUID, SourceEvidenceDocument],
    ) -> _UnitResult:
        if isinstance(unit, _SingleUnit):
            return await self._execute_single(unit, archives=archives, evidence=evidence)
        return await self._execute_batch(unit, archives=archives, evidence=evidence)

    async def _execute_single(
        self,
        unit: _SingleUnit,
        *,
        archives: Mapping[UUID, SourceArchive],
        evidence: Mapping[UUID, SourceEvidenceDocument],
    ) -> _UnitResult:
        planned = unit.source
        archive = archives[planned.source_document_id]
        document = evidence[planned.source_document_id]
        text = document.parsed_text
        chunks = archived_source_chunks(text)
        if len(chunks) > MAX_CHUNKS_PER_SOURCE:
            return _UnitResult(
                failures=(
                    (
                        planned,
                        SourceExtractionFailure(
                            "extraction_source_text_too_large",
                            "The archived source exceeds the chunking limit",
                        ),
                    ),
                )
            )
        prompt_version = source_prompt_version(planned.profile)
        warnings: list[str] = []
        try:
            outputs: list[Q2SourceOutput] = []
            raw_texts: list[str] = []
            model_run_id: UUID | None = None
            for index, chunk in enumerate(chunks, start=1):
                execution = await self._invoke_source_model(
                    planned,
                    policy=archive.policy,
                    text=chunk,
                    chunk_index=index,
                    chunk_count=len(chunks),
                )
                output, raw = self._execution_output(execution)
                outputs.append(output)
                raw_texts.append(raw)
                model_run_id = execution.run.id if execution.run is not None else None
            merged = merge_q2_source_outputs(outputs)
            checkpoint_id = await self._persist_checkpoint(
                planned,
                output=merged,
                raw_text="\n\n".join(raw_texts),
                model_run_id=model_run_id,
                prompt_version=prompt_version,
            )
            outcome = self._materialize(
                planned,
                document=document,
                output=merged,
                prompt_version=prompt_version,
                checkpoint_id=checkpoint_id,
                reuse_state=ExtractionReuseState.FRESH,
                warnings=warnings,
            )
        except ExtractionSubmissionAmbiguity as ambiguity:
            return _UnitResult(ambiguity=ambiguity)
        except SourceExtractionFailure as failure:
            return _UnitResult(failures=((planned, failure),))
        except ValueError as exc:
            return _UnitResult(
                failures=(
                    (
                        planned,
                        SourceExtractionFailure("extraction_source_output_invalid", str(exc)),
                    ),
                )
            )
        return _UnitResult(outcomes=(outcome,), warnings=tuple(warnings))

    async def _execute_batch(
        self,
        unit: _BatchUnit,
        *,
        archives: Mapping[UUID, SourceArchive],
        evidence: Mapping[UUID, SourceEvidenceDocument],
    ) -> _UnitResult:
        sources = unit.sources
        entries = make_archive_q2_batch(
            tuple(
                ArchiveQ2BatchSource(
                    source_document_id=source.source_document_id,
                    content_sha256=source.content_sha256,
                )
                for source in sources
            )
        )
        archive = archives[sources[0].source_document_id]
        prompt = ProductionPromptTemplates.get_canonical_archive_batch_prompt(
            tuple(
                (
                    entry.batch_id,
                    evidence[entry.source_document_id].parsed_text,
                )
                for entry in entries
            )
        )
        request = self._model_request(
            planned=sources[0],
            policy=archive.policy,
            prompt=prompt,
            prompt_template_id=_SOURCE_EXTRACTION_BATCH_PROMPT_TEMPLATE_ID,
            prompt_version=CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
            execution_kind="archive_batch",
            metadata={
                "batch_identity": archive_q2_batch_identity(entries),
                "batch_source_count": len(entries),
            },
        )
        try:
            execution = await self._invoke_structured(request, Q2BatchResponse)
            response = self._batch_output(execution)
            attribution = attribute_q2_batch_response(
                response, {entry.batch_id: entry for entry in entries}
            )
        except ExtractionSubmissionAmbiguity as ambiguity:
            return _UnitResult(ambiguity=ambiguity)
        except SourceExtractionFailure as failure:
            return _UnitResult(failures=tuple((source, failure) for source in sources))

        by_id = {source.source_document_id: source for source in sources}
        outcomes: list[_SourceOutcome] = []
        fallbacks: list[PlannedExtractionSource] = []
        warnings: list[str] = list(attribution.warnings)
        for result in attribution.results:
            entry = next(entry for entry in entries if entry.batch_id == result.batch_id)
            planned = by_id[entry.source_document_id]
            if result.output is None:
                warnings.append(f"extraction_batch_source_retry:{result.error_code}")
                fallbacks.append(planned)
                continue
            try:
                checkpoint_id = await self._persist_checkpoint(
                    planned,
                    output=result.output,
                    raw_text=result.raw_block,
                    model_run_id=(execution.run.id if execution.run is not None else None),
                    prompt_version=CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
                )
                outcomes.append(
                    self._materialize(
                        planned,
                        document=evidence[planned.source_document_id],
                        output=result.output,
                        prompt_version=CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
                        checkpoint_id=checkpoint_id,
                        reuse_state=ExtractionReuseState.FRESH,
                        warnings=warnings,
                    )
                )
            except ValueError as exc:
                warnings.append(f"extraction_batch_source_invalid:{exc}")
                fallbacks.append(planned)
        return _UnitResult(
            outcomes=tuple(outcomes),
            fallbacks=tuple(fallbacks),
            warnings=tuple(warnings),
        )

    async def _invoke_source_model(
        self,
        planned: PlannedExtractionSource,
        *,
        policy: SourceAccessPolicy,
        text: str,
        chunk_index: int,
        chunk_count: int,
    ) -> ModelExecution:
        prompt = ProductionPromptTemplates.get_canonical_archive_extraction_prompt(
            text, profile=planned.profile
        )
        request = self._model_request(
            planned=planned,
            policy=policy,
            prompt=prompt,
            prompt_template_id=_SOURCE_EXTRACTION_PROMPT_TEMPLATE_ID,
            prompt_version=source_prompt_version(planned.profile),
            metadata={"chunk_index": chunk_index, "chunk_count": chunk_count},
        )
        return await self._invoke_structured(request, Q2SourceOutput)

    async def _invoke_structured(
        self, request: ModelRequest, output_schema: type[BaseModel]
    ) -> ModelExecution:
        """Ask the gateway for one structured capability, never for a provider."""

        try:
            return await self._model_gateway.extract(request, output_schema)
        except ModelSubmissionReconciliationRequiredError as exc:
            raise ExtractionSubmissionAmbiguity(
                getattr(exc, "code", "model_submission_reconciliation_required"),
                str(exc),
                details=dict(getattr(exc, "details", {}) or {}),
            ) from exc
        except ExternalModelBlockedError as exc:
            raise SourceExtractionFailure("extraction_source_policy_blocked", str(exc)) from exc
        except ModelGatewayError as exc:
            if getattr(exc, "retryable", False):
                raise
            raise SourceExtractionFailure(
                "extraction_model_call_failed",
                str(exc),
                details={"error_code": getattr(exc, "code", None)},
            ) from exc
        except Exception as exc:
            raise SourceExtractionFailure("extraction_model_call_failed", str(exc)) from exc

    def _require_succeeded_run(self, execution: ModelExecution) -> None:
        run = getattr(execution, "run", None)
        status = getattr(run, "status", None)
        if status is ModelRunStatus.NEEDS_REVIEW:
            raise ExtractionSubmissionAmbiguity(
                str(getattr(run, "error_code", None) or "extraction_submission_needs_review"),
                str(getattr(run, "error_message", None) or "Model run needs review"),
                details=dict(getattr(run, "error_details", None) or {}),
            )
        if status is not None and status is not ModelRunStatus.SUCCEEDED:
            raise SourceExtractionFailure(
                "extraction_model_call_failed",
                f"The model run reached status {getattr(status, 'value', status)}",
            )

    def _batch_output(self, execution: ModelExecution) -> Q2BatchResponse:
        self._require_succeeded_run(execution)
        structured = getattr(execution, "structured_output", None)
        if isinstance(structured, Q2BatchResponse):
            return structured
        raise SourceExtractionFailure(
            "extraction_batch_output_invalid",
            "The batch answer is not a source-local Q2 batch object",
        )

    def _execution_output(self, execution: ModelExecution) -> tuple[Q2SourceOutput, str]:
        self._require_succeeded_run(execution)
        structured = getattr(execution, "structured_output", None)
        if isinstance(structured, Q2SourceOutput):
            return structured, json.dumps(
                structured.model_dump(mode="json"), sort_keys=True, ensure_ascii=False
            )
        text = getattr(execution, "output_text", None) or ""
        if text.strip():
            parsed = parse_q2_proposals_markdown(text)
            if parsed.usable and parsed.value is not None:
                return parsed.value, text
        raise SourceExtractionFailure(
            "extraction_source_output_invalid",
            "The provider returned no source-local Q2 output",
        )

    def _model_request(
        self,
        *,
        planned: PlannedExtractionSource,
        policy: SourceAccessPolicy,
        prompt: str,
        prompt_template_id: str,
        prompt_version: str,
        metadata: Mapping[str, Any],
        execution_kind: str = "archive_source",
    ) -> ModelRequest:
        return ModelRequest(
            text=prompt,
            prompt_template_id=prompt_template_id,
            prompt_template_version=prompt_version,
            evidence_pack_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            external_llm_allowed=policy.submission_allowed,
            routing_hint=ModelRoutingHint.BULK_EXTRACTION,
            sensitivity=policy.sensitivity,
            web_search=False,
            metadata={
                "extraction_service_version": PRODUCTION_EXTRACTION_SERVICE_VERSION,
                "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
                "profile_policy_version": EXTRACTION_PROFILE_POLICY_VERSION,
                "profile": planned.profile.value,
                "tier": planned.tier.value,
                "source_content_sha256": planned.content_sha256,
                "source_text_contract_version": source_text_contract_version(),
                "source_evidence_version": SOURCE_EVIDENCE_VERSION,
                "verifier_version": ARTIFACT_VERIFIER_VERSION,
                "model_policy_version": EXTRACTION_MODEL_POLICY_VERSION,
                "routing_policy_version": EXTRACTION_ROUTING_POLICY_VERSION,
                "tlp": policy.tlp.value,
                "do_not_submit": policy.do_not_submit,
                "external_llm_allowed": policy.submission_allowed,
                **dict(metadata),
            },
            parameters={
                "q2_execution_kind": execution_kind,
                "profile": planned.profile.value,
                "source_content_sha256": planned.content_sha256,
                "prompt_version": prompt_version,
                "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
            },
        )


# --- Legacy compatibility boundary ----------------------------------------


def project_legacy_technical_extraction(
    extraction: ProductionExtractionV1,
) -> TechnicalExtraction:
    """Project the canonical contract onto the legacy TechnicalExtraction.

    This is the single one-way compatibility boundary: ``ProductionExtractionV1``
    feeds the current Synthesis and Repair Desk consumers. Reconstructing the
    canonical contract from a legacy extraction is deliberately impossible.
    """

    items: list[ExtractionItem] = []
    rules: list[DetectionRule] = []
    uncertainties: list[str] = []
    for source in extraction.sources:
        source_id = str(source.source_document_id)
        for index, fact in enumerate(source.facts, start=1):
            items.append(
                ExtractionItem(
                    local_id=f"Q2F{source_id}:{index}",
                    category=fact.category,
                    value=fact.value,
                    context=fact.context,
                    artifact_type=None,
                    attack_id=fact.attack_id,
                    reference_ids=(),
                    source_ids=(source_id,),
                    supported=True,
                    semantic_type=_semantic_type_for_fact(fact.category),
                    indicator_status=IndicatorStatus.CONTEXTUAL,
                    provenance=IndicatorProvenance.SOURCE,
                    evidence_quote=fact.evidence_quote,
                    evidence_basis=fact.evidence_basis,
                )
            )
        for index, event in enumerate(source.events, start=1):
            items.append(
                ExtractionItem(
                    local_id=f"Q2E{source_id}:{index}",
                    category="events",
                    value=event.text,
                    context=event.context,
                    artifact_type=None,
                    attack_id=None,
                    reference_ids=(),
                    source_ids=(source_id,),
                    supported=True,
                    semantic_type=_semantic_type_for_fact("events"),
                    indicator_status=IndicatorStatus.CONTEXTUAL,
                    provenance=IndicatorProvenance.SOURCE,
                    evidence_quote=event.evidence_quote,
                    evidence_basis=event.evidence_basis,
                )
            )
        for index, indicator in enumerate(source.indicators, start=1):
            item_status = (
                IndicatorStatus.CONFIRMED_IOC
                if indicator.indicator_status is ExtractionIndicatorStatus.CONFIRMED_IOC
                else IndicatorStatus.CONTEXTUAL
            )
            category, semantic_type, _, display_policy = _artifact_fields(
                indicator.artifact_type, item_status
            )
            try:
                normalized = normalize_indicator_value(indicator.value, indicator.artifact_type)
            except ValueError:
                normalized = None
            items.append(
                ExtractionItem(
                    local_id=f"Q2A{source_id}:{index}",
                    category=category,
                    value=indicator.value,
                    context=indicator.context,
                    artifact_type=indicator.artifact_type,
                    attack_id=None,
                    reference_ids=(),
                    source_ids=(source_id,),
                    supported=True,
                    semantic_type=semantic_type,
                    indicator_status=item_status,
                    provenance=IndicatorProvenance.SOURCE,
                    display_policy=display_policy,
                    normalized_value=normalized,
                    evidence_quote=indicator.evidence_quote,
                    evidence_basis=indicator.evidence_basis,
                )
            )
        for rule in source.rules:
            rules.append(
                DetectionRule(
                    rule_type=rule.rule_type,
                    name=rule.name,
                    body=rule.body,
                    source_ids=(source_id,),
                    context=rule.context,
                    evidence_quote=rule.evidence_quote,
                    supported=True,
                    model_run_ids=(),
                    sha256=rule.sha256,
                    evidence_basis=rule.evidence_basis,
                )
            )
        uncertainties.extend(source.uncertainties)
    return TechnicalExtraction(
        items=tuple(items),
        uncertainties=tuple(dict.fromkeys(uncertainties)),
        rules=tuple(rules),
    )


def _semantic_type_for_fact(category: str) -> Any:
    return _SEMANTIC_TYPE_BY_FACT_CATEGORY.get(category, SemanticType.OTHER)


async def load_production_extraction(
    *,
    uow_factory: ProductionUnitOfWorkFactory,
    artifact_store: ExtractionArtifactStore,
    run_id: UUID,
) -> ProductionExtractionV1 | None:
    """Load the canonical EXTRACTION artifact of one run, when it exists."""

    from cti_app.domain.production_extraction import production_extraction_from_json

    async with uow_factory() as uow:
        artifacts = getattr(uow, "production_artifacts", None)
        getter = getattr(artifacts, "get_current", None)
        if not callable(getter):
            return None
        artifact = await getter(run_id, ProductionArtifactStage.EXTRACTION.value)
    if artifact is None or artifact.canonical_blob_id is None:
        return None
    payload = await artifact_store.read_json(artifact.canonical_blob_id)
    return production_extraction_from_json(payload)


async def load_legacy_technical_extraction(
    *,
    uow_factory: ProductionUnitOfWorkFactory,
    artifact_store: ExtractionArtifactStore,
    run_id: UUID,
) -> TechnicalExtraction | None:
    """Load the legacy extraction view of a run through the single boundary.

    A canonical ``ProductionExtractionV1`` payload is projected one way. A
    pre-AW-011 payload is passed through as already-legacy data; the reverse
    reconstruction never happens.
    """

    async with uow_factory() as uow:
        artifacts = getattr(uow, "production_artifacts", None)
        getter = getattr(artifacts, "get_current", None)
        if not callable(getter):
            return None
        artifact = await getter(run_id, ProductionArtifactStage.EXTRACTION.value)
    if artifact is None or artifact.canonical_blob_id is None:
        return None
    payload = await artifact_store.read_json(artifact.canonical_blob_id)
    try:
        from cti_app.domain.production_extraction import production_extraction_from_json

        canonical = production_extraction_from_json(payload)
    except ValueError:
        return technical_extraction_from_json(payload)
    return project_legacy_technical_extraction(canonical)


__all__ = [
    "BATCH_SOURCE_MAX_CHARS",
    "BATCH_TOTAL_MAX_CHARS",
    "EXTRACTION_MODEL_POLICY_VERSION",
    "EXTRACTION_ROUTING_POLICY_VERSION",
    "MAX_ARCHIVED_SOURCE_BYTES",
    "SOURCE_TEXT_CONTRACT_VERSION",
    "ExtractionArtifactStore",
    "ExtractionControlCode",
    "ExtractionExecutionStatus",
    "ExtractionPlan",
    "ExtractionSubmissionAmbiguity",
    "PlannedExtractionSource",
    "ProductionExtractionControlError",
    "ProductionExtractionExecution",
    "ProductionExtractionService",
    "SourceAccessPolicy",
    "SourceArchive",
    "SourceExtractionFailure",
    "archived_source_chunks",
    "build_canonical_source_extraction",
    "build_extraction_plan",
    "build_production_extraction",
    "build_source_evidence_document",
    "extraction_input_hash",
    "gate_source_output",
    "load_legacy_technical_extraction",
    "load_production_extraction",
    "load_reference_corpus",
    "load_source_archive",
    "local_evidence_quote",
    "merge_q2_source_outputs",
    "production_extraction_metadata",
    "project_legacy_technical_extraction",
    "references_corpus_hash",
    "resolve_source_policy",
    "source_checkpoint_identity",
    "source_prompt_version",
    "source_text_contract_version",
]
