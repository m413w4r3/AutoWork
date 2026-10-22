"""Compact, versioned operator context projections and paged context items."""

from __future__ import annotations

import base64
import hashlib
import json
import zlib
from collections.abc import Mapping
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from cti_app.application.investigation_contracts import (
    MAX_CONTEXT_BYTES,
    InvestigationContextV1,
    InvestigationSourceV1,
)
from cti_app.domain.investigation import canonical_json_bytes
from cti_app.domain.investigation_state import InvestigationWorkingState

OPERATOR_CONTEXT_SECTIONS = (
    "new_evidence",
    "evidence_index",
    "findings",
    "finding_versions",
    "opinion_notes",
    "tool_coverage",
    "sample_deltas",
    "publication_anchors",
    "article_claims",
    "article_long_texts",
    "documented_samples",
    "known_chain",
    "open_questions",
    "hypotheses",
    "rule_deltas",
    "available_tools",
    "available_pivots",
    "latest_tool_results",
    "previous_actions",
    "operator_requests",
    "agenda",
    "blocked_capabilities",
    "budget_remaining",
    "advisor_guidance",
    "unavailable_tools",
    "indicator_inventory",
    "current_opinion",
    "truncation",
    "source",
)
_FINGERPRINT_SCHEMA = "investigation-operator-context-fingerprint.v1"
_FINGERPRINT_MAX_BYTES = 8 * 1024

_TRUNCATION_SECTIONS: dict[str, tuple[str, ...]] = {
    "investigation.new_evidence_truncated": ("new_evidence",),
    "investigation.evidence_index_truncated": ("evidence_index",),
    "investigation.findings_truncated": ("findings",),
    "investigation.opinion_notes_truncated": ("opinion_notes",),
    "investigation.tool_coverage_truncated": ("tool_coverage",),
    "investigation.sample_deltas_truncated": ("sample_deltas",),
    "article.publication_anchors_truncated": ("publication_anchors",),
    "article.claims_truncated": ("article_claims",),
    "article.long_texts_truncated": ("article_long_texts",),
    "article.documented_samples_truncated": ("documented_samples",),
    "article.known_chain_truncated": ("known_chain",),
    "article.open_questions_truncated": ("open_questions",),
    "investigation.hypotheses_truncated": ("hypotheses",),
    "investigation.rule_deltas_truncated": ("rule_deltas",),
    "available_pivots_truncated": ("pivot_frontier",),
    "unavailable_tools_truncated": ("unavailable_tools",),
    "indicator_inventory.truncated": ("indicator_inventory",),
    "investigation.long_texts_truncated": (
        "new_evidence",
        "findings",
        "opinion_notes",
        "tool_coverage",
        "rule_deltas",
    ),
}
_TRUNCATION_COUNT_SECTIONS: dict[str, tuple[str, ...]] = {
    "article_long_texts": ("article_long_texts",),
    "findings_superseded": ("finding_versions",),
    "previous_actions": ("previous_actions",),
    "new_evidence": ("new_evidence",),
    "evidence_index": ("evidence_index",),
    "findings": ("findings",),
    "opinion_notes": ("opinion_notes",),
    "tool_coverage": ("tool_coverage",),
    "hypotheses": ("hypotheses",),
    "sample_deltas": ("sample_deltas",),
    "rule_deltas": ("rule_deltas",),
    "publication_anchors": ("publication_anchors",),
    "article_claims": ("article_claims",),
    "documented_samples": ("documented_samples",),
    "known_chain": ("known_chain",),
    "open_questions": ("open_questions",),
    "long_texts": ("article_long_texts",),
}


class _OperatorContextContract(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, populate_by_name=True)


class OperatorContextPointerV1(_OperatorContextContract):
    kind: Literal["endpoint"]
    uri: str = Field(min_length=1, max_length=2_048)
    section: str = Field(min_length=1, max_length=64)
    cursor: int | str | None = None
    limit: int | None = Field(default=None, ge=1, le=500)


class OperatorContextPointerSetV1(_OperatorContextContract):
    section_uri_template: str = Field(min_length=1, max_length=2_048)
    section_limit: int = Field(default=20, ge=1, le=20)
    section_cursors: dict[str, int]
    endpoints: dict[str, OperatorContextPointerV1]


class OperatorContextDeltaSinceVersionV1(_OperatorContextContract):
    from_version: int = Field(ge=1)
    to_version: int = Field(ge=1)
    pointers_to_check: tuple[str, ...]
    strategy: Literal["section_hashes", "all_sections_conservative"]


class OperatorContextStatusV1(_OperatorContextContract):
    execution_mode: Literal["operator_led"]
    stop_reason: str | None = None
    working_state_version: int = Field(ge=1)
    pending_context_checkpoint: bool
    advisor_checkpoint_due: bool


class OperatorContextBriefV1(_OperatorContextContract):
    article_summary: str = Field(max_length=900)
    article_summary_truncated: bool = False
    agenda: tuple[str, ...] = Field(max_length=6)
    agenda_truncated: bool = False
    operator_request_count: int = Field(ge=0)
    untrusted_article_data: Literal[True] = True


class OperatorContextPolicyV1(_OperatorContextContract):
    external_llm_allowed: bool
    blocked_capabilities: tuple[str, ...] = Field(max_length=128)
    budget_remaining: dict[str, int | None]
    max_context_bytes: Literal[262144] = MAX_CONTEXT_BYTES


class OperatorOpinionPointerV1(_OperatorContextContract):
    opinion_id: UUID
    version: int = Field(ge=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    section: str
    cursor: int = Field(ge=0)


class InvestigationOperatorContextSummaryV1(_OperatorContextContract):
    schema_version: Literal["investigation-operator-context-summary.v1"] = Field(
        default="investigation-operator-context-summary.v1", alias="schema"
    )
    investigation_id: UUID
    working_state_version: int = Field(ge=1)
    working_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_bytes: int = Field(ge=0, le=MAX_CONTEXT_BYTES)
    summary_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    fingerprint: str = Field(min_length=1, max_length=8_192)
    max_context_bytes: Literal[262144] = MAX_CONTEXT_BYTES
    source: InvestigationSourceV1
    status: OperatorContextStatusV1
    brief: OperatorContextBriefV1
    delta_since_version: OperatorContextDeltaSinceVersionV1 | None = None
    pointers: OperatorContextPointerSetV1
    truncation: dict[str, bool]
    truncation_counts: dict[str, int]
    truncation_pointers: dict[str, tuple[str, ...]]
    current_opinion: OperatorOpinionPointerV1 | None
    policy: OperatorContextPolicyV1

    def canonical_bytes(self, *, include_summary_hash: bool = True) -> bytes:
        excluded = set() if include_summary_hash else {"summary_sha256"}
        return canonical_json_bytes(self.model_dump(mode="json", by_alias=True, exclude=excluded))


class OperatorContextItemPageV1(_OperatorContextContract):
    schema_version: Literal["investigation-operator-context-item-page.v1"] = Field(
        default="investigation-operator-context-item-page.v1", alias="schema"
    )
    investigation_id: UUID
    working_state_version: int = Field(ge=1)
    working_state_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    context_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    section: str = Field(min_length=1, max_length=64)
    offset: int = Field(ge=0)
    limit: int = Field(ge=1, le=20)
    total: int = Field(ge=0)
    next_offset: int | None = Field(default=None, ge=0)
    next_cursor: str | None = Field(default=None, max_length=1_024)
    items: tuple[Any, ...]
    section_truncated: bool = False
    untrusted_data: Literal[True] = True


def build_operator_context_summary(
    context: InvestigationContextV1,
    *,
    investigation_id: UUID,
    working_state_version: int,
    working_state_sha256: str,
    state: InvestigationWorkingState,
    since_version: int | None = None,
    since_fingerprint: str | None = None,
    invalidation_facts: Mapping[str, Any] | None = None,
) -> InvestigationOperatorContextSummaryV1:
    """Project the full, hash-checked context into a small local-operator brief."""
    if state.version != working_state_version:
        raise ValueError("Operator context summary state version mismatch")
    if state.sha256 != working_state_sha256:
        raise ValueError("Operator context summary state hash mismatch")
    if since_version is not None and (since_version < 1 or since_version > state.version):
        raise ValueError("Operator context summary delta base is invalid")

    context_bytes = context.canonical_bytes()
    context_sha256 = hashlib.sha256(context_bytes).hexdigest()
    pointers = _pointers(investigation_id=investigation_id)
    section_fingerprints = _section_fingerprints(
        context,
        state=state,
        invalidation_facts=invalidation_facts,
        pointer_names=_pointer_names(pointers),
    )
    invalidation_fingerprints = _invalidation_fingerprints(
        context,
        section_fingerprints=section_fingerprints,
        invalidation_facts=invalidation_facts,
    )
    fingerprint = _encode_fingerprint(
        state_version=state.version,
        section_fingerprints=section_fingerprints,
        invalidation_fingerprints=invalidation_fingerprints,
    )
    if since_version is None:
        delta = None
    else:
        baseline = _decode_fingerprint(since_fingerprint)
        strategy: Literal["section_hashes", "all_sections_conservative"]
        if (
            baseline is None
            or baseline["state_version"] != since_version
            or set(baseline["sections"]) != set(section_fingerprints)
            or set(baseline["invalidations"]) != set(invalidation_fingerprints)
        ):
            pointers_to_check = _pointer_names(pointers)
            strategy = "all_sections_conservative"
        else:
            invalidated = any(
                baseline["invalidations"][name] != digest
                for name, digest in invalidation_fingerprints.items()
            )
            if invalidated:
                pointers_to_check = _pointer_names(pointers)
                strategy = "all_sections_conservative"
            else:
                pointers_to_check = tuple(
                    name
                    for name in _pointer_names(pointers)
                    if baseline["sections"][name] != section_fingerprints[name]
                )
                strategy = "section_hashes"
        delta = OperatorContextDeltaSinceVersionV1(
            from_version=since_version,
            to_version=state.version,
            pointers_to_check=pointers_to_check,
            strategy=strategy,
        )

    article_summary = context.article.short_summary if context.article is not None else ""
    article_summary_truncated = _article_summary_truncated(article_summary, 320)
    article_summary = _untrusted_excerpt(article_summary, 320)
    stop_reason = getattr(state.stop_reason, "value", state.stop_reason)
    blocked = context.investigation.blocked_capabilities
    truncation = {name: True for name, truncated in _truncation_flags(context).items() if truncated}
    if article_summary_truncated:
        truncation["brief.article_summary_truncated"] = True
    truncation_counts = {
        name: count for name, count in context.investigation.truncation_counts.items() if count > 0
    }
    summary = InvestigationOperatorContextSummaryV1(
        investigation_id=investigation_id,
        working_state_version=working_state_version,
        working_state_sha256=working_state_sha256,
        context_sha256=context_sha256,
        context_bytes=len(context_bytes),
        summary_sha256="0" * 64,
        fingerprint=fingerprint,
        max_context_bytes=MAX_CONTEXT_BYTES,
        source=context.source,
        status=OperatorContextStatusV1(
            execution_mode="operator_led",
            stop_reason=str(stop_reason) if stop_reason is not None else None,
            working_state_version=state.version,
            pending_context_checkpoint=state.pending_context_sha256 is not None,
            advisor_checkpoint_due=context.advisor_guidance.checkpoint_due,
        ),
        brief=OperatorContextBriefV1(
            article_summary=article_summary,
            article_summary_truncated=article_summary_truncated,
            agenda=tuple(context.investigation.agenda[:3]),
            agenda_truncated=len(context.investigation.agenda) > 3,
            operator_request_count=len(context.operator_requests),
        ),
        delta_since_version=delta,
        pointers=pointers,
        truncation=truncation,
        truncation_counts=truncation_counts,
        truncation_pointers=_truncation_pointers(truncation, truncation_counts),
        current_opinion=(
            OperatorOpinionPointerV1(
                opinion_id=context.current_opinion.opinion_id,
                version=context.current_opinion.version,
                content_sha256=context.current_opinion.content_sha256,
                section="opinion_notes",
                cursor=0,
            )
            if context.current_opinion is not None
            else None
        ),
        policy=OperatorContextPolicyV1(
            external_llm_allowed=not any(
                item.startswith("bridge.advisor:external_llm_disclosure_denied") for item in blocked
            ),
            blocked_capabilities=tuple(blocked),
            budget_remaining=dict(context.investigation.budget_remaining),
            max_context_bytes=MAX_CONTEXT_BYTES,
        ),
    )
    summary_hash = hashlib.sha256(summary.canonical_bytes(include_summary_hash=False)).hexdigest()
    return summary.model_copy(update={"summary_sha256": summary_hash})


def _pointer_names(pointers: OperatorContextPointerSetV1) -> tuple[str, ...]:
    return tuple(sorted((*pointers.section_cursors, *pointers.endpoints)))


def _section_fingerprints(
    context: InvestigationContextV1,
    *,
    state: InvestigationWorkingState,
    invalidation_facts: Mapping[str, Any] | None,
    pointer_names: tuple[str, ...],
) -> dict[str, str]:
    payload = context.model_dump(mode="json", by_alias=True)
    article = payload.get("article") or {}
    investigation = payload.get("investigation") or {}
    facts = invalidation_facts or {}
    current_opinion = (
        context.current_opinion.model_dump(mode="json")
        if context.current_opinion is not None
        else None
    )
    sections: dict[str, Any] = {
        **investigation,
        "operator_requests": payload.get("operator_requests"),
        "available_tools": payload.get("available_tools"),
        "unavailable_tools": payload.get("unavailable_tools"),
        "indicator_inventory": payload.get("indicator_inventory"),
        "available_pivots": payload.get("available_pivots"),
        **{
            "article_claims": article.get("claims"),
            "article_long_texts": {
                "short_summary": article.get("short_summary"),
                "long_texts": article.get("long_texts"),
            },
            "documented_samples": article.get("documented_samples"),
            "known_chain": article.get("known_chain"),
            "open_questions": article.get("open_questions"),
            "publication_anchors": article.get("publication_anchors"),
            "source": payload.get("source"),
            "current_opinion": current_opinion,
            "truncation": {
                "flags": _truncation_flags(context),
                "counts": dict(context.investigation.truncation_counts),
            },
            "pivot_frontier": {
                "available_pivots": payload.get("available_pivots"),
                "available_pivots_truncated": payload.get("available_pivots_truncated"),
                "indicator_inventory": payload.get("indicator_inventory"),
                "source": payload.get("source"),
            },
            "experiments": facts.get("experiments"),
            "advisor_checkpoint": {
                "advisor_guidance": payload.get("advisor_guidance"),
                "pending_context_checkpoint": state.pending_context_sha256 is not None,
                "stop_reason": getattr(state.stop_reason, "value", state.stop_reason),
                "turn_index": state.turn_index,
            },
        },
    }
    return {name: _fingerprint_digest(sections.get(name)) for name in pointer_names}


def _invalidation_fingerprints(
    context: InvestigationContextV1,
    *,
    section_fingerprints: Mapping[str, str],
    invalidation_facts: Mapping[str, Any] | None,
) -> dict[str, str]:
    facts = invalidation_facts or {}
    defaults: dict[str, Any] = {
        "observations": {
            name: section_fingerprints[name]
            for name in (
                "new_evidence",
                "evidence_index",
                "latest_tool_results",
                "rule_deltas",
            )
        },
        "publication": {
            name: section_fingerprints[name]
            for name in ("publication_anchors", "current_opinion", "source")
        },
        "schema": {
            "context_schema": context.schema_version,
            "available_tools": section_fingerprints["available_tools"],
        },
        "worker": {
            name: section_fingerprints[name]
            for name in ("tool_coverage", "available_tools", "unavailable_tools")
        },
        "g2": {
            name: section_fingerprints[name]
            for name in (
                "blocked_capabilities",
                "budget_remaining",
                "available_tools",
                "unavailable_tools",
            )
        },
    }
    aliases = {
        "observations": "observations",
        "publication": "publication",
        "schema": "schema",
        "worker": "worker",
        "g2": "g2",
    }
    return {
        name: _fingerprint_digest(facts.get(fact_key, defaults[name]))
        for name, fact_key in aliases.items()
    }


def _fingerprint_digest(value: Any) -> str:
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _encode_fingerprint(
    *,
    state_version: int,
    section_fingerprints: Mapping[str, str],
    invalidation_fingerprints: Mapping[str, str],
) -> str:
    payload = {
        "schema": _FINGERPRINT_SCHEMA,
        "state_version": state_version,
        "sections": dict(section_fingerprints),
        "invalidations": dict(invalidation_fingerprints),
    }
    encoded = zlib.compress(canonical_json_bytes(payload), level=9)
    return base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_fingerprint(value: str | None) -> dict[str, Any] | None:
    if not isinstance(value, str) or not value or len(value) > 8_192:
        return None
    try:
        compressed = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
        decoder = zlib.decompressobj()
        raw = decoder.decompress(compressed, _FINGERPRINT_MAX_BYTES + 1)
        if len(raw) > _FINGERPRINT_MAX_BYTES or decoder.unconsumed_tail:
            return None
        raw += decoder.flush()
        if len(raw) > _FINGERPRINT_MAX_BYTES:
            return None
        payload = json.loads(raw)
    except (ValueError, TypeError, zlib.error, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("schema") != _FINGERPRINT_SCHEMA:
        return None
    version = payload.get("state_version")
    sections = payload.get("sections")
    invalidations = payload.get("invalidations")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        return None
    if not isinstance(sections, dict) or not isinstance(invalidations, dict):
        return None
    if any(not _is_sha256(value) for value in sections.values()):
        return None
    if any(not _is_sha256(value) for value in invalidations.values()):
        return None
    return {
        "state_version": version,
        "sections": sections,
        "invalidations": invalidations,
    }


def _is_sha256(value: Any) -> bool:
    return (
        isinstance(value, str)
        and len(value) == 64
        and all(character in "0123456789abcdef" for character in value)
    )


def _truncation_flags(context: InvestigationContextV1) -> dict[str, bool]:
    payload = context.model_dump(mode="python", by_alias=True)
    flags: dict[str, bool] = {}

    def collect(value: Any, prefix: str = "") -> None:
        if isinstance(value, dict):
            for key, item in value.items():
                name = f"{prefix}.{key}" if prefix else str(key)
                if str(key).endswith("_truncated") and isinstance(item, bool):
                    flags[name] = item
                else:
                    collect(item, name)
        elif isinstance(value, list | tuple):
            for index, item in enumerate(value):
                collect(item, f"{prefix}[{index}]")

    collect(payload)
    if context.indicator_inventory.truncated:
        flags["indicator_inventory.truncated"] = True
    return dict(sorted(flags.items()))


def _pointers(
    *,
    investigation_id: UUID,
) -> OperatorContextPointerSetV1:
    base = f"/api/investigations/{investigation_id}/operator/context/section"
    investigation_uri = f"/api/investigations/{investigation_id}"
    return OperatorContextPointerSetV1(
        section_uri_template=(
            f"{base}/{{section}}?cursor={{cursor}}&limit={{limit}}"
            "&expected_version={version}&expected_sha256={sha256}"
        ),
        section_limit=20,
        section_cursors=dict.fromkeys(OPERATOR_CONTEXT_SECTIONS, 0),
        endpoints={
            "pivot_frontier": OperatorContextPointerV1(
                kind="endpoint",
                uri=f"{investigation_uri}/pivots?limit=200",
                section="pivot_frontier",
                cursor=None,
                limit=200,
            ),
            "experiments": OperatorContextPointerV1(
                kind="endpoint",
                uri=f"{investigation_uri}/experiments",
                section="experiments",
            ),
            "advisor_checkpoint": OperatorContextPointerV1(
                kind="endpoint",
                uri=f"{investigation_uri}/operator-status",
                section="advisor_checkpoint",
            ),
        },
    )


def _truncation_pointers(
    truncation: dict[str, bool],
    truncation_counts: dict[str, int],
) -> dict[str, tuple[str, ...]]:
    result: dict[str, tuple[str, ...]] = {}
    section_map = {
        **_TRUNCATION_SECTIONS,
        "brief.article_summary_truncated": ("article_long_texts",),
    }
    for flag, sections in section_map.items():
        if truncation.get(flag) is True:
            result[flag] = sections
    for name, sections in _TRUNCATION_COUNT_SECTIONS.items():
        if truncation_counts.get(name, 0) > 0:
            result[f"count.{name}"] = sections
    return result


def _article_summary_truncated(value: str, limit: int) -> bool:
    prefix = "[UNTRUSTED ARTICLE OR TOOL DATA]"
    suffix = "[END UNTRUSTED DATA]"
    text = value.strip()
    body = text
    if text.startswith(prefix):
        body = text[len(prefix) :].strip()
        if body.endswith(suffix):
            body = body[: -len(suffix)].rstrip()
    return len(body) > limit


def _untrusted_excerpt(value: str, limit: int) -> str:
    prefix = "[UNTRUSTED ARTICLE OR TOOL DATA]"
    suffix = "[END UNTRUSTED DATA]"
    text = value.strip()
    if text.startswith(prefix):
        body = text[len(prefix) :].strip()
        if body.endswith(suffix):
            body = body[: -len(suffix)].rstrip()
    else:
        body = text
    if len(body) > limit:
        body = body[:limit].rstrip() + "…"
    return f"{prefix}\n{body}\n{suffix}" if body else ""


__all__ = [
    "InvestigationOperatorContextSummaryV1",
    "OperatorContextItemPageV1",
    "OperatorContextPointerSetV1",
    "OperatorContextPointerV1",
    "build_operator_context_summary",
]
