"""Batching primitives for archived IOC_RULES Q2 extraction.

The objects in this module deliberately carry the Q1 source only as local
orchestration state.  The model-facing identifier is the per-batch ``B#``
label; no Q1 source id is put in the batch wire format.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.production_artifact_verification import ARTIFACT_VERIFIER_VERSION
from cti_app.application.production_parsers import (
    Q2_EXTRACTION_CONTRACT_VERSION,
    Q2_MARKDOWN_PARSER_VERSION,
    ParsedSource,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
)
from cti_app.application.production_prompts import (
    IOC_RULES_BATCH_PROMPT_VERSION,
    Q2_BATCH_INPUT_MARKER,
    Q2_BATCH_OUTPUT_MARKER,
)
from cti_app.application.production_source_evidence import SOURCE_EVIDENCE_VERSION
from cti_app.domain.model_runs import ModelProvider

MAX_Q2_BATCH_ARCHIVED_CHARS = 70_000
MAX_Q2_BATCH_SOURCES = 8
# "q2-batch-v3": source blocks are delimited by minimal Q2 markers. The
# framing scanner is intentionally independent from Markdown fence state, so a
# malformed rule fence in one block can no longer swallow the next block.
Q2_BATCH_PARSER_VERSION = "q2-batch-v3"

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_BATCH_ID = re.compile(r"^B(?P<number>[0-9]+)$", re.IGNORECASE)
_Q2_BATCH_MARKER = re.compile(r"^\s*@@Q2:B(?P<number>[0-9]+)@@\s*$", re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class Q2BatchCandidate:
    """One exact archived source eligible for IOC_RULES batching."""

    source: ParsedSource
    archived_text: str
    source_content_sha256: str

    def __post_init__(self) -> None:
        if not _SHA256.fullmatch(self.source_content_sha256):
            raise ValueError("source_content_sha256 must be a lowercase SHA-256")
        if not self.archived_text:
            raise ValueError("archived_text must not be empty")


@dataclass(frozen=True, slots=True)
class Q2BatchSource:
    """A candidate with the local B# label used by one model response."""

    batch_id: str
    candidate: Q2BatchCandidate

    @property
    def source(self) -> ParsedSource:
        return self.candidate.source

    @property
    def archived_text(self) -> str:
        return self.candidate.archived_text

    @property
    def source_content_sha256(self) -> str:
        return self.candidate.source_content_sha256


@dataclass(frozen=True, slots=True)
class Q2Batch:
    """One deterministic greedy batch, in ReferenceReport order."""

    sources: tuple[Q2BatchSource, ...]

    @property
    def source_mapping(self) -> Mapping[str, ParsedSource]:
        return {item.batch_id: item.source for item in self.sources}

    @property
    def archived_chars(self) -> int:
        return sum(len(item.archived_text) for item in self.sources)


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
    """Greedily partition candidates without truncating any source."""

    batches: list[tuple[Q2BatchCandidate, ...]] = []
    current: list[Q2BatchCandidate] = []
    current_chars = 0
    for candidate in candidates:
        candidate_chars = len(candidate.archived_text)
        if candidate_chars > MAX_Q2_BATCH_ARCHIVED_CHARS:
            raise ValueError("A batch candidate exceeds the batch character budget")
        if current and (
            len(current) >= MAX_Q2_BATCH_SOURCES
            or current_chars + candidate_chars > MAX_Q2_BATCH_ARCHIVED_CHARS
        ):
            batches.append(tuple(current))
            current = []
            current_chars = 0
        current.append(candidate)
        current_chars += candidate_chars
    if current:
        batches.append(tuple(current))
    return tuple(batches)


def make_q2_batch(candidates: Sequence[Q2BatchCandidate]) -> Q2Batch:
    """Assign deterministic local B1..Bn identifiers to one batch."""

    if not 2 <= len(candidates) <= MAX_Q2_BATCH_SOURCES:
        raise ValueError("A Q2 batch must contain between 2 and 8 sources")
    if sum(len(candidate.archived_text) for candidate in candidates) > MAX_Q2_BATCH_ARCHIVED_CHARS:
        raise ValueError("Q2 batch exceeds the archived character budget")
    return Q2Batch(
        sources=tuple(
            Q2BatchSource(batch_id=f"B{index}", candidate=candidate)
            for index, candidate in enumerate(candidates, start=1)
        )
    )


def q2_batch_model_run_id(
    *,
    source_content_sha256: Sequence[str],
    routing_policy_version: str,
    provider: ModelProvider = ModelProvider.OPENAI,
    extraction_contract_version: str | None = None,
    ioc_rules_batch_prompt_version: str | None = None,
    q2_markdown_parser_version: str | None = None,
    q2_batch_parser_version: str | None = None,
    source_evidence_version: str | None = None,
    artifact_verifier_version: str | None = None,
) -> UUID:
    """Return a content/version-addressed identity for one exact Q2 batch.

    Only versions that can change what this batch actually does belong here.
    The Q1 ``PARSER_VERSION`` and the single-source ``IOC_RULES_PROMPT_VERSION``
    are deliberately excluded: neither changes the batch prompt, the batch wire
    format or the batch parser, so neither may invalidate a reusable batch.
    """

    hashes = tuple(source_content_sha256)
    if not hashes or any(not _SHA256.fullmatch(value) for value in hashes):
        raise ValueError("source_content_sha256 must contain lowercase SHA-256 values")
    identity = json.dumps(
        {
            "ordered_source_content_sha256": hashes,
            "q2_extraction_contract_version": (
                extraction_contract_version or Q2_EXTRACTION_CONTRACT_VERSION
            ),
            "ioc_rules_batch_prompt_version": (
                ioc_rules_batch_prompt_version or IOC_RULES_BATCH_PROMPT_VERSION
            ),
            "q2_markdown_parser_version": (
                q2_markdown_parser_version or Q2_MARKDOWN_PARSER_VERSION
            ),
            "q2_batch_parser_version": q2_batch_parser_version or Q2_BATCH_PARSER_VERSION,
            "source_evidence_version": source_evidence_version or SOURCE_EVIDENCE_VERSION,
            "artifact_verifier_version": artifact_verifier_version or ARTIFACT_VERIFIER_VERSION,
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


def q2_batch_input_marker(batch_id: str) -> str:
    """Return the exact input marker for one normalized local batch id."""

    return Q2_BATCH_INPUT_MARKER.format(batch_id=_normalize_batch_id(batch_id))


def q2_batch_framing_markers(batch_ids: Iterable[str]) -> tuple[str, ...]:
    """Return every exact Q2/Q2IN marker that structures a batch."""

    normalized_ids = tuple(_normalize_batch_id(batch_id) for batch_id in batch_ids)
    return tuple(q2_batch_output_marker(batch_id) for batch_id in normalized_ids) + tuple(
        q2_batch_input_marker(batch_id) for batch_id in normalized_ids
    )


def q2_batch_framing_collides(batch_ids: Iterable[str], archived_texts: Iterable[str]) -> bool:
    """Report whether an archive already contains an exact batch marker."""

    markers = q2_batch_framing_markers(batch_ids)
    return any(marker in text for text in archived_texts for marker in markers)


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


__all__ = [
    "MAX_Q2_BATCH_ARCHIVED_CHARS",
    "MAX_Q2_BATCH_SOURCES",
    "Q2_BATCH_PARSER_VERSION",
    "Q2Batch",
    "Q2BatchCandidate",
    "Q2BatchParseResult",
    "Q2BatchSource",
    "Q2BatchSourceResult",
    "make_q2_batch",
    "parse_q2_batch_response",
    "partition_q2_batch_candidates",
    "q2_batch_framing_collides",
    "q2_batch_framing_markers",
    "q2_batch_input_marker",
    "q2_batch_model_run_id",
    "q2_batch_output_marker",
]
