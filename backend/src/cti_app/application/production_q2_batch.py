"""Batching primitives for URL-only IOC_RULES Q2 extraction.

The objects in this module deliberately carry the Q1 source only as local
orchestration state.  The model-facing identifiers are the exact canonical URL
and the per-batch ``B#`` label; no Q1 source id is put in the batch wire format.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.production_parsers import (
    Q2_MARKDOWN_PARSER_VERSION,
    ParsedSource,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
)
from cti_app.application.production_prompts import (
    IOC_RULES_BATCH_PROMPT_VERSION,
    Q2_BATCH_OUTPUT_MARKER,
)
from cti_app.domain.model_runs import ModelProvider
from cti_app.domain.production import ExtractionProfile

# Un lot large rate plus de sources : le modèle en omet, et chaque omission
# repart en appel individuel. Quatre sources est le meilleur compromis observé
# entre nombre d'appels et taux d'omission.
MAX_Q2_BATCH_SOURCES = 4
# "q2-batch-v3": source blocks are delimited by minimal Q2 markers. The
# framing scanner is intentionally independent from Markdown fence state, so a
# malformed rule fence in one block can no longer swallow the next block.
Q2_BATCH_PARSER_VERSION = "q2-batch-v3"

_HTTP_URL = re.compile(r"^https?://\S+$", re.IGNORECASE)
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BATCH_ID = re.compile(r"^B(?P<number>[0-9]+)$", re.IGNORECASE)
_Q2_BATCH_MARKER = re.compile(r"^\s*@@Q2:B(?P<number>[0-9]+)@@\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Q2BatchCandidate:
    """One Q1 source eligible for IOC_RULES batching.

    Eligibility is decided by the source itself: the batch sends the exact
    canonical URL, so an HTTP(S) URL is the only content requirement.
    """

    source: ParsedSource

    def __post_init__(self) -> None:
        if not _HTTP_URL.fullmatch(self.source.canonical_url or ""):
            raise ValueError("A batch candidate needs an HTTP(S) canonical URL")

    @property
    def canonical_url(self) -> str:
        return self.source.canonical_url


@dataclass(frozen=True, slots=True)
class Q2BatchSource:
    """A candidate with the local B# label used by one model response."""

    batch_id: str
    candidate: Q2BatchCandidate

    @property
    def source(self) -> ParsedSource:
        return self.candidate.source

    @property
    def canonical_url(self) -> str:
        return self.candidate.canonical_url


@dataclass(frozen=True, slots=True)
class Q2Batch:
    """One deterministic greedy batch, in ReferenceReport order."""

    sources: tuple[Q2BatchSource, ...]

    @property
    def source_mapping(self) -> Mapping[str, ParsedSource]:
        return {item.batch_id: item.source for item in self.sources}

    @property
    def canonical_urls(self) -> tuple[str, ...]:
        return tuple(item.canonical_url for item in self.sources)


@dataclass(frozen=True, slots=True)
class Q2BatchSourceResult:
    """One source-local interpretation of a batch response block."""

    batch_id: str
    output: Q2SourceOutput | None
    status: str
    error_code: str | None = None
    errors: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    raw_block: str = ""

    @property
    def usable(self) -> bool:
        return self.output is not None and self.status == "succeeded"


@dataclass(frozen=True, slots=True)
class Q2BatchParseResult:
    """Marker-delimited batch parsing result."""

    sources: tuple[Q2BatchSourceResult, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    @property
    def usable(self) -> bool:
        return not self.errors


def partition_q2_batch_candidates(
    candidates: Sequence[Q2BatchCandidate],
) -> tuple[tuple[Q2BatchCandidate, ...], ...]:
    """Partition candidates in ReferenceReport order, never dropping one."""

    batches: list[tuple[Q2BatchCandidate, ...]] = []
    current: list[Q2BatchCandidate] = []
    for candidate in candidates:
        if len(current) >= MAX_Q2_BATCH_SOURCES:
            batches.append(tuple(current))
            current = []
        current.append(candidate)
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def make_q2_batch(candidates: Sequence[Q2BatchCandidate]) -> Q2Batch:
    """Assign deterministic local B1..Bn identifiers to one batch."""

    if not 2 <= len(candidates) <= MAX_Q2_BATCH_SOURCES:
        raise ValueError(f"A Q2 batch must contain between 2 and {MAX_Q2_BATCH_SOURCES} sources")
    return Q2Batch(
        sources=tuple(
            Q2BatchSource(batch_id=f"B{index}", candidate=candidate)
            for index, candidate in enumerate(candidates, start=1)
        )
    )


def q2_batch_model_run_id(
    *,
    production_run_id: UUID,
    pipeline_generation: int,
    canonical_urls: Sequence[str],
    routing_policy_version: str,
    provider: ModelProvider = ModelProvider.OPENAI,
    ioc_rules_batch_prompt_version: str | None = None,
    q2_markdown_parser_version: str | None = None,
    q2_batch_parser_version: str | None = None,
) -> UUID:
    """Return the identity of one exact batch of web readings, scoped to a run.

    A Q2 batch reads live publications, so it is not a pure function of any
    content we hold: the identity is scoped to the production run that decided
    the work.  A retry of the same run reuses the same ModelRun; a new
    production reads the web again.  Versions that cannot change what this
    batch does — the Q1 parser, the single-source prompt, the archive-only
    source-evidence and SourceExtraction versions — are deliberately excluded.
    """

    urls = tuple(canonical_urls)
    if not urls or any(not _HTTP_URL.fullmatch(url or "") for url in urls):
        raise ValueError("canonical_urls must contain HTTP(S) URLs")
    identity = json.dumps(
        {
            "production_run_id": str(production_run_id),
            "pipeline_generation": pipeline_generation,
            "ordered_canonical_urls": urls,
            "ioc_rules_batch_prompt_version": (
                ioc_rules_batch_prompt_version or IOC_RULES_BATCH_PROMPT_VERSION
            ),
            "q2_markdown_parser_version": (
                q2_markdown_parser_version or Q2_MARKDOWN_PARSER_VERSION
            ),
            "q2_batch_parser_version": q2_batch_parser_version or Q2_BATCH_PARSER_VERSION,
            "q2_routing_policy_version": routing_policy_version,
            "provider": provider.value,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return uuid5(NAMESPACE_URL, f"production-q2-ioc-batch:{identity}")


def _normalize_batch_id(batch_id: str) -> str:
    match = _BATCH_ID.fullmatch(batch_id.strip())
    if match is None:
        raise ValueError("batch_id must match B<number>")
    return f"B{int(match.group('number'))}"


def q2_batch_output_marker(batch_id: str) -> str:
    """Return the exact output marker for one normalized local batch id."""

    return Q2_BATCH_OUTPUT_MARKER.format(batch_id=_normalize_batch_id(batch_id))


def _split_batch_blocks(text: str) -> tuple[list[tuple[str, str]], list[str]]:
    """Split Q2 marker-delimited blocks without looking at Markdown fences."""

    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    blocks: list[tuple[str, str]] = []
    warnings: list[str] = []
    current_id: str | None = None
    current_lines: list[str] = []

    def close() -> None:
        nonlocal current_id, current_lines
        if current_id is not None:
            blocks.append((current_id, "\n".join(current_lines).strip()))
        current_id = None
        current_lines = []

    for line in lines:
        found = _Q2_BATCH_MARKER.fullmatch(line)
        if found is not None:
            close()
            current_id = f"B{int(found.group('number'))}"
            current_lines = []
            continue
        if current_id is None:
            if line.strip():
                warnings.append("batch_text_outside_source")
            continue
        current_lines.append(line)
    if current_id is not None:
        close()
    return blocks, list(dict.fromkeys(warnings))


def parse_q2_batch_response(
    text: str,
    expected_sources: Mapping[str, ParsedSource] | Sequence[Q2BatchSource],
) -> Q2BatchParseResult:
    """Parse source blocks while keeping malformed blocks source-local."""

    expected = (
        {_normalize_batch_id(item.batch_id): item.source for item in expected_sources}
        if not isinstance(expected_sources, Mapping)
        else {
            _normalize_batch_id(batch_id): source for batch_id, source in expected_sources.items()
        }
    )
    blocks, warnings = _split_batch_blocks(text)
    occurrences: dict[str, list[str]] = {}
    for batch_id, body in blocks:
        occurrences.setdefault(batch_id, []).append(body)

    results: list[Q2BatchSourceResult] = []
    recognized_expected = 0
    for batch_id, entries in occurrences.items():
        if batch_id not in expected:
            warnings.append("batch_source_unknown")
            continue
        recognized_expected += 1
        if len(entries) > 1:
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=None,
                    status="failed",
                    error_code="batch_source_duplicate",
                    raw_block="\n\n---\n\n".join(entries),
                )
            )
            continue
        body = entries[0]
        normalized = body.strip()
        if not normalized:
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=None,
                    status="failed",
                    error_code="batch_source_invalid",
                    raw_block=body,
                )
            )
            continue
        if normalized.casefold() == "empty":
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=Q2SourceOutput(),
                    status="succeeded",
                    raw_block=body,
                )
            )
            continue
        if normalized.casefold() == "unavailable":
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=None,
                    status="failed",
                    error_code="batch_source_unavailable",
                    raw_block=body,
                )
            )
            continue

        parsed = parse_q2_proposals_markdown(body)
        if parsed.usable and parsed.value is not None:
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=parsed.value,
                    status="succeeded",
                    errors=tuple(parsed.errors),
                    warnings=tuple(parsed.warnings),
                    raw_block=body,
                )
            )
        else:
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=None,
                    status="failed",
                    error_code="batch_source_invalid",
                    errors=tuple(parsed.errors),
                    warnings=tuple(parsed.warnings),
                    raw_block=body,
                )
            )

    result_by_id = {result.batch_id: result for result in results}
    for batch_id in expected:
        if batch_id not in result_by_id:
            result_by_id[batch_id] = Q2BatchSourceResult(
                batch_id=batch_id,
                output=None,
                status="failed",
                error_code="batch_source_missing",
            )

    ordered_results = tuple(result_by_id[batch_id] for batch_id in expected)
    if recognized_expected == 0:
        return Q2BatchParseResult(
            sources=ordered_results,
            warnings=tuple(dict.fromkeys(warnings)),
            errors=("batch_response_failure",),
        )
    return Q2BatchParseResult(
        sources=ordered_results,
        warnings=tuple(dict.fromkeys(warnings)),
    )


# --- AW-011 archive-backed batches ----------------------------------------


class Q2BatchSourceOutput(BaseModel):
    """One source-local output inside a structured batch answer.

    ``batch_id`` is the temporary local handle rendered by the prompt. It maps
    one answer entry to one expected capture and never becomes a canonical
    source identity.
    """

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=16)
    output: Q2SourceOutput


class Q2BatchResponse(BaseModel):
    """The structured AW-011 batch contract: one entry per analysed capture."""

    model_config = ConfigDict(extra="forbid")

    sources: list[Q2BatchSourceOutput] = Field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ArchiveQ2BatchSource:
    """One archived capture inside a deterministic IOC_RULES batch.

    The batch carries content identities only: no Q1 source id, no URL and no
    local handle before the batch is formed.  ``batch_id`` stays empty until
    ``make_archive_q2_batch`` assigns the temporary ``B#`` handles.
    """

    source_document_id: UUID
    content_sha256: str
    profile: ExtractionProfile = ExtractionProfile.IOC_RULES
    batch_id: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.source_document_id, UUID):
            raise ValueError("A batch source requires a document identity")
        if (
            not isinstance(self.content_sha256, str)
            or _SHA256.fullmatch(self.content_sha256) is None
        ):
            raise ValueError("A batch source requires a lowercase SHA-256")
        if self.profile is not ExtractionProfile.IOC_RULES:
            raise ValueError("Archive-backed batches only carry the IOC_RULES profile")
        if self.batch_id:
            object.__setattr__(self, "batch_id", _normalize_batch_id(self.batch_id))


@dataclass(frozen=True, slots=True)
class ArchiveQ2BatchAttribution:
    """One attribution of a structured batch answer to its expected sources."""

    results: tuple[Q2BatchSourceResult, ...]
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()

    def result_for(self, batch_id: str) -> Q2BatchSourceResult:
        wanted = _normalize_batch_id(batch_id)
        for result in self.results:
            if result.batch_id == wanted:
                return result
        raise KeyError(wanted)

    @property
    def usable(self) -> bool:
        return not self.errors


def partition_archive_q2_batch_sources(
    entries: Sequence[ArchiveQ2BatchSource],
    *,
    text_lengths: Mapping[UUID, int] | None = None,
    max_total_chars: int | None = None,
) -> tuple[tuple[ArchiveQ2BatchSource, ...], ...]:
    """Partition captures in the supplied (canonical) order, never dropping one.

    ``MAX_Q2_BATCH_SOURCES`` bounds every batch. When ``text_lengths`` and
    ``max_total_chars`` are supplied, a batch also stops before its bounded
    model input would exceed the provider-agnostic character budget, so the
    partition depends only on the archived content and the fixed limits.
    """

    batches: list[tuple[ArchiveQ2BatchSource, ...]] = []
    current: list[ArchiveQ2BatchSource] = []
    current_chars = 0
    for entry in entries:
        length = 0
        if text_lengths is not None:
            length = max(0, text_lengths.get(entry.source_document_id, 0))
        exceeds_chars = (
            max_total_chars is not None
            and bool(current)
            and current_chars + length > max_total_chars
        )
        if len(current) >= MAX_Q2_BATCH_SOURCES or exceeds_chars:
            batches.append(tuple(current))
            current = []
            current_chars = 0
        current.append(entry)
        current_chars += length
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def make_archive_q2_batch(
    entries: Sequence[ArchiveQ2BatchSource],
) -> tuple[ArchiveQ2BatchSource, ...]:
    """Assign deterministic local B1..Bn handles to one archive-backed batch."""

    if not 2 <= len(entries) <= MAX_Q2_BATCH_SOURCES:
        raise ValueError(f"A Q2 batch must contain between 2 and {MAX_Q2_BATCH_SOURCES} sources")
    return tuple(
        ArchiveQ2BatchSource(
            source_document_id=entry.source_document_id,
            content_sha256=entry.content_sha256,
            profile=entry.profile,
            batch_id=f"B{index}",
        )
        for index, entry in enumerate(entries, start=1)
    )


def archive_q2_batch_identity(entries: Sequence[ArchiveQ2BatchSource]) -> str:
    """Return the content-addressed identity of one deterministic batch.

    The identity covers the ordered handles and content identities only, so it
    is independent of the Subject, run and job that decided the work.
    """

    payload = [
        {
            "batch_id": entry.batch_id,
            "source_document_id": str(entry.source_document_id),
            "content_sha256": entry.content_sha256,
            "profile": entry.profile.value,
        }
        for entry in entries
    ]
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def attribute_q2_batch_response(
    response: Q2BatchResponse,
    expected_sources: Mapping[str, ArchiveQ2BatchSource],
) -> ArchiveQ2BatchAttribution:
    """Map a structured batch answer to its sources without heuristic repair.

    An entry is usable only when its handle matches exactly one expected
    capture. A duplicated, unknown or missing handle stays source-local: the
    caller re-processes the affected capture individually and no indicator is
    ever moved from one publication to another.
    """

    expected = {
        _normalize_batch_id(batch_id): entry for batch_id, entry in expected_sources.items()
    }
    warnings: list[str] = []
    occurrences: dict[str, list[Q2BatchSourceOutput]] = {}
    for entry in response.sources:
        try:
            normalized = _normalize_batch_id(entry.batch_id)
        except ValueError:
            # A malformed handle is unattributable, exactly like an unknown one.
            warnings.append("batch_source_unknown")
            continue
        if normalized not in expected:
            warnings.append("batch_source_unknown")
            continue
        occurrences.setdefault(normalized, []).append(entry)

    results: list[Q2BatchSourceResult] = []
    for batch_id in sorted(expected, key=lambda value: int(value[1:])):
        entries = occurrences.get(batch_id, [])
        if not entries:
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=None,
                    status="failed",
                    error_code="batch_source_missing",
                )
            )
            continue
        if len(entries) > 1:
            results.append(
                Q2BatchSourceResult(
                    batch_id=batch_id,
                    output=None,
                    status="failed",
                    error_code="batch_source_duplicate",
                    raw_block=json.dumps(
                        [entry.model_dump(mode="json") for entry in entries],
                        sort_keys=True,
                    )[:4000],
                )
            )
            continue
        entry = entries[0]
        results.append(
            Q2BatchSourceResult(
                batch_id=batch_id,
                output=entry.output,
                status="succeeded",
                raw_block=json.dumps(entry.model_dump(mode="json"), sort_keys=True)[:4000],
            )
        )
    return ArchiveQ2BatchAttribution(
        results=tuple(results),
        warnings=tuple(dict.fromkeys(warnings)),
    )


__all__ = [
    "MAX_Q2_BATCH_SOURCES",
    "Q2_BATCH_PARSER_VERSION",
    "ArchiveQ2BatchAttribution",
    "ArchiveQ2BatchSource",
    "Q2Batch",
    "Q2BatchCandidate",
    "Q2BatchParseResult",
    "Q2BatchResponse",
    "Q2BatchSource",
    "Q2BatchSourceOutput",
    "Q2BatchSourceResult",
    "archive_q2_batch_identity",
    "attribute_q2_batch_response",
    "make_archive_q2_batch",
    "make_q2_batch",
    "parse_q2_batch_response",
    "partition_archive_q2_batch_sources",
    "partition_q2_batch_candidates",
    "q2_batch_model_run_id",
    "q2_batch_output_marker",
]
