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

It is also the single compatibility boundary towards the not-yet-migrated
``TechnicalExtraction`` consumers (Synthesis, Repair Desk, state transfer):
``ProductionExtractionV1`` is projected one way, never rebuilt from legacy data.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from enum import StrEnum
from typing import Any, Protocol, cast
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ValidationError

from cti_app.application.extraction import _html_encoding, parse_document
from cti_app.application.model_gateway import (
    ExternalModelBlockedError,
    ModelExecution,
    ModelGateway,
    ModelGatewayError,
    ModelRequest,
    ModelRoutingHint,
    ModelSubmissionReconciliationRequiredError,
    StructuredOutputError,
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
    Q2_SCHEMA_VERSION,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    Q2SourceOutput,
    SemanticType,
    TechnicalExtraction,
    project_q2_source_output,
    q2_source_output_from_json,
    q2_source_output_to_json,
    technical_extraction_from_json,
)
from cti_app.application.production_prompts import (
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
    SourceEvidenceRejection,
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
    PRODUCTION_RECONCILIATION_ERROR_CODE,
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
    model_run_awaits_reconciliation,
)
from cti_app.domain.production_extraction import (
    EXTRACTION_PROFILE_POLICY_VERSION,
    EXTRACTION_TIER_ORDER,
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
    production_extraction_from_json,
)
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceTier,
)
from cti_app.domain.publication import ArtifactType

#: Canonical service contract version. It participates in the run-level input
#: hash but never in a per-source checkpoint identity.
PRODUCTION_EXTRACTION_SERVICE_VERSION = "production-extraction-service-v2"

#: Model and routing policies of the archive-backed path. The service asks for a
#: capability; the gateway decides which authorized adapter answers it.
EXTRACTION_MODEL_POLICY_VERSION = "archive-source-extraction-v1"
EXTRACTION_ROUTING_POLICY_VERSION = "bulk-extraction-routing-v1"

#: The provider answers one structured ``Q2SourceOutput``; the gateway validates
#: it against the schema, so the response parser is the schema itself.
EXTRACTION_RESPONSE_PARSER_VERSION = f"q2-structured-schema-v{Q2_SCHEMA_VERSION}"

#: Deterministic archived-text transform. Chunking is a pure function of the
#: archived content, the transformers' versions, the maximum size and the
#: overlap, so it never depends on the provider that answers.
SOURCE_TEXT_CONTRACT_VERSION = "archived-text-v1"
SOURCE_TEXT_CHUNKER_VERSION = "chunker-v1"
SOURCE_CHUNK_MAX_CHARS = 24_000
SOURCE_CHUNK_OVERLAP_CHARS = 400

#: Bounded model input. A capture larger than the chunk size is chunked
#: deterministically instead of being truncated.
MAX_ARCHIVED_SOURCE_BYTES = 25 * 1024 * 1024
MAX_CHUNKS_PER_SOURCE = 64
BATCH_SOURCE_MAX_CHARS = 8_000
BATCH_TOTAL_MAX_CHARS = 24_000

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


class ExtractionFailureCode(StrEnum):
    """Source-local failures. CORE blocks the stage; the others are omitted."""

    CORE_SOURCE_FAILED = "extraction_core_source_failed"
    SOURCE_TEXT_UNREADABLE = "extraction_source_text_unreadable"
    SOURCE_TEXT_TOO_LARGE = "extraction_source_text_too_large"
    SOURCE_OUTPUT_INVALID = "extraction_source_output_invalid"
    SOURCE_POLICY_BLOCKED = "extraction_source_policy_blocked"
    MODEL_CALL_FAILED = "extraction_model_call_failed"
    BATCH_SOURCE_UNATTRIBUTED = "extraction_batch_source_unattributed"


class ExtractionExecutionStatus(StrEnum):
    """Outcome of one canonical extraction execution."""

    SUCCEEDED = "succeeded"
    #: A provider submission may have been accepted and is not reconciled.
    NEEDS_REVIEW = "needs_review"
    #: A control invariant failed or a CORE source has no FULL extraction.
    BLOCKED = "blocked"


class _ExtractionError(RuntimeError):
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


class ProductionExtractionControlError(_ExtractionError):
    """One control invariant failed; no fallback path may be attempted."""


class SourceExtractionFailure(_ExtractionError):
    """One source-local failure. CORE blocks the stage; others are omitted."""


class ExtractionSubmissionAmbiguity(_ExtractionError):
    """A provider submission may have been accepted and cannot be replayed."""


class ExtractionArtifactStore(Protocol):
    """The payload storage the canonical extraction boundary needs."""

    async def read_json(self, blob_id: UUID) -> dict[str, Any]: ...

    async def read_bytes(self, blob_id: UUID, *, max_bytes: int) -> bytes: ...

    async def store_source_extraction_payloads(
        self, *, raw: str, canonical: dict[str, Any]
    ) -> tuple[UUID | None, UUID]: ...


BeforeModelCall = Callable[[], Awaitable[None]]


# --- Planning --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class PlannedExtractionSource:
    """One eligible corpus source, with the profile its tier imposes."""

    source_document_id: UUID
    canonical_url: str
    content_sha256: str
    tier: ProductionReferenceTier
    kind: ProductionReferenceKind
    role: SourceRole
    profile: ExtractionProfile
    title: str | None
    collection_state: CollectionState
    position: int = 0

    @property
    def computation_key(self) -> tuple[str, ExtractionProfile]:
        """Identical bytes under one profile are computed once per run."""
        return (self.content_sha256, self.profile)


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

    # Stored in a 64-character checkpoint identity column.
    return (
        f"{SOURCE_TEXT_CONTRACT_VERSION}:{SOURCE_TEXT_CHUNKER_VERSION}"
        f":evidence-{SOURCE_EVIDENCE_VERSION}"
        f":{SOURCE_CHUNK_MAX_CHARS}/{SOURCE_CHUNK_OVERLAP_CHARS}"
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
                    error_code=None,
                )
            )
            warnings.append(f"reference_not_eligible:{source.tier.value}:{source.canonical_url}")
            continue
        planned.append(
            PlannedExtractionSource(
                source_document_id=source.source_document_id,
                canonical_url=source.canonical_url,
                content_sha256=source.content_sha256,
                tier=source.tier,
                kind=source.kind,
                role=source.role,
                profile=extraction_profile_for_tier(source.tier),
                title=source.title,
                collection_state=source.collection_state,
            )
        )

    ordered = sorted(
        planned,
        key=lambda source: (
            EXTRACTION_TIER_ORDER[source.tier],
            source.canonical_url,
            str(source.source_document_id),
        ),
    )
    sources = tuple(replace(source, position=index) for index, source in enumerate(ordered))
    corpus_hash = references_corpus_hash(corpus)
    return ExtractionPlan(
        subject_id=corpus.subject_id,
        production_input_hash=corpus.production_input_hash,
        references_corpus_hash=corpus_hash,
        profile_policy_version=EXTRACTION_PROFILE_POLICY_VERSION,
        input_hash=extraction_input_hash(references_corpus_hash=corpus_hash),
        sources=sources,
        omitted_sources=tuple(
            sorted(
                omitted,
                key=lambda omission: (
                    EXTRACTION_TIER_ORDER[omission.tier],
                    omission.canonical_url,
                ),
            )
        ),
        warnings=tuple(dict.fromkeys(warnings)),
    )


def extraction_input_hash(*, references_corpus_hash: str) -> str:
    """Return the deterministic functional identity of one extraction run.

    The corpus hash already covers every planned source (document, bytes, tier);
    the remaining dimensions are the versions that define the result. Execution
    identities (run id, job id, model run id, provider, conversation id) and
    timestamps are deliberately absent: two functionally identical runs get the
    same hash.
    """

    return _canonical_hash(
        {
            "stage": "extraction",
            "service_version": PRODUCTION_EXTRACTION_SERVICE_VERSION,
            "references_corpus_hash": references_corpus_hash,
            "profile_policy_version": EXTRACTION_PROFILE_POLICY_VERSION,
            "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
            "source_text_contract_version": source_text_contract_version(),
            "prompt_versions": {
                **{
                    profile.value: version
                    for profile, version in CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE.items()
                },
                "ioc_rules_batch": CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
            },
            "response_parser_version": EXTRACTION_RESPONSE_PARSER_VERSION,
            "verifier_version": ARTIFACT_VERIFIER_VERSION,
            "model_policy_version": EXTRACTION_MODEL_POLICY_VERSION,
            "routing_policy_version": EXTRACTION_ROUTING_POLICY_VERSION,
        }
    )


def _canonical_hash(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def source_prompt_version(profile: ExtractionProfile) -> str:
    """The canonical single-source prompt version of one profile."""

    try:
        return CANONICAL_EXTRACTION_PROMPT_VERSION_BY_PROFILE[profile]
    except KeyError as exc:
        raise ValueError(f"Unsupported extraction profile: {profile}") from exc


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
        "parser_version": EXTRACTION_RESPONSE_PARSER_VERSION,
        "verifier_version": ARTIFACT_VERIFIER_VERSION,
        "source_text_contract_version": source_text_contract_version(),
        "model_policy_version": EXTRACTION_MODEL_POLICY_VERSION,
        "routing_policy_version": EXTRACTION_ROUTING_POLICY_VERSION,
    }


def _checkpoint_identities_satisfying(
    profile: ExtractionProfile,
) -> tuple[tuple[ExtractionProfile, str], ...]:
    """Every (profile, prompt) checkpoint that may answer ``profile``.

    IOC_RULES accepts its own single-source or batch checkpoint and a FULL one,
    whose artifacts/rules contract is a strict superset of IOC_RULES. FULL is
    only ever satisfied by a FULL checkpoint.
    """

    if profile is ExtractionProfile.FULL:
        return ((ExtractionProfile.FULL, source_prompt_version(ExtractionProfile.FULL)),)
    return (
        (ExtractionProfile.IOC_RULES, source_prompt_version(ExtractionProfile.IOC_RULES)),
        (ExtractionProfile.IOC_RULES, CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION),
        (ExtractionProfile.FULL, source_prompt_version(ExtractionProfile.FULL)),
    )


def is_current_source_checkpoint(
    row: SourceExtraction, *, content_sha256: str, profile: ExtractionProfile
) -> bool:
    """Whether a durable checkpoint row would satisfy ``profile`` today."""

    if row.status is not SourceExtractionStatus.VERIFIED or row.canonical_blob_id is None:
        return False
    for candidate_profile, prompt_version in _checkpoint_identities_satisfying(profile):
        identity = source_checkpoint_identity(
            content_sha256=content_sha256,
            profile=candidate_profile,
            prompt_version=prompt_version,
        )
        if all(
            getattr(row, key).value == value if key == "profile" else getattr(row, key) == value
            for key, value in identity.items()
        ):
            return True
    return False


def extraction_model_run_id(*, run: ProductionRun, unit_identity: Mapping[str, Any]) -> UUID:
    """Stable ModelRun identity of one model call inside one pipeline generation.

    A retry of the same generation reuses this identity, so the gateway either
    returns the durable answer, resubmits only after a proven pre-submission
    failure, or asks for reconciliation -- it never double-submits.
    """

    return uuid5(
        NAMESPACE_URL,
        "production-extraction-model:"
        f"{run.id}:g{run.pipeline_generation}:{_canonical_hash(dict(unit_identity))}",
    )


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

    artifact = cast(
        "ProductionArtifact | None",
        await uow.production_artifacts.get_current(
            run.id, ProductionArtifactStage.REFERENCES.value
        ),
    )
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

    if snapshot is None:
        snapshot = await uow.production_input_snapshots.get_by_run(run.id)
    if snapshot is None:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_SNAPSHOT_MISMATCH,
            "The production input snapshot of this run is missing",
            details={"run_id": str(run.id)},
        )
    if corpus.production_input_hash != snapshot.input_hash:
        raise ProductionExtractionControlError(
            ExtractionControlCode.REFERENCE_SNAPSHOT_MISMATCH,
            "The canonical corpus does not match the production input snapshot",
            details={
                "corpus_input_hash": corpus.production_input_hash,
                "snapshot_input_hash": snapshot.input_hash,
            },
        )
    return corpus


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
    subject_id: UUID,
    artifact_store: ExtractionArtifactStore,
) -> SourceArchive:
    """Resolve one eligible source strictly by its exact document identity.

    The document is read by ``source_document_id``; no URL lookup, no "latest
    document of this URL" and no network acquisition exists on this path. The
    archived decoded blob is verified against ``corpus.content_sha256`` before
    any model call.
    """

    document = await uow.source_documents.get(planned.source_document_id)
    if document is None or document.subject_id != subject_id:
        raise ProductionExtractionControlError(
            ExtractionControlCode.SOURCE_DOCUMENT_MISSING,
            "The exact archived document of this corpus source is missing",
            details={
                "source_document_id": str(planned.source_document_id),
                "canonical_url": planned.canonical_url,
            },
        )

    collection: SourceCollection | None = None
    if document.source_collection_id is not None:
        collection = await uow.source_collections.get(document.source_collection_id)
    if collection is not None and (
        collection.subject_id != subject_id
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

    if document.decoded_blob_id is None:
        raise ProductionExtractionControlError(
            ExtractionControlCode.SOURCE_DOCUMENT_MISSING,
            "The exact archived document carries no decoded blob",
            details={"source_document_id": str(document.id)},
        )
    try:
        content = await artifact_store.read_bytes(
            document.decoded_blob_id, max_bytes=MAX_ARCHIVED_SOURCE_BYTES
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
            ExtractionFailureCode.SOURCE_TEXT_UNREADABLE,
            "The archived source text is not readable",
        ) from exc
    if not parsed.text.strip():
        raise SourceExtractionFailure(
            ExtractionFailureCode.SOURCE_TEXT_UNREADABLE,
            "The archived source carries no readable text",
        )
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
    """Chunk one archived text deterministically, never truncating it.

    Paragraphs are packed up to ``SOURCE_CHUNK_MAX_CHARS``; a single oversized
    paragraph is cut in fixed windows overlapping by
    ``SOURCE_CHUNK_OVERLAP_CHARS``.
    """

    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    if len(normalized) <= SOURCE_CHUNK_MAX_CHARS:
        return (normalized,)
    # The overlap never exceeds a quarter of a chunk, so every window advances.
    step = SOURCE_CHUNK_MAX_CHARS - min(SOURCE_CHUNK_OVERLAP_CHARS, SOURCE_CHUNK_MAX_CHARS // 4)
    chunks: list[str] = []
    current = ""
    for paragraph in normalized.split("\n\n"):
        candidate = paragraph if not current else f"{current}\n\n{paragraph}"
        if len(candidate) <= SOURCE_CHUNK_MAX_CHARS:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(paragraph) > SOURCE_CHUNK_MAX_CHARS:
            chunks.append(paragraph[:SOURCE_CHUNK_MAX_CHARS])
            paragraph = paragraph[step:]
        current = paragraph
    if current:
        chunks.append(current)
    return tuple(chunks)


def merge_q2_source_outputs(outputs: Sequence[Q2SourceOutput]) -> Q2SourceOutput:
    """Merge chunk-local outputs into one deterministic source-local result."""

    facts = [fact for output in outputs for fact in output.facts]
    events = [event for output in outputs for event in output.events]
    artifacts = [artifact for output in outputs for artifact in output.artifacts]
    rules = [rule for output in outputs for rule in output.rules]
    return Q2SourceOutput(
        facts=_unique(facts, lambda fact: (fact.category, fact.value.strip().casefold())),
        events=_unique(
            events, lambda event: (event.event_date, event.date_text, event.text.strip().casefold())
        ),
        artifacts=_unique(
            artifacts, lambda artifact: (artifact.artifact_type, artifact.value.strip().casefold())
        ),
        rules=_unique(rules, lambda rule: (rule.rule_type, _normalized_rule_body(rule.body))),
        uncertainties=list(
            dict.fromkeys(item for output in outputs for item in output.uncertainties)
        ),
    )


def _normalized_rule_body(body: str) -> str:
    return body.replace("\r\n", "\n").replace("\r", "\n")


# --- Evidence gate and canonical building -----------------------------------


def gate_source_output(
    output: Q2SourceOutput,
    document: SourceEvidenceDocument,
    *,
    profile: ExtractionProfile,
) -> tuple[Q2SourceOutput, tuple[str, ...], tuple[SourceEvidenceRejection, ...]]:
    """Run the deterministic local evidence gate of one profile."""

    if profile is ExtractionProfile.FULL:
        result = verify_q2_output_against_source(output, document)
    else:
        result = verify_ioc_rules_output_against_source(output, document)
    warnings = [
        *result.warnings,
        *(
            f"extraction_proposal_rejected:{rejection.reason_code}"
            for rejection in result.rejections
        ),
    ]
    return result.output, tuple(dict.fromkeys(warnings)), result.rejections


def build_canonical_source_extraction(
    *,
    planned: PlannedExtractionSource,
    gated: Q2SourceOutput,
    checkpoint_id: UUID | None,
    reuse_state: ExtractionReuseState,
) -> ProductionSourceExtractionV1:
    """Attach deterministic identities to evidence-gated proposals.

    ``gated`` must come from :func:`gate_source_output`: every proposal then
    carries the local quote of the exact archived document in
    ``evidence_quote``.
    """

    document_ids = (planned.source_document_id,)
    basis = ProductionEvidenceBasis.SOURCE_VERIFIED
    facts = tuple(
        ExtractionFactV1(
            category=fact.category,
            value=fact.value,
            attack_id=fact.attack_id,
            context=fact.context,
            evidence_quote=fact.evidence_quote,
            evidence_basis=basis,
            source_document_ids=document_ids,
        )
        for fact in _unique(
            gated.facts, lambda fact: (fact.category, fact.value.strip().casefold())
        )
    )
    events = tuple(
        ExtractionEventV1(
            event_date=event.event_date,
            date_text=event.date_text,
            text=event.text,
            context=event.context,
            evidence_quote=event.evidence_quote,
            evidence_basis=basis,
            source_document_ids=document_ids,
        )
        for event in _unique(
            gated.events,
            lambda event: (event.event_date, event.date_text, event.text.strip().casefold()),
        )
    )
    indicators = tuple(
        ExtractionIndicatorV1(
            value=artifact.value,
            artifact_type=ArtifactType(artifact.artifact_type),
            indicator_status=ExtractionIndicatorStatus(artifact.indicator_status),
            context=artifact.context,
            evidence_quote=artifact.evidence_quote,
            evidence_basis=basis,
            source_document_ids=document_ids,
        )
        for artifact in _unique(
            [
                artifact
                for artifact in gated.artifacts
                if artifact.indicator_status in _CANONICAL_INDICATOR_STATUSES
            ],
            lambda artifact: (artifact.artifact_type, _indicator_key(artifact)),
        )
    )
    rules = tuple(
        ExtractionRuleV1(
            rule_type=rule.rule_type,
            name=rule.name,
            body=_normalized_rule_body(rule.body),
            sha256=hashlib.sha256(_normalized_rule_body(rule.body).encode("utf-8")).hexdigest(),
            context=rule.context,
            evidence_quote=rule.evidence_quote,
            evidence_basis=basis,
            source_document_ids=document_ids,
        )
        for rule in _unique(
            gated.rules, lambda rule: (rule.rule_type, _normalized_rule_body(rule.body))
        )
    )
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
        facts=facts,
        events=events,
        indicators=indicators,
        rules=rules,
        uncertainties=tuple(dict.fromkeys(gated.uncertainties)),
    )


_CANONICAL_INDICATOR_STATUSES = frozenset(status.value for status in ExtractionIndicatorStatus)


def _unique[T](items: Sequence[T], key: Callable[[T], Any]) -> list[T]:
    seen: set[Any] = set()
    kept: list[T] = []
    for item in items:
        identity = key(item)
        if identity in seen:
            continue
        seen.add(identity)
        kept.append(item)
    return kept


def _indicator_key(artifact: Any) -> str:
    try:
        return normalize_indicator_value(artifact.value, ArtifactType(artifact.artifact_type))
    except ValueError:
        return str(artifact.value).strip().casefold()


def _union_provenance(
    sources: Sequence[ProductionSourceExtractionV1],
) -> tuple[ProductionSourceExtractionV1, ...]:
    """Give every equivalent element the union of the documents publishing it.

    Deduplication across sources never removes a source entry: each source keeps
    its own elements, and an element published by several documents names all of
    them, so no provenance silently disappears.
    """

    def keys(source: ProductionSourceExtractionV1) -> dict[str, list[Any]]:
        return {
            "facts": [(fact.category, fact.value.strip().casefold()) for fact in source.facts],
            "events": [
                (event.event_date, event.text.strip().casefold()) for event in source.events
            ],
            "indicators": [
                (indicator.artifact_type, _indicator_key(indicator))
                for indicator in source.indicators
            ],
            "rules": [(rule.rule_type, rule.sha256) for rule in source.rules],
        }

    documents: dict[tuple[str, Any], set[UUID]] = {}
    for source in sources:
        for kind, identities in keys(source).items():
            for identity in identities:
                documents.setdefault((kind, identity), set()).add(source.source_document_id)

    def widened(items: tuple[Any, ...], kind: str, identities: list[Any]) -> tuple[Any, ...]:
        return tuple(
            replace(item, source_document_ids=tuple(documents[(kind, identity)]))
            for item, identity in zip(items, identities, strict=True)
        )

    result: list[ProductionSourceExtractionV1] = []
    for source in sources:
        source_keys = keys(source)
        result.append(
            replace(
                source,
                facts=widened(source.facts, "facts", source_keys["facts"]),
                events=widened(source.events, "events", source_keys["events"]),
                indicators=widened(source.indicators, "indicators", source_keys["indicators"]),
                rules=widened(source.rules, "rules", source_keys["rules"]),
            )
        )
    return tuple(result)


def build_production_extraction(
    *,
    plan: ExtractionPlan,
    sources: Sequence[ProductionSourceExtractionV1],
    failed_sources: Sequence[tuple[PlannedExtractionSource, str]] = (),
    warnings: Sequence[str] = (),
) -> ProductionExtractionV1:
    """Aggregate gated source results into the canonical run-level contract."""

    failures = tuple(
        ProductionExtractionOmissionV1(
            canonical_url=planned.canonical_url,
            tier=planned.tier,
            collection_state=planned.collection_state,
            reason=ProductionExtractionOmissionReason.SOURCE_EXTRACTION_FAILED,
            error_code=error_code,
        )
        for planned, error_code in failed_sources
    )
    return ProductionExtractionV1(
        schema_version=PRODUCTION_EXTRACTION_SCHEMA_VERSION,
        subject_id=plan.subject_id,
        production_input_hash=plan.production_input_hash,
        references_corpus_hash=plan.references_corpus_hash,
        profile_policy_version=plan.profile_policy_version,
        sources=_union_provenance(sources),
        omitted_sources=(*plan.omitted_sources, *failures),
        warnings=tuple(dict.fromkeys((*plan.warnings, *warnings))),
    )


def production_extraction_metadata(extraction: ProductionExtractionV1) -> dict[str, Any]:
    """The bounded counter projection an EXTRACTION artifact may store."""

    sources = extraction.sources
    full_sources = sum(source.profile is ExtractionProfile.FULL for source in sources)
    return {
        "schema_version": extraction.schema_version,
        "service_version": PRODUCTION_EXTRACTION_SERVICE_VERSION,
        "contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
        "profile_policy_version": extraction.profile_policy_version,
        "source_count": len(sources),
        "full_source_count": full_sources,
        "ioc_rules_source_count": len(sources) - full_sources,
        "reused_source_count": sum(
            source.reuse_state is ExtractionReuseState.REUSED for source in sources
        ),
        "fresh_source_count": sum(
            source.reuse_state is ExtractionReuseState.FRESH for source in sources
        ),
        "duplicate_source_count": sum(
            source.reuse_state is ExtractionReuseState.CONTENT_DUPLICATE for source in sources
        ),
        "omitted_source_count": len(extraction.omitted_sources),
        "fact_count": sum(len(source.facts) for source in sources),
        "event_count": sum(len(source.events) for source in sources),
        "indicator_count": sum(len(source.indicators) for source in sources),
        "rule_count": sum(len(source.rules) for source in sources),
        "warning_count": len(extraction.warnings),
    }


# --- Execution --------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class ExtractionRejection:
    """One artifact/rule proposal the evidence gate refused, for the Repair Desk."""

    source: PlannedExtractionSource
    model_run_id: UUID | None
    rejection: SourceEvidenceRejection


@dataclass(frozen=True, slots=True)
class ProductionExtractionExecution:
    """The outcome of one canonical extraction execution."""

    status: ExtractionExecutionStatus
    plan: ExtractionPlan | None = None
    extraction: ProductionExtractionV1 | None = None
    rejections: tuple[ExtractionRejection, ...] = ()
    model_calls: int = 0
    error_code: str | None = None
    error: str | None = None
    details: Mapping[str, Any] = field(default_factory=dict)

    @property
    def succeeded(self) -> bool:
        return self.status is ExtractionExecutionStatus.SUCCEEDED


@dataclass(frozen=True, slots=True)
class _Computation:
    """One source-local output shared by every source with the same bytes."""

    output: Q2SourceOutput
    checkpoint_id: UUID | None
    model_run_id: UUID | None
    fresh: bool


@dataclass(frozen=True, slots=True)
class _BatchResult:
    computations: dict[UUID, _Computation]
    retry_individually: tuple[PlannedExtractionSource, ...]
    warnings: tuple[str, ...]


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

    async def plan(
        self, *, run: ProductionRun, snapshot: ProductionInputSnapshot | None = None
    ) -> ExtractionPlan:
        """Load the canonical corpus of ``run`` and plan its extraction."""

        async with self._uow_factory() as uow:
            corpus = await load_reference_corpus(
                uow=uow, run=run, snapshot=snapshot, artifact_store=self._artifact_store
            )
        return build_extraction_plan(corpus)

    async def execute(
        self,
        *,
        run: ProductionRun,
        snapshot: ProductionInputSnapshot | None = None,
        plan: ExtractionPlan | None = None,
        before_model_call: BeforeModelCall | None = None,
    ) -> ProductionExtractionExecution:
        """Run the canonical extraction of one production run.

        A retryable ``ModelGatewayError`` (a proven pre-submission failure) is
        raised to the caller, which retries the stage with the same
        deterministic ModelRun identities.
        """

        try:
            if plan is None:
                plan = await self.plan(run=run, snapshot=snapshot)
            archives = await self._load_archives(plan)
        except ProductionExtractionControlError as control:
            # A control invariant failed: the legacy REFERENCES RAW is never a
            # fallback input and no model call is attempted.
            return ProductionExtractionExecution(
                status=ExtractionExecutionStatus.BLOCKED,
                plan=plan,
                error_code=control.code,
                error=str(control),
                details=control.details,
            )
        return await _ExtractionRun(
            service=self,
            run=run,
            plan=plan,
            archives=archives,
            before_model_call=before_model_call,
        ).execute()

    async def _load_archives(self, plan: ExtractionPlan) -> dict[UUID, SourceArchive]:
        archives: dict[UUID, SourceArchive] = {}
        async with self._uow_factory() as uow:
            for planned in plan.sources:
                archives[planned.source_document_id] = await load_source_archive(
                    uow=uow,
                    planned=planned,
                    subject_id=plan.subject_id,
                    artifact_store=self._artifact_store,
                )
        return archives

    # -- checkpoints --------------------------------------------------------

    async def _read_checkpoint(self, planned: PlannedExtractionSource) -> _Computation | None:
        for profile, prompt_version in _checkpoint_identities_satisfying(planned.profile):
            identity = source_checkpoint_identity(
                content_sha256=planned.content_sha256,
                profile=profile,
                prompt_version=prompt_version,
            )
            async with self._uow_factory() as uow:
                row = await uow.source_extractions.get_by_identity(**identity)
            if (
                row is None
                or row.status is not SourceExtractionStatus.VERIFIED
                or row.canonical_blob_id is None
            ):
                continue
            try:
                output = q2_source_output_from_json(
                    await self._artifact_store.read_json(row.canonical_blob_id)
                )
            except ProductionReuseStorageUnavailableError:
                raise
            except Exception:
                # A missing or unreadable checkpoint payload is a miss, never a
                # canonical result; only an unavailable store is an outage.
                continue
            return _Computation(
                output=project_q2_source_output(output, planned.profile),
                checkpoint_id=row.id,
                model_run_id=row.model_run_id,
                fresh=False,
            )
        return None

    async def _persist_checkpoint(
        self,
        planned: PlannedExtractionSource,
        *,
        output: Q2SourceOutput,
        raw_text: str,
        model_run_id: UUID | None,
        prompt_version: str,
    ) -> UUID:
        """Persist one content-addressed checkpoint, or return the winner's id."""

        identity = source_checkpoint_identity(
            content_sha256=planned.content_sha256,
            profile=planned.profile,
            prompt_version=prompt_version,
        )
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
            contract_version=identity["contract_version"],
            prompt_version=prompt_version,
            parser_version=identity["parser_version"],
            verifier_version=identity["verifier_version"],
            source_text_contract_version=identity["source_text_contract_version"],
            model_policy_version=identity["model_policy_version"],
            routing_policy_version=identity["routing_policy_version"],
            status=SourceExtractionStatus.VERIFIED,
            canonical_blob_id=canonical_blob_id,
            raw_blob_id=raw_blob_id,
            model_run_id=model_run_id,
        )
        async with self._uow_factory() as uow:
            if await uow.source_extractions.claim(row):
                await uow.commit()
                return row.id
            existing = await uow.source_extractions.get_by_identity(**identity)
        if existing is None:
            raise RuntimeError("Source extraction checkpoint claim lost without a winner")
        return existing.id

    # -- model requests -----------------------------------------------------

    async def invoke(self, request: ModelRequest, output_schema: type[BaseModel]) -> ModelExecution:
        """Ask the gateway for one structured capability, never for a provider."""

        try:
            execution = await self._model_gateway.extract(request, output_schema)
        except ModelSubmissionReconciliationRequiredError as exc:
            raise _ambiguity(exc.model_run_id or request.run_id, dict(exc.details)) from exc
        except ExternalModelBlockedError as exc:
            raise SourceExtractionFailure(
                ExtractionFailureCode.SOURCE_POLICY_BLOCKED, str(exc)
            ) from exc
        except StructuredOutputError as exc:
            # A durable answer that does not match the schema.
            raise SourceExtractionFailure(
                ExtractionFailureCode.SOURCE_OUTPUT_INVALID, str(exc)
            ) from exc
        except ModelGatewayError as exc:
            if exc.retryable:
                raise
            raise SourceExtractionFailure(
                ExtractionFailureCode.MODEL_CALL_FAILED,
                str(exc),
                details={"error_code": exc.code},
            ) from exc
        model_run = execution.run
        if model_run is not None and model_run.status is ModelRunStatus.NEEDS_REVIEW:
            if model_run_awaits_reconciliation(model_run.error_code):
                raise _ambiguity(model_run.id, dict(model_run.error_details or {}))
            raise SourceExtractionFailure(
                ExtractionFailureCode.MODEL_CALL_FAILED,
                model_run.error_message or "The model run needs review",
                details={"error_code": model_run.error_code, "model_run_id": str(model_run.id)},
            )
        if model_run is not None and model_run.status is not ModelRunStatus.SUCCEEDED:
            raise SourceExtractionFailure(
                ExtractionFailureCode.MODEL_CALL_FAILED,
                f"The model run reached status {model_run.status.value}",
            )
        if isinstance(execution.structured_output, output_schema):
            return execution
        # Validation is deferred to this boundary: an answer outside the schema
        # is a source-local content failure, never a submission ambiguity.
        try:
            structured = output_schema.model_validate_json(execution.output_text or "")
        except ValidationError as exc:
            raise SourceExtractionFailure(
                ExtractionFailureCode.SOURCE_OUTPUT_INVALID,
                "The provider answer is not a source-local structured output",
            ) from exc
        return replace(execution, structured_output=structured)

    def model_request(
        self,
        *,
        run: ProductionRun,
        profile: ExtractionProfile,
        policy: SourceAccessPolicy,
        prompt: str,
        prompt_template_id: str,
        prompt_version: str,
        unit_identity: Mapping[str, Any],
        metadata: Mapping[str, Any],
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
            run_id=extraction_model_run_id(run=run, unit_identity=unit_identity),
            # The gateway only resubmits a FAILED run whose submission state
            # proves the provider was never reached.
            allow_failed_resubmit=True,
            metadata={
                "defer_validation": True,
                "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
                "profile_policy_version": EXTRACTION_PROFILE_POLICY_VERSION,
                "profile": profile.value,
                "source_text_contract_version": source_text_contract_version(),
                "model_policy_version": EXTRACTION_MODEL_POLICY_VERSION,
                "routing_policy_version": EXTRACTION_ROUTING_POLICY_VERSION,
                "tlp": policy.tlp.value,
                "do_not_submit": policy.do_not_submit,
                "external_llm_allowed": policy.submission_allowed,
                **dict(metadata),
            },
            parameters={
                "profile": profile.value,
                "prompt_version": prompt_version,
                "extraction_contract_version": Q2_EXTRACTION_CONTRACT_VERSION,
            },
        )


def _ambiguity(
    model_run_id: UUID | None, details: Mapping[str, Any]
) -> ExtractionSubmissionAmbiguity:
    """The reconciliation identity the job handler records on the run."""

    return ExtractionSubmissionAmbiguity(
        PRODUCTION_RECONCILIATION_ERROR_CODE,
        "A provider submission may have been accepted and must be reconciled",
        details={
            **dict(details),
            "error_code": PRODUCTION_RECONCILIATION_ERROR_CODE,
            "model_run_id": str(model_run_id) if model_run_id is not None else None,
        },
    )


class _ExtractionRun:
    """The mutable bookkeeping of one :meth:`ProductionExtractionService.execute`."""

    def __init__(
        self,
        *,
        service: ProductionExtractionService,
        run: ProductionRun,
        plan: ExtractionPlan,
        archives: Mapping[UUID, SourceArchive],
        before_model_call: BeforeModelCall | None,
    ) -> None:
        self._service = service
        self._run = run
        self._plan = plan
        self._archives = archives
        self._before_model_call = before_model_call
        self._evidence: dict[UUID, SourceEvidenceDocument] = {}
        self._computations: dict[tuple[str, ExtractionProfile], _Computation] = {}
        self._failures: dict[UUID, SourceExtractionFailure] = {}
        self._warnings: list[str] = []
        self._model_calls = 0

    async def execute(self) -> ProductionExtractionExecution:
        try:
            await self._compute()
        except ExtractionSubmissionAmbiguity as ambiguity:
            return self._stopped(ExtractionExecutionStatus.NEEDS_REVIEW, ambiguity)
        except _CoreSourceFailed as blocked:
            return self._stopped(ExtractionExecutionStatus.BLOCKED, blocked.error)
        return self._assemble()

    def _stopped(
        self, status: ExtractionExecutionStatus, error: _ExtractionError
    ) -> ProductionExtractionExecution:
        return ProductionExtractionExecution(
            status=status,
            plan=self._plan,
            model_calls=self._model_calls,
            error_code=error.code,
            error=str(error),
            details=error.details,
        )

    def _fail(self, planned: PlannedExtractionSource, failure: SourceExtractionFailure) -> None:
        """Record a source-local failure; a CORE failure stops the whole stage."""

        for source in self._plan.sources:
            if source.computation_key == planned.computation_key:
                self._failures[source.source_document_id] = failure
        if any(
            source.tier is ProductionReferenceTier.CORE
            and source.source_document_id in self._failures
            for source in self._plan.sources
        ):
            raise _CoreSourceFailed(planned, failure)

    async def _compute(self) -> None:
        for planned in self._plan.sources:
            archive = self._archives[planned.source_document_id]
            try:
                self._evidence[planned.source_document_id] = build_source_evidence_document(
                    archive.content, mime_type=archive.document.detected_mime_type
                )
            except SourceExtractionFailure as failure:
                self._fail(planned, failure)

        misses: list[PlannedExtractionSource] = []
        for planned in self._representatives():
            computation = await self._service._read_checkpoint(planned)
            if computation is None:
                misses.append(planned)
            else:
                self._computations[planned.computation_key] = computation

        # CORE (FULL) work is planned first, so a blocking failure stops the
        # stage before any complementary source is sent to a provider.  A
        # proven pre-submission failure only defers its own source: the others
        # still reach a durable checkpoint before the stage is retried.
        deferred: list[ModelGatewayError] = []
        singles, batches = self._units(misses)
        for planned in singles:
            await _defer_transient(self._compute_single(planned), deferred)
        for batch in batches:
            try:
                result = await self._compute_batch(batch)
            except ModelGatewayError as exc:
                if not exc.retryable:
                    raise
                deferred.append(exc)
                continue
            self._warnings.extend(result.warnings)
            for document_id, computation in result.computations.items():
                source = next(item for item in batch if item.source_document_id == document_id)
                self._computations[source.computation_key] = computation
            for planned in result.retry_individually:
                await _defer_transient(self._compute_single(planned), deferred)
        if deferred:
            raise deferred[0]

    def _representatives(self) -> list[PlannedExtractionSource]:
        """The first source of every computation key still worth computing."""

        seen: set[tuple[str, ExtractionProfile]] = set()
        representatives: list[PlannedExtractionSource] = []
        for planned in self._plan.sources:
            if planned.source_document_id in self._failures or planned.computation_key in seen:
                continue
            seen.add(planned.computation_key)
            representatives.append(planned)
        return representatives

    def _units(
        self, misses: Sequence[PlannedExtractionSource]
    ) -> tuple[list[PlannedExtractionSource], list[tuple[PlannedExtractionSource, ...]]]:
        """FULL is always individual; small IOC_RULES captures may be batched.

        A batch only groups captures governed by the same diffusion policy, so
        no capture ever reaches a provider its own policy forbids.
        """

        batchable: dict[SourceAccessPolicy, list[PlannedExtractionSource]] = {}
        singles: list[PlannedExtractionSource] = []
        for planned in misses:
            if (
                self._service._ioc_rules_batching
                and planned.profile is ExtractionProfile.IOC_RULES
                and len(self._evidence[planned.source_document_id].parsed_text)
                <= BATCH_SOURCE_MAX_CHARS
            ):
                policy = self._archives[planned.source_document_id].policy
                batchable.setdefault(policy, []).append(planned)
            else:
                singles.append(planned)
        batches: list[tuple[PlannedExtractionSource, ...]] = []
        for group in batchable.values():
            by_id = {planned.source_document_id: planned for planned in group}
            for partition in partition_archive_q2_batch_sources(
                tuple(
                    ArchiveQ2BatchSource(
                        source_document_id=planned.source_document_id,
                        content_sha256=planned.content_sha256,
                    )
                    for planned in group
                ),
                text_lengths={
                    planned.source_document_id: len(
                        self._evidence[planned.source_document_id].parsed_text
                    )
                    for planned in group
                },
                max_total_chars=BATCH_TOTAL_MAX_CHARS,
            ):
                members = tuple(by_id[entry.source_document_id] for entry in partition)
                if len(members) < 2:
                    singles.extend(members)
                else:
                    batches.append(members)
        singles.sort(key=lambda planned: planned.position)
        batches.sort(key=lambda batch: batch[0].position)
        return singles, batches

    async def _call(self, request: ModelRequest, schema: type[BaseModel]) -> ModelExecution:
        if self._before_model_call is not None:
            await self._before_model_call()
        self._model_calls += 1
        return await self._service.invoke(request, schema)

    async def _compute_single(self, planned: PlannedExtractionSource) -> None:
        archive = self._archives[planned.source_document_id]
        chunks = archived_source_chunks(self._evidence[planned.source_document_id].parsed_text)
        if len(chunks) > MAX_CHUNKS_PER_SOURCE:
            self._fail(
                planned,
                SourceExtractionFailure(
                    ExtractionFailureCode.SOURCE_TEXT_TOO_LARGE,
                    "The archived source exceeds the chunking limit",
                ),
            )
            return
        prompt_version = source_prompt_version(planned.profile)
        outputs: list[Q2SourceOutput] = []
        raw_texts: list[str] = []
        model_run_id: UUID | None = None
        try:
            for index, chunk in enumerate(chunks, start=1):
                request = self._service.model_request(
                    run=self._run,
                    profile=planned.profile,
                    policy=archive.policy,
                    prompt=ProductionPromptTemplates.get_canonical_archive_extraction_prompt(
                        chunk, profile=planned.profile
                    ),
                    prompt_template_id=_SOURCE_EXTRACTION_PROMPT_TEMPLATE_ID,
                    prompt_version=prompt_version,
                    unit_identity={
                        "kind": "source",
                        "source_content_sha256": planned.content_sha256,
                        "profile": planned.profile.value,
                        "prompt_version": prompt_version,
                        "source_text_contract_version": source_text_contract_version(),
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                    },
                    metadata={
                        "tier": planned.tier.value,
                        "source_content_sha256": planned.content_sha256,
                        "chunk_index": index,
                        "chunk_count": len(chunks),
                    },
                )
                execution = await self._call(request, Q2SourceOutput)
                output = cast(Q2SourceOutput, execution.structured_output)
                outputs.append(output)
                raw_texts.append(
                    json.dumps(output.model_dump(mode="json"), sort_keys=True, ensure_ascii=False)
                )
                model_run_id = execution.run.id if execution.run is not None else model_run_id
        except SourceExtractionFailure as failure:
            self._fail(planned, failure)
            return
        merged = merge_q2_source_outputs(outputs)
        checkpoint_id = await self._service._persist_checkpoint(
            planned,
            output=merged,
            raw_text="\n\n".join(raw_texts),
            model_run_id=model_run_id,
            prompt_version=prompt_version,
        )
        self._computations[planned.computation_key] = _Computation(
            output=merged, checkpoint_id=checkpoint_id, model_run_id=model_run_id, fresh=True
        )

    async def _compute_batch(self, sources: tuple[PlannedExtractionSource, ...]) -> _BatchResult:
        entries = make_archive_q2_batch(
            tuple(
                ArchiveQ2BatchSource(
                    source_document_id=source.source_document_id,
                    content_sha256=source.content_sha256,
                )
                for source in sources
            )
        )
        by_id = {source.source_document_id: source for source in sources}
        batch_identity = archive_q2_batch_identity(entries)
        request = self._service.model_request(
            run=self._run,
            profile=ExtractionProfile.IOC_RULES,
            policy=self._archives[sources[0].source_document_id].policy,
            prompt=ProductionPromptTemplates.get_canonical_archive_batch_prompt(
                tuple(
                    (entry.batch_id, self._evidence[entry.source_document_id].parsed_text)
                    for entry in entries
                )
            ),
            prompt_template_id=_SOURCE_EXTRACTION_BATCH_PROMPT_TEMPLATE_ID,
            prompt_version=CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
            unit_identity={
                "kind": "batch",
                "batch_identity": batch_identity,
                "prompt_version": CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
                "source_text_contract_version": source_text_contract_version(),
            },
            metadata={
                "batch_identity": batch_identity,
                "batch_sources": [
                    {"batch_id": entry.batch_id, "source_content_sha256": entry.content_sha256}
                    for entry in entries
                ],
            },
        )
        try:
            execution = await self._call(request, Q2BatchResponse)
        except SourceExtractionFailure as failure:
            # A batch failure is never attributed to one publication: every
            # capture is retried individually, where its own outcome is known.
            return _BatchResult(
                computations={},
                retry_individually=sources,
                warnings=(f"extraction_batch_failed:{failure.code}",),
            )
        model_run_id = execution.run.id if execution.run is not None else None
        attribution = attribute_q2_batch_response(
            cast(Q2BatchResponse, execution.structured_output),
            {entry.batch_id: entry for entry in entries},
        )
        computations: dict[UUID, _Computation] = {}
        retry: list[PlannedExtractionSource] = []
        warnings = list(attribution.warnings)
        for entry in entries:
            planned = by_id[entry.source_document_id]
            result = attribution.result_for(entry.batch_id)
            if result.output is None:
                # Unattributable content is rejected, never redistributed.
                warnings.append(
                    f"{ExtractionFailureCode.BATCH_SOURCE_UNATTRIBUTED.value}:{result.error_code}"
                )
                retry.append(planned)
                continue
            output = project_q2_source_output(result.output, ExtractionProfile.IOC_RULES)
            checkpoint_id = await self._service._persist_checkpoint(
                planned,
                output=output,
                raw_text=result.raw_block,
                model_run_id=model_run_id,
                prompt_version=CANONICAL_IOC_RULES_BATCH_PROMPT_VERSION,
            )
            computations[planned.source_document_id] = _Computation(
                output=output, checkpoint_id=checkpoint_id, model_run_id=model_run_id, fresh=True
            )
        return _BatchResult(
            computations=computations,
            retry_individually=tuple(retry),
            warnings=tuple(warnings),
        )

    def _assemble(self) -> ProductionExtractionExecution:
        sources: list[ProductionSourceExtractionV1] = []
        rejections: list[ExtractionRejection] = []
        failed: list[tuple[PlannedExtractionSource, str]] = []
        warnings = list(self._warnings)
        representatives_done: set[tuple[str, ExtractionProfile]] = set()
        for planned in self._plan.sources:
            failure = self._failures.get(planned.source_document_id)
            computation = self._computations.get(planned.computation_key)
            if failure is not None or computation is None:
                failure = failure or SourceExtractionFailure(
                    ExtractionFailureCode.MODEL_CALL_FAILED,
                    "The source produced no extraction",
                )
                if planned.tier is ProductionReferenceTier.CORE:
                    return self._stopped(
                        ExtractionExecutionStatus.BLOCKED, _CoreSourceFailed(planned, failure).error
                    )
                code = failure.code
                failed.append((planned, code))
                warnings.append(f"extraction_source_skipped:{planned.canonical_url}:{code}")
                continue
            if not computation.fresh:
                reuse_state = ExtractionReuseState.REUSED
            elif planned.computation_key in representatives_done:
                reuse_state = ExtractionReuseState.CONTENT_DUPLICATE
            else:
                reuse_state = ExtractionReuseState.FRESH
            representatives_done.add(planned.computation_key)
            gated, gate_warnings, gate_rejections = gate_source_output(
                computation.output,
                self._evidence[planned.source_document_id],
                profile=planned.profile,
            )
            warnings.extend(gate_warnings)
            rejections.extend(
                ExtractionRejection(
                    source=planned, model_run_id=computation.model_run_id, rejection=rejection
                )
                for rejection in gate_rejections
                if rejection.proposal_kind in {"artifact", "rule"}
            )
            sources.append(
                build_canonical_source_extraction(
                    planned=planned,
                    gated=gated,
                    checkpoint_id=computation.checkpoint_id,
                    reuse_state=reuse_state,
                )
            )
        extraction = build_production_extraction(
            plan=self._plan,
            sources=sources,
            failed_sources=failed,
            warnings=warnings,
        )
        return ProductionExtractionExecution(
            status=ExtractionExecutionStatus.SUCCEEDED,
            plan=self._plan,
            extraction=extraction,
            rejections=tuple(rejections),
            model_calls=self._model_calls,
            details=production_extraction_metadata(extraction),
        )


async def _defer_transient(work: Awaitable[None], deferred: list[ModelGatewayError]) -> None:
    """Run one unit, keeping a proven pre-submission failure for the end."""

    try:
        await work
    except ModelGatewayError as exc:
        if not exc.retryable:
            raise
        deferred.append(exc)


class _CoreSourceFailed(Exception):
    def __init__(self, planned: PlannedExtractionSource, failure: SourceExtractionFailure) -> None:
        super().__init__(str(failure))
        self.error = SourceExtractionFailure(
            ExtractionFailureCode.CORE_SOURCE_FAILED,
            f"The CORE source {planned.canonical_url} has no verified FULL extraction",
            details={
                "source_document_id": str(planned.source_document_id),
                "canonical_url": planned.canonical_url,
                "source_failure_code": failure.code,
                **failure.details,
            },
        )


# --- Legacy compatibility boundary ----------------------------------------


def project_legacy_technical_extraction(
    extraction: ProductionExtractionV1,
    *,
    source_labels: Mapping[str, str] | None = None,
) -> TechnicalExtraction:
    """Project the canonical contract onto the legacy TechnicalExtraction.

    This is the single one-way compatibility boundary: ``ProductionExtractionV1``
    feeds the current Synthesis, QA, Assembly and Repair Desk consumers.
    Reconstructing the canonical contract from a legacy extraction is
    deliberately impossible.

    ``source_labels`` maps a canonical URL to the label the legacy REFERENCES
    projection gives that source ("S1"), because those consumers correlate
    extraction items with the legacy REFERENCES report.  Without it, a source is
    labelled by its ``source_document_id``.
    """

    labels = source_labels or {}
    items: list[ExtractionItem] = []
    rules: list[DetectionRule] = []
    uncertainties: list[str] = []
    for source in extraction.sources:
        source_id = labels.get(source.canonical_url) or str(source.source_document_id)
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
                normalized: str | None = normalize_indicator_value(
                    indicator.value, indicator.artifact_type
                )
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


def _semantic_type_for_fact(category: str) -> SemanticType:
    return _SEMANTIC_TYPE_BY_FACT_CATEGORY.get(category, SemanticType.OTHER)


@dataclass(frozen=True, slots=True)
class ExtractionCompatibilityView:
    """One EXTRACTION payload decoded through the single compatibility boundary.

    ``legacy`` is the ``TechnicalExtraction`` view every not-yet-migrated
    consumer reads: a ``ProductionExtractionV1`` payload is projected one way.
    Only an imported production state (state transfer V4) still carries a
    ``TechnicalExtraction`` payload; it is read as such and never promoted.
    ``canonical`` is the loaded ``ProductionExtractionV1``, or ``None`` for such
    an imported payload.
    """

    legacy: TechnicalExtraction
    canonical: ProductionExtractionV1 | None


def extraction_compatibility_view(
    payload: Mapping[str, Any], *, source_labels: Mapping[str, str] | None = None
) -> ExtractionCompatibilityView:
    """Decode one EXTRACTION payload through the compatibility boundary."""

    if "schema_version" in payload and "sources" in payload:
        canonical = production_extraction_from_json(payload)
        return ExtractionCompatibilityView(
            legacy=project_legacy_technical_extraction(canonical, source_labels=source_labels),
            canonical=canonical,
        )
    return ExtractionCompatibilityView(
        legacy=technical_extraction_from_json(dict(payload)),
        canonical=None,
    )


def legacy_technical_extraction_from_payload(
    payload: Mapping[str, Any], *, source_labels: Mapping[str, str] | None = None
) -> TechnicalExtraction:
    """Return only the legacy view of one EXTRACTION payload."""

    return extraction_compatibility_view(payload, source_labels=source_labels).legacy
