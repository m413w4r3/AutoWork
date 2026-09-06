"""The single way to obtain the exact value behind one Q2 repair issue.

The Repair Desk detail, the adjudication that accepts an INCLUDE and the
projection that materializes it must all see the same string, or the analyst
decides on a value the deliverable will never contain.  This module owns that
resolution, once, for all three.

Nothing here calls a model.  A historical rejection is recovered strictly from
what was persisted at the time — the repair evidence pack, the inline legacy
value, or the raw Q2 output already archived for its ``ModelRun`` — and every
recovered candidate must match the SHA-256 that was stored with the rejection.
The archived output is never replayed through the current source-evidence
gate: the gate's policy has changed, and the historical truth is the persisted
identity plus the persisted hash, not today's verdict.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.production_parsers import (
    ParsedSource,
    Q2SourceOutput,
    parse_q2_proposals_markdown,
)
from cti_app.application.production_q2_batch import parse_q2_batch_response
from cti_app.application.production_source_evidence import (
    Q2ProposalIdentity,
    enumerate_q2_proposals,
)
from cti_app.domain.discovery import SourceRole

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
# One archived Q2 answer is a Markdown document, not a corpus.
MAX_MODEL_OUTPUT_BYTES = 4_000_000


class RepairPayloadOrigin(StrEnum):
    """Where the exact value shown to the analyst actually came from."""

    REPAIR_EVIDENCE_PACK = "repair_evidence_pack"
    LEGACY_INLINE_VERIFIED = "legacy_inline_verified"
    MODEL_OUTPUT_RECOVERED = "model_output_recovered"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class RepairPayload:
    """The resolved value of one repair issue, and how it was obtained."""

    value: str | None
    value_sha256: str
    origin: RepairPayloadOrigin
    available: bool

    @property
    def recovered(self) -> bool:
        """True when the value came from an archive rather than the pack."""
        return self.origin in {
            RepairPayloadOrigin.LEGACY_INLINE_VERIFIED,
            RepairPayloadOrigin.MODEL_OUTPUT_RECOVERED,
        }


class ModelOutputArchiveReader(Protocol):
    """The read-only slice of the model archive this resolver may use."""

    async def get_run(self, run_id: UUID) -> Any | None: ...

    async def read_output(self, reference: str, *, max_bytes: int = ...) -> bytes: ...


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _unavailable(value_sha256: str) -> RepairPayload:
    return RepairPayload(
        value=None,
        value_sha256=value_sha256,
        origin=RepairPayloadOrigin.UNAVAILABLE,
        available=False,
    )


def _entry_hash(entry: Mapping[str, Any]) -> str:
    """Return the persisted exact-value hash, or an empty string."""
    candidate = entry.get("value_sha256") or entry.get("value_hash")
    if isinstance(candidate, str) and _SHA256_RE.fullmatch(candidate.casefold()):
        return candidate.casefold()
    return ""


def _matches_persisted_identity(candidate: Q2ProposalIdentity, entry: Mapping[str, Any]) -> bool:
    """Keep only candidates the persisted facts allow this rejection to be."""
    proposal_kind = entry.get("proposal_kind")
    if isinstance(proposal_kind, str) and proposal_kind:
        if proposal_kind != candidate.proposal_kind:
            return False
    artifact_type = entry.get("artifact_type")
    if isinstance(artifact_type, str) and artifact_type:
        recorded = artifact_type.casefold()
        actual = (candidate.artifact_type or "").casefold()
        # Rule rejections were recorded with the bare rule type ("yara"), the
        # publication side sometimes with the "_rule" suffix.
        if recorded.removesuffix("_rule") != actual.removesuffix("_rule"):
            return False
    return True


class _ArchivedOutputCache:
    """Read and parse each archived Q2 answer at most once per operation."""

    def __init__(self, archive: ModelOutputArchiveReader | None) -> None:
        self._archive = archive
        self._raw: dict[UUID, str | None] = {}
        self._runs: dict[UUID, Any | None] = {}
        self._outputs: dict[tuple[UUID, str | None], Q2SourceOutput | None] = {}

    async def q2_output(self, entry: Mapping[str, Any]) -> Q2SourceOutput | None:
        if self._archive is None:
            return None
        try:
            run_id = UUID(str(entry.get("model_run_id")))
        except (TypeError, ValueError):
            return None
        batch_id = entry.get("batch_id")
        key = (run_id, batch_id if isinstance(batch_id, str) and batch_id else None)
        if key in self._outputs:
            return self._outputs[key]
        output = await self._parse(run_id, key[1], entry)
        self._outputs[key] = output
        return output

    async def _parse(
        self, run_id: UUID, batch_id: str | None, entry: Mapping[str, Any]
    ) -> Q2SourceOutput | None:
        raw = await self._read(run_id)
        if not raw or not raw.strip():
            return None
        run = self._runs.get(run_id)
        parameters = getattr(run, "parameters", None)
        parameters = parameters if isinstance(parameters, dict) else {}
        batch_sources = parameters.get("q2_batch_sources")
        if parameters.get("q2_execution_kind") == "batch" and isinstance(batch_sources, list):
            return _parse_batch_block(raw, batch_sources, batch_id, entry)
        parsed = parse_q2_proposals_markdown(raw)
        return parsed.value if parsed.usable else None

    async def _read(self, run_id: UUID) -> str | None:
        if run_id in self._raw:
            return self._raw[run_id]
        content: str | None = None
        assert self._archive is not None
        try:
            run = await self._archive.get_run(run_id)
            self._runs[run_id] = run
            reference = getattr(run, "raw_output_reference", None) or next(
                iter(getattr(run, "output_references", ()) or ()), None
            )
            if reference:
                content = (
                    await self._archive.read_output(
                        str(reference), max_bytes=MAX_MODEL_OUTPUT_BYTES
                    )
                ).decode("utf-8", errors="replace")
        except Exception:
            # A missing or unreadable archive is "not recoverable", never a
            # failure of the Repair Desk read that asked for it.
            content = None
        self._raw[run_id] = content
        return content


def _parse_batch_block(
    raw: str,
    batch_sources: Sequence[Any],
    batch_id: str | None,
    entry: Mapping[str, Any],
) -> Q2SourceOutput | None:
    """Parse only the block belonging to this issue's source in a batch."""
    mapping = {
        str(item["batch_id"]): str(item["canonical_url"])
        for item in batch_sources
        if isinstance(item, dict) and item.get("batch_id") and item.get("canonical_url")
    }
    if batch_id is None:
        source_url = str(entry.get("source_url", ""))
        matching = [key for key, url in mapping.items() if url == source_url]
        if len(matching) != 1:
            return None
        batch_id = matching[0]
    if batch_id not in mapping:
        return None
    parsed = parse_q2_batch_response(raw, {batch_id: _batch_source(batch_id, mapping[batch_id])})
    if not parsed.usable:
        return None
    result = next((item for item in parsed.sources if item.batch_id == batch_id), None)
    return result.output if result is not None and result.usable else None


def _batch_source(batch_id: str, canonical_url: str) -> ParsedSource:
    """The batch parser only needs the expected labels, not Q1 metadata."""
    return ParsedSource(
        local_id=batch_id,
        title="",
        url=canonical_url,
        canonical_url=canonical_url,
        publisher=None,
        published_at=None,
        role=SourceRole.UNKNOWN,
    )


class ProductionRepairPayloadResolver:
    """Resolve repair values from persisted evidence, never from a model."""

    def __init__(self, model_output_archive: ModelOutputArchiveReader | None = None) -> None:
        self._archive = model_output_archive

    async def resolve(
        self,
        entry: Mapping[str, Any],
        *,
        payload_available: bool,
        value_sha256: str | None = None,
    ) -> RepairPayload:
        """Resolve one issue.

        ``value_sha256`` is the identity the caller already computed for this
        issue; it wins over the entry's own field so the value shown is always
        the value the repair key was built from.
        """
        payloads = await self.resolve_many(
            [entry],
            payload_available=payload_available,
            value_sha256_by_index={0: value_sha256} if value_sha256 else None,
        )
        return payloads[0]

    async def resolve_many(
        self,
        entries: Sequence[Mapping[str, Any]],
        *,
        payload_available: bool,
        value_sha256_by_index: Mapping[int, str] | None = None,
    ) -> tuple[RepairPayload, ...]:
        """Resolve several issues, reading each ModelRun output only once."""
        overrides = value_sha256_by_index or {}
        cache = _ArchivedOutputCache(self._archive)
        resolved: list[RepairPayload] = []
        for index, entry in enumerate(entries):
            override = overrides.get(index)
            target = override.casefold() if override else _entry_hash(entry)
            resolved.append(await self._resolve_one(entry, payload_available, target, cache))
        return tuple(resolved)

    async def _resolve_one(
        self,
        entry: Mapping[str, Any],
        payload_available: bool,
        target: str,
        cache: _ArchivedOutputCache,
    ) -> RepairPayload:
        raw_value = entry.get("value")
        inline = raw_value if isinstance(raw_value, str) and raw_value else None

        # 1. The LOT18+ evidence pack, verified exactly as before.
        if payload_available and inline is not None and _sha256(inline) == target:
            return RepairPayload(
                value=inline,
                value_sha256=target,
                origin=RepairPayloadOrigin.REPAIR_EVIDENCE_PACK,
                available=True,
            )

        # 2. A legacy inline value is trusted only when its hash proves it is
        #    complete. A rule truncated to the old 512-character preview never
        #    matches the hash of its full body, so it is never declared exact.
        if inline is not None and target and _sha256(inline) == target:
            return RepairPayload(
                value=inline,
                value_sha256=target,
                origin=RepairPayloadOrigin.LEGACY_INLINE_VERIFIED,
                available=True,
            )

        # 3. The archived Q2 output the rejection was extracted from.
        if not target:
            return _unavailable(target)
        recovered = await self._recover_from_archived_output(entry, target, cache)
        if recovered is not None:
            return RepairPayload(
                value=recovered,
                value_sha256=target,
                origin=RepairPayloadOrigin.MODEL_OUTPUT_RECOVERED,
                available=True,
            )
        return _unavailable(target)

    async def _recover_from_archived_output(
        self,
        entry: Mapping[str, Any],
        target: str,
        cache: _ArchivedOutputCache,
    ) -> str | None:
        """Find the one archived proposal whose hash is the persisted hash."""
        output = await cache.q2_output(entry)
        if output is None:
            return None
        candidates = [
            candidate
            for candidate in enumerate_q2_proposals(output)
            if _matches_persisted_identity(candidate, entry) and _sha256(candidate.value) == target
        ]
        if not candidates:
            return None
        proposal_index = entry.get("proposal_index")
        if isinstance(proposal_index, int):
            # The historical index disambiguates; it must also be consistent.
            indexed = [item for item in candidates if item.proposal_index == proposal_index]
            return indexed[0].value if len(indexed) == 1 else None
        # Without an index, only an unambiguous hash match may be accepted:
        # "the first domain in the output" is never an answer.
        return candidates[0].value if len(candidates) == 1 else None
