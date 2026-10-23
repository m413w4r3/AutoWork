"""Build compact, readable adviser dossiers from verified task facts.

This module deliberately produces prose rather than a serialization of the
internal consultation object.  Its manifest is local provenance metadata and
must never be appended to the Bridge prompt.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from cti_app.domain.advisor_completeness import (
    AdvisorEvidenceStatus,
    AdvisorPreflightStatus,
)

_WORD = re.compile(r"[\w.+#/-]+", re.UNICODE)
_CLAIM_ID = re.compile(r"\b(?:S\d+P\d+(?:\.\d+)?|L\d+(?:\.\d+)?|T\d+)\b", re.I)
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_INTERNAL_SHA = re.compile(r"(?i)\b(?:sha(?:-?256)?\s*[:=]\s*)?[0-9a-f]{64}\b")
_HEXDUMP_LINE = re.compile(r"(?im)^\s*(?:0x)?[0-9a-f]{4,16}:\s+(?:[0-9a-f]{2,8}\s+){4,}")
# Aggregate rendered observation-excerpt cap; dossier size remains uncapped.
MAX_OBSERVATION_EXCERPT_BYTES = 16_384
MAX_GENERIC_BINARY_CONTENT_BYTES = 512
_STOP = frozenset(
    "avec dans pour sans cette celui celle sont être avoir mais comme dans sur sous une des les "
    "aux par que qui quoi dont alors leur leurs elles ils cela ceci nous vous elle lui son sa "
    "ses from that this those have with which what where when then than into over under".split()
)


class ContentExcerptLimitExceeded(ValueError):
    """Reject oversized generic content before it can enter a dossier or prompt."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


def _reject_oversized_base64_content(value: str, *, encoding: object = None) -> None:
    declared_base64 = isinstance(encoding, str) and encoding.casefold() in {
        "base64",
        "base64url",
        "base64_url",
    }
    candidate = re.sub(r"\s+", "", value)
    if not candidate or re.fullmatch(r"[A-Za-z0-9+/=_-]+", candidate) is None:
        if declared_base64:
            raise ContentExcerptLimitExceeded("declared_base64_content_is_invalid")
        return
    padded = candidate + ("=" * (-len(candidate) % 4))
    try:
        decoded = base64.b64decode(padded, altchars=b"-_", validate=True)
    except (ValueError, binascii.Error):
        if declared_base64:
            raise ContentExcerptLimitExceeded("declared_base64_content_is_invalid") from None
        return
    if len(decoded) > MAX_GENERIC_BINARY_CONTENT_BYTES:
        raise ContentExcerptLimitExceeded("base64_content_exceeds_512_byte_decoded_limit")


_DEFAULT_TASKS = {
    "plan_investigation": "Choisir la prochaine étape utile à partir des faits vérifiés.",
    "pivot_strategy": (
        "Proposer le pivot le plus discriminant à partir des observations et mesures."
    ),
    "yara_draft": "Construire une règle YARA à partir des marqueurs mesurés du binaire.",
    "review_text": "Relire le texte cible au regard des sources citées et passages canoniques.",
    "draft_text": "Rédiger le texte cible à partir des assertions et faits transmis.",
    "suricata_draft": "Construire une règle Suricata à partir des faits réseau transmis.",
    "figure_plan": "Décrire un plan de figure étayé par les faits transmis.",
}
_METHOD_KEYS = frozenset(
    {"decompilation", "decompiled_code", "method_body", "body", "source_code", "code"}
)
_METHOD_CONTAINER_KEYS = frozenset({"methods", "functions", "decompiled_methods", "decompilations"})
_STRING_KEYS = frozenset({"strings", "decoded_strings", "decoded_values", "configuration_values"})
_CONTENT_KEYS = frozenset(
    {
        "text",
        "statement",
        "decoded",
        "decoded_value",
        "value",
        "output",
        "stdout",
        "content",
        "excerpt",
        "description",
    }
)
_OMIT_KEYS = frozenset(
    {
        "id",
        "uuid",
        "sha256",
        "sha256_short",
        "sample_sha256",
        "evidence_id",
        "finding_id",
        "blob_id",
        "object_id",
        "tool_id",
        "invocation_id",
        "schema",
        "schema_version",
        "truncated",
        "limitations",
        "warnings",
        "status",
        "reason",
        "duration",
        "duration_ms",
        "byte_size",
        "mime_type",
        "source_version",
    }
)


@dataclass(frozen=True, slots=True)
class AdvisorDossier:
    text: str
    manifest: tuple[dict[str, Any], ...]

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


def extract_content_excerpts(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Extract complete methods, decoded values, and sentence-bounded text."""
    excerpts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
    total_excerpt_bytes = 0
    short_hex_rows: list[tuple[str, str]] = []
    short_hex_identities: set[tuple[int | None, str]] = set()
    short_hex_payload_bytes = 0
    short_hex_payload_exceeds_limit = False
    assembly_name = next(
        (
            value.strip()
            for key, value in payload.items()
            if str(key).casefold() in {"assembly", "assembly_name", "file_name", "binary_name"}
            and isinstance(value, str)
            and value.strip()
        ),
        "",
    )

    def add(kind: str, title: str, value: object, *, encoding: object = None) -> None:
        nonlocal total_excerpt_bytes
        if not isinstance(value, str) or not value.strip():
            return
        if kind != "hex":
            _reject_oversized_base64_content(value, encoding=encoding)
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
        if _HEXDUMP_LINE.search(text):
            raise ContentExcerptLimitExceeded("raw_hexdump_content_rejected")
        prefix = (
            f"{assembly_name} — "
            if assembly_name and assembly_name.casefold() not in title.casefold()
            else ""
        )
        chunks = [text]
        if kind == "text":
            chunks = [line.strip() for line in text.splitlines() if line.strip()] or [text]
            sentence_chunks: list[str] = []
            for chunk in chunks:
                sentence_chunks.extend(
                    part.strip()
                    for part in re.split(r"(?<=[.!?])\s+(?=[A-ZÀ-Þ0-9])", chunk)
                    if part.strip()
                )
            chunks = sentence_chunks or chunks
        for index, chunk in enumerate(chunks, start=1):
            identity = (kind, f"{title.strip()}\n{chunk}" if kind == "hex" else chunk)
            if identity in seen:
                continue
            suffix = f" {index}" if len(chunks) > 1 else ""
            rendered_title = f"{prefix}{title.strip()}{suffix}"
            excerpt_piece = f"{rendered_title}:\n{_quote(chunk)}"
            chunk_bytes = len(excerpt_piece.encode("utf-8"))
            if total_excerpt_bytes + chunk_bytes > MAX_OBSERVATION_EXCERPT_BYTES:
                raise ContentExcerptLimitExceeded("content_excerpt_budget_exceeds_16384_bytes")
            total_excerpt_bytes += chunk_bytes
            seen.add(identity)
            excerpts.append({"kind": kind, "title": rendered_title, "text": chunk})

    def display_name(row: Mapping[str, Any], fallback: str) -> str:
        for key in ("name", "method", "function", "symbol", "member", "key"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    def add_short_hex_rows(value: object) -> None:
        nonlocal short_hex_payload_bytes, short_hex_payload_exceeds_limit
        if short_hex_payload_exceeds_limit:
            return
        rows = value if isinstance(value, (list, tuple)) else (value,)
        for row in rows:
            if not isinstance(row, Mapping):
                continue
            raw = row.get("value", row.get("bytes", row.get("hex")))
            if not isinstance(raw, str):
                continue
            if re.fullmatch(
                r"(?i)(?:0x)?[0-9a-f]{2}(?:(?:[\s,:-]+)(?:0x)?[0-9a-f]{2}|[0-9a-f]{2})*",
                raw.strip(),
            ) is None:
                continue
            compact = re.sub(r"[^0-9a-fA-F]", "", raw)
            if (
                re.fullmatch(r"[0-9a-fA-F]+", compact) is None
                or len(compact) % 2
                or len(compact) > MAX_GENERIC_BINARY_CONTENT_BYTES * 2
            ):
                # Full or malformed dumps are not evidence excerpts.
                continue
            offset = row.get("offset")
            normalized_offset = offset if type(offset) is int and offset >= 0 else None
            identity = (normalized_offset, compact.casefold())
            if identity in short_hex_identities:
                continue
            short_hex_identities.add(identity)
            short_hex_payload_bytes += len(compact) // 2
            if short_hex_payload_bytes > MAX_GENERIC_BINARY_CONTENT_BYTES:
                # A dump split across offset rows is still too large to include.
                short_hex_payload_exceeds_limit = True
                short_hex_rows.clear()
                continue
            if short_hex_payload_exceeds_limit:
                continue
            title = (
                f"Octets observés — offset {normalized_offset}"
                if normalized_offset is not None
                else "Octets observés"
            )
            short_hex_rows.append((title, raw))

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if any(
            token in part.casefold().replace("_", " ")
            for part in path
            for token in ("hex", "byte", "hexdump", "base64")
        ):
            return
        if isinstance(value, Mapping):
            name = display_name(value, path[-1] if path else "contenu observé")
            for raw_key, child in value.items():
                key = str(raw_key).casefold().replace("-", "_")
                if key in _OMIT_KEYS or "sha256" in key or key.endswith("_id"):
                    continue
                if payload.get("schema") == "local-command-execution.v1" and key in {
                    "command",
                    "command_id",
                    "output",
                    "output_chunks",
                    "output_encoding",
                    "output_byte_length",
                    "output_truncated",
                    "output_truncated_by_evidence_limit",
                    "output_truncated_by_response",
                }:
                    # Command bytes and their encodings use the verified measurement path.
                    continue
                if key in _METHOD_KEYS and isinstance(child, str):
                    method_kind = (
                        "Méthodes de mappage PE"
                        if re.search(
                            r"(?i)\b(?:map|mapping|peimage|manualmap)\b", f"{name} {child}"
                        )
                        else "Méthode"
                    )
                    add("method", f"{method_kind} — {name}", child)
                    continue
                if key in {"hex", "hex_bytes", "bytes_hex", "hexdump"}:
                    # Preserve bounded offset measurements; reject complete dumps.
                    add_short_hex_rows(child)
                    continue
                if key in _METHOD_CONTAINER_KEYS:
                    walk(child, (*path, key))
                    continue
                if key in _STRING_KEYS:
                    walk(child, (*path, key))
                    continue
                if key in _CONTENT_KEYS and isinstance(child, str):
                    content_path = " ".join(path).casefold()
                    if any(token in content_path for token in ("hex", "byte", "hexdump")):
                        # A nested textual rendering of bytes is raw material too.
                        continue
                    elif any(token in content_path for token in ("string", "decoded", "config")):
                        add(
                            "decoded",
                            f"Valeur décodée — {name}",
                            child,
                            encoding=value.get("encoding"),
                        )
                    else:
                        add(
                            "text",
                            f"Texte observé — {name}",
                            child,
                            encoding=value.get("encoding"),
                        )
                    continue
                if isinstance(child, (Mapping, list, tuple)):
                    walk(child, (*path, key))
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value, start=1):
                if isinstance(child, str):
                    kind = (
                        "decoded"
                        if path and any("string" in item or "decoded" in item for item in path)
                        else "text"
                    )
                    add(
                        kind,
                        f"Valeur décodée {index}"
                        if kind == "decoded"
                        else f"Texte observé {index}",
                        child,
                    )
                else:
                    walk(child, (*path, str(index)))
            return
        if isinstance(value, str) and path:
            key = path[-1].casefold()
            if key in _METHOD_KEYS:
                add("method", f"Méthode {path[-2] if len(path) > 1 else 'observée'}", value)

    walk(payload)
    if not short_hex_payload_exceeds_limit:
        for title, raw in short_hex_rows:
            add("hex", title, raw)
    return excerpts


def build_advisor_dossier(prompt_data: Mapping[str, Any]) -> AdvisorDossier:
    """Select pertinent whole facts and render a shared French task dossier."""
    consultation_type = _as_text(prompt_data.get("consultation_type"))
    task = _as_text(prompt_data.get("question")) or _DEFAULT_TASKS.get(consultation_type, "")
    targets = _target_rows(prompt_data)
    target_text = "\n".join(_as_text(item.get("text")) for item in targets)
    relevance_text = f"{task}\n{target_text}"
    task_terms = _terms(relevance_text)
    explicit_claims = _requested_claim_ids(prompt_data, relevance_text)
    contradiction_required = _contradiction_is_required(prompt_data, relevance_text)
    explicitly_referenced = set(_explicit_reference_ids(prompt_data))
    unavailable_observations: set[str] = set()
    candidates: list[dict[str, Any]] = []

    article_ref = _article_identity(prompt_data)
    for item in prompt_data.get("article_passages", ()):
        if not isinstance(item, Mapping):
            continue
        claim_id = _as_text(item.get("claim_id"))
        heading = _as_text(item.get("heading"))
        text = _as_text(item.get("text"))
        score = _relevance_score(task_terms, f"{claim_id} {heading} {text}")
        if claim_id.casefold() in explicit_claims:
            score += 100
        if score > 0:
            candidates.append(
                _candidate(
                    kind="article",
                    identifier=article_ref,
                    sha256=_as_text(prompt_data.get("article_sha256")),
                    label=f"Article {claim_id} — {heading}".strip(" —"),
                    body=text,
                    score=score,
                    origin="article_passage",
                    extra={
                        "claim_id": claim_id,
                        "source_ref": _as_text(prompt_data.get("article_source_ref"))
                        or f"article:{article_ref}",
                        **(
                            {"section_index": item["section_index"]}
                            if type(item.get("section_index")) is int
                            else {}
                        ),
                        **(
                            {"passage_offset": item["offset"]}
                            if type(item.get("offset")) is int
                            else {}
                        ),
                    },
                )
            )

    if consultation_type in {"plan_investigation", "pivot_strategy"}:
        for item in prompt_data.get("hypotheses", ()):
            if not isinstance(item, Mapping):
                continue
            identifier = _as_text(item.get("id"))
            statement = _as_text(item.get("statement"))
            if not identifier or not statement:
                continue
            state_sha256 = _as_text(prompt_data.get("working_state_sha256"))
            state_version = prompt_data.get("working_state_version")
            candidates.append(
                _candidate(
                    kind="hypothesis",
                    identifier=identifier,
                    sha256=state_sha256,
                    label=f"Hypothèse — {_as_text(item.get('status')) or 'état courant'}",
                    body=statement,
                    score=max(1, _relevance_score(task_terms, statement)),
                    origin="working_state",
                    extra={
                        "state_version": state_version,
                        "source_ref": {
                            "kind": "hypothesis",
                            "id": identifier,
                            "version": state_version,
                            "sha256": state_sha256,
                        },
                    },
                )
            )

    present_claims = {
        _as_text(item.get("claim_id")).casefold()
        for item in prompt_data.get("article_passages", ())
        if isinstance(item, Mapping)
    }
    absent_claims = sorted(explicit_claims - present_claims)
    if absent_claims and article_ref:
        candidates.append(
            _candidate(
                kind="article",
                identifier=article_ref,
                sha256=_as_text(prompt_data.get("article_sha256")),
                label=f"Article — passage {', '.join(absent_claims)} demandé",
                body=f"Passage {', '.join(absent_claims)} non présent dans le snapshot chargé.",
                score=100,
                origin="article_passage_unavailable",
            )
        )

    if consultation_type == "review_text":
        for item in _target_rows(prompt_data):
            opinion_id = _as_text(item.get("opinion_id"))
            text = _as_text(item.get("text"))
            if not opinion_id or not text:
                continue
            source_ref = item.get("source_ref")
            if not isinstance(source_ref, str):
                source_ref = f"opinion:{opinion_id}:v{item.get('opinion_version')}:"
                source_ref += (
                    f"{_as_text(item.get('opinion_sha256'))}:{_as_text(item.get('block_type'))}"
                )
            candidates.append(
                _candidate(
                    kind="opinion",
                    identifier=opinion_id,
                    sha256=_as_text(item.get("opinion_sha256")),
                    label=(f"Avis à relire — {_as_text(item.get('block_type'))}"),
                    body=text,
                    score=10_000,
                    origin="versioned_review_target",
                    opinion_version=item.get("opinion_version"),
                    extra={
                        "source_ref": source_ref,
                        "block_type": _as_text(item.get("block_type")),
                        "reviewed_text_sha256": _as_text(item.get("reviewed_text_sha256")),
                        "anchor": _as_text(item.get("anchor")),
                    },
                )
            )

    for item in prompt_data.get("opinion_sections", ()):
        if not isinstance(item, Mapping):
            continue
        title = _as_text(item.get("title"))
        text = _as_text(item.get("text"))
        score = _relevance_score(task_terms, f"{title} {text}")
        if score > 0:
            candidates.append(
                _candidate(
                    kind="opinion",
                    identifier=_as_text(item.get("opinion_id")),
                    sha256=_as_text(item.get("opinion_sha256")),
                    label=f"Avis — {title}",
                    body=text,
                    score=score,
                    origin="opinion_section",
                    opinion_version=item.get("opinion_version"),
                    extra={
                        key: item[key]
                        for key in ("block_type", "source_ref")
                        if isinstance(item.get(key), str)
                    },
                )
            )

    evidence_rows = [item for item in prompt_data.get("evidence", ()) if isinstance(item, Mapping)]
    for item in evidence_rows:
        identifier = _as_text(item.get("evidence_id"))
        excerpts = [row for row in item.get("content_excerpts", ()) if isinstance(row, Mapping)]
        measurement_rows = [
            row
            for row in prompt_data.get("measured_facts", ())
            if isinstance(row, Mapping) and str(row.get("evidence_id")) == identifier
        ]
        typed_measurements = [
            row
            for row in prompt_data.get("measurements", ())
            if isinstance(row, Mapping) and str(row.get("evidence_id")) == identifier
        ]
        pieces: list[str] = []
        included_excerpts = False
        label = _human_evidence_label(item, excerpts)
        row_score = _relevance_score(task_terms, label)
        for excerpt in excerpts:
            body = _as_text(excerpt.get("text"))
            title = _as_text(excerpt.get("title")) or "Extrait"
            score = _relevance_score(task_terms, f"{title} {body}")
            if score > 0:
                pieces.append(f"{title}:\n{_quote(body)}")
                included_excerpts = True
                row_score = max(row_score, score)
        for fact in (*measurement_rows, *typed_measurements):
            value = _as_text(fact.get("sample_material", fact.get("value")))
            if not value or value in {"…", "..."}:
                continue
            kind = _as_text(fact.get("kind")) or "mesure"
            offset = fact.get("offset")
            if type(offset) is int and offset >= 0:
                fact_text = f"{kind}, offset {offset}: {value}"
            else:
                fact_text = f"{kind}: {value}"
            score = _relevance_score(task_terms, fact_text)
            if _numeric_targets_conflict(relevance_text, fact_text):
                score = 0
            if score > 0 or ("observation", identifier) in explicitly_referenced:
                pieces.append(f"Mesure observée: {fact_text}")
                row_score = max(row_score, score, 1)
        if (
            ("observation", identifier) in explicitly_referenced
            and not included_excerpts
            and excerpts
        ):
            best = max(
                excerpts, key=lambda row: _relevance_score(task_terms, _as_text(row.get("text")))
            )
            pieces.append(
                f"{_as_text(best.get('title')) or 'Extrait'}:\n{_quote(_as_text(best.get('text')))}"
            )
        if pieces:
            candidates.append(
                _candidate(
                    kind="observation",
                    identifier=identifier,
                    sha256=_as_text(item.get("sha256")),
                    label=label,
                    body="\n\n".join(pieces),
                    score=max(
                        row_score, 1 if ("observation", identifier) in explicitly_referenced else 0
                    ),
                    origin="observation_content",
                    extra={
                        "source_ref": {
                            "kind": "observation",
                            "id": identifier,
                            "sha256": _as_text(item.get("sha256")),
                        },
                    },
                )
            )
        elif ("observation", identifier) in explicitly_referenced:
            unavailable_observations.add(identifier)

    for item in prompt_data.get("findings", ()):
        if not isinstance(item, Mapping):
            continue
        statement = _as_text(item.get("line", item.get("statement")))
        if not statement:
            continue
        finding_id = _as_text(item.get("finding_id"))
        score = _relevance_score(task_terms, statement)
        assessment = _as_text(item.get("assessment"))
        claim_refs = item.get("claim_refs", ())
        claim_text = (
            ", ".join(str(value) for value in claim_refs)
            if isinstance(claim_refs, (list, tuple))
            else ""
        )
        if (
            assessment == "refuted"
            and contradiction_required
            and _finding_matches_requested_claims(item, explicit_claims)
        ):
            score += 100
        if score > 0 or ("finding", finding_id) in explicitly_referenced:
            candidates.append(
                _candidate(
                    kind="finding",
                    identifier=finding_id,
                    sha256=_as_text(item.get("sha256")),
                    label=(
                        "Contradiction"
                        if assessment == "refuted"
                        else f"Constat — {_as_text(item.get('kind')) or 'observation'}"
                    ),
                    body="\n".join(
                        part
                        for part in (
                            f"Évaluation: {assessment}" if assessment else "",
                            f"Claims: {claim_text}" if claim_text else "",
                            "Observations: "
                            + ", ".join(str(value) for value in item.get("observation_refs", ()))
                            if isinstance(item.get("observation_refs", ()), (list, tuple))
                            and item.get("observation_refs")
                            else "",
                            statement,
                        )
                        if part
                    ),
                    score=max(score, 1 if ("finding", finding_id) in explicitly_referenced else 0),
                    origin="finding_statement",
                    extra={
                        "assessment": assessment,
                        "claim_refs": list(claim_refs)
                        if isinstance(claim_refs, (list, tuple))
                        else [],
                        "observation_refs": list(item.get("observation_refs", ()))
                        if isinstance(item.get("observation_refs", ()), (list, tuple))
                        else [],
                        "source_ref": {
                            "kind": "finding",
                            "id": finding_id,
                            "sha256": _as_text(item.get("sha256")),
                        },
                    },
                )
            )

    for item in prompt_data.get("measurements", ()):
        if not isinstance(item, Mapping):
            continue
        measurement_id = _as_text(item.get("measurement_id"))
        evidence_id = _as_text(item.get("evidence_id"))
        value = _as_text(item.get("value"))
        if not measurement_id or not value:
            continue
        kind = _as_text(item.get("kind")) or "mesure"
        offset = item.get("offset")
        sample_sha256 = _as_text(item.get("sample_sha256"))
        source_evidence_sha256 = _as_text(item.get("evidence_sha256"))
        body = f"{kind}, offset {offset}: {value}" if type(offset) is int else f"{kind}: {value}"
        score = _relevance_score(task_terms, body)
        if _numeric_targets_conflict(relevance_text, body):
            score = 0
        if (
            score > 0
            or ("observation", evidence_id) in explicitly_referenced
            or (
                consultation_type == "yara_draft"
                and not _numeric_targets_conflict(relevance_text, body)
            )
        ):
            measurement_sha = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            candidates.append(
                _candidate(
                    kind="measurement",
                    identifier=measurement_id,
                    sha256=measurement_sha,
                    label=f"Mesure — {kind}"
                    + (f", offset {offset}" if type(offset) is int else ""),
                    body=body,
                    score=max(
                        score, 1 if ("observation", evidence_id) in explicitly_referenced else 0
                    ),
                    origin="typed_measurement",
                    extra={
                        "measurement_id": measurement_id,
                        "evidence_id": evidence_id,
                        "evidence_sha256": source_evidence_sha256,
                        "sample_sha256": sample_sha256,
                        "offset": offset if type(offset) is int else None,
                        "length": item.get("length"),
                        "extractor_version": _as_text(item.get("extractor_version")),
                        "source_ref": {
                            "kind": "measurement",
                            "id": measurement_id,
                            "sha256": measurement_sha,
                        },
                    },
                )
            )

    for item in prompt_data.get("candidate_rules", ()):
        if not isinstance(item, Mapping):
            continue
        evidence_id = _as_text(item.get("evidence_id"))
        if not evidence_id:
            continue
        fields = {
            str(key): value
            for key, value in item.items()
            if key
            in {
                "source",
                "status",
                "false_positives",
                "false_negatives",
                "false_positives_added",
                "false_positives_removed",
                "fp",
                "fn",
                "candidate_id",
                "candidate_version",
                "corpus_sha256",
                "corpus_sha256s_by_role",
            }
            and isinstance(value, (str, int, float, bool, list, tuple, Mapping))
        }
        digest = _as_text(item.get("sha256"))
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            continue
        body = json.dumps(fields, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        candidates.append(
            _candidate(
                kind="rule",
                identifier=evidence_id,
                sha256=digest,
                label="État mesuré de règle et corpus",
                body=body,
                score=max(1, _relevance_score(task_terms, body)),
                origin="candidate_rule_summary",
                extra={
                    "source_ref": {
                        "kind": "observation",
                        "id": evidence_id,
                        "sha256": digest,
                    },
                    **{
                        key: item[key]
                        for key in (
                            "candidate_id",
                            "candidate_version",
                            "corpus_sha256",
                            "corpus_sha256s_by_role",
                        )
                        if key in item
                    },
                },
            )
        )

    for item in prompt_data.get("network_facts", ()):
        if not isinstance(item, Mapping):
            continue
        fact_id = _as_text(item.get("fact_id"))
        evidence_id = _as_text(item.get("evidence_id"))
        field = _as_text(item.get("field"))
        value = _as_text(item.get("value"))
        if not fact_id or not value:
            continue
        body = f"{field}: {value}" if field else value
        score = _relevance_score(task_terms, body)
        if (
            score > 0
            or ("observation", evidence_id) in explicitly_referenced
            or consultation_type == "suricata_draft"
        ):
            candidates.append(
                _candidate(
                    kind="fact",
                    identifier=fact_id,
                    sha256=_as_text(item.get("observation_sha256")),
                    label=f"Fait réseau — {field or 'mesure observée'}",
                    body=body,
                    score=max(
                        score, 1 if ("observation", evidence_id) in explicitly_referenced else 0
                    ),
                    origin="network_fact",
                    extra={
                        "fact_id": fact_id,
                        "evidence_id": evidence_id,
                        "evidence_sha256": _as_text(item.get("observation_sha256")),
                        "source_ref": {
                            "kind": "observation",
                            "id": evidence_id,
                            "sha256": _as_text(item.get("observation_sha256")),
                        },
                    },
                )
            )

    broad_planning_fallback = consultation_type in {
        "plan_investigation",
        "pivot_strategy",
    } and not any(
        item["kind"] in {"observation", "finding", "article", "measurement", "fact"}
        for item in candidates
    )
    if not candidates or broad_planning_fallback:
        # When lexical matching finds nothing, retain the upstream-selected
        # best source facts so broad tasks such as "which pivot next?" still
        # receive evidence to reason from.
        for item in evidence_rows:
            identifier = _as_text(item.get("evidence_id"))
            excerpts = [row for row in item.get("content_excerpts", ()) if isinstance(row, Mapping)]
            if excerpts:
                best = max(
                    excerpts,
                    key=lambda row: _relevance_score(
                        task_terms, f"{row.get('title', '')} {row.get('text', '')}"
                    ),
                )
                candidates.append(
                    _candidate(
                        kind="observation",
                        identifier=identifier,
                        sha256=_as_text(item.get("sha256")),
                        label=_human_evidence_label(item, excerpts),
                        body=f"{_as_text(best.get('title'))}:\n{_quote(_as_text(best.get('text')))}",
                        score=0,
                        origin="fallback_best_observation",
                    )
                )
            else:
                unavailable_observations.add(identifier)
        for item in prompt_data.get("measurements", ()):
            if not isinstance(item, Mapping) or not isinstance(item.get("measurement_id"), str):
                continue
            value = _as_text(item.get("value"))
            offset = item.get("offset")
            kind = _as_text(item.get("kind")) or "mesure"
            if not value:
                continue
            measurement_id = str(item["measurement_id"])
            digest = hashlib.sha256(
                json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                    "utf-8"
                )
            ).hexdigest()
            candidates.append(
                _candidate(
                    kind="measurement",
                    identifier=measurement_id,
                    sha256=digest,
                    label=f"Mesure — {kind}"
                    + (f", offset {offset}" if type(offset) is int else ""),
                    body=f"{kind}, offset {offset}: {value}"
                    if type(offset) is int
                    else f"{kind}: {value}",
                    score=0,
                    origin="fallback_best_measurement",
                    extra={
                        "measurement_id": measurement_id,
                        "evidence_id": _as_text(item.get("evidence_id")),
                        "evidence_sha256": _as_text(item.get("evidence_sha256")),
                    },
                )
            )

        for item in prompt_data.get("network_facts", ()):
            if not isinstance(item, Mapping) or not isinstance(item.get("fact_id"), str):
                continue
            value = _as_text(item.get("value"))
            if not value:
                continue
            fact_id = str(item["fact_id"])
            field = _as_text(item.get("field"))
            candidates.append(
                _candidate(
                    kind="fact",
                    identifier=fact_id,
                    sha256=_as_text(item.get("observation_sha256")),
                    label=f"Fait réseau — {field or 'mesure observée'}",
                    body=f"{field}: {value}" if field else value,
                    score=0,
                    origin="fallback_best_network_fact",
                    extra={
                        "fact_id": fact_id,
                        "evidence_id": _as_text(item.get("evidence_id")),
                        "evidence_sha256": _as_text(item.get("observation_sha256")),
                    },
                )
            )

        if broad_planning_fallback:
            fallback_proofs = [
                item
                for item in candidates
                if item["origin"].startswith("fallback_best_")
                and item["kind"] in {"observation", "measurement", "fact"}
            ]
            if fallback_proofs:
                best_fallback = min(
                    fallback_proofs,
                    key=lambda row: (-row["score"], row["label"].casefold(), row["body"]),
                )
                candidates = [item for item in candidates if item not in fallback_proofs]
                candidates.append(best_fallback)

    # Keep only task-relevant facts. Every explicitly cited source retains its
    # best available content card; there is no byte-based clipping here.
    selected: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    for item in sorted(
        candidates, key=lambda row: (-row["score"], row["label"].casefold(), row["body"])
    ):
        key = (item["kind"], item["id"], item["body"])
        if key not in seen:
            seen.add(key)
            selected.append(item)
    for kind, identifier in sorted(explicitly_referenced):
        if any(item["kind"] == kind and item["id"] == identifier for item in selected):
            continue
        available = [
            item for item in candidates if item["kind"] == kind and item["id"] == identifier
        ]
        if available:
            best = max(available, key=lambda row: row["score"])
            if best not in selected:
                selected.append(dict(best))
        elif kind == "observation":
            unavailable_observations.add(identifier)

    retained_excerpt_bytes = sum(
        len(f"[E] {item['label']}\n{_quote(item['body'])}".encode())
        for item in selected
        if item["kind"] == "observation"
    )
    if retained_excerpt_bytes > MAX_OBSERVATION_EXCERPT_BYTES:
        raise ContentExcerptLimitExceeded("dossier_content_excerpt_budget_exceeds_16384_bytes")

    # Keep a stable alias for each displayed fact. The mapping stays local and
    # carries only the data needed by output validation and provenance.
    manifest: list[dict[str, Any]] = []
    rendered: list[str] = []
    review_target_index = 0
    evidence_index = 0
    for item in selected:
        if item["origin"] == "versioned_review_target":
            review_target_index += 1
            alias = f"T{review_target_index}"
        else:
            evidence_index += 1
            alias = f"E{evidence_index}"
        piece = f"[{alias}] {item['label']}\n{_quote(item['body'])}"
        body_bytes = item["body"].encode("utf-8")
        piece_bytes = piece.encode("utf-8")
        manifest.append(
            {
                "alias": alias,
                "kind": item["kind"],
                "id": item["id"],
                "sha256": item["sha256"],
                "label": item["label"],
                "origin": item["origin"],
                **(
                    {"opinion_version": item["opinion_version"]}
                    if item.get("opinion_version")
                    else {}
                ),
                **item.get("extra", {}),
                "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
                "body_bytes": len(body_bytes),
                "rendered_sha256": hashlib.sha256(piece_bytes).hexdigest(),
                "rendered_bytes": len(piece_bytes),
            }
        )
        rendered.append(piece)

    lines = ["DOSSIER DE FAITS"]
    if rendered:
        lines.extend(rendered)
    else:
        lines.append("Aucun fait source pertinent n\u2019a été chargé pour cette tâche.")

    # One precise note replaces one placeholder card per unavailable source.
    if unavailable_observations:
        lines.append(
            "Limite pertinente: contenu lisible indisponible pour "
            f"{len(unavailable_observations)} observation(s) citée(s)."
        )

    return AdvisorDossier(text="\n\n".join(lines), manifest=tuple(manifest))


def evaluate_advisor_evidence_preflight(
    prompt_data: Mapping[str, Any],
    dossier: AdvisorDossier,
    prompt: str,
    *,
    submitted: bool = False,
) -> dict[str, Any]:
    """Report which canonical proof entered the dossier and exact Bridge payload."""
    consultation_type = _as_text(prompt_data.get("consultation_type"))
    task = "\n".join(
        [_as_text(prompt_data.get("question"))]
        + [_as_text(item.get("text")) for item in _target_rows(prompt_data)]
    )
    claim_ids = _requested_claim_ids(prompt_data, task)
    contradiction_required = _contradiction_is_required(prompt_data, task)
    references = set(_explicit_reference_ids(prompt_data))
    records: list[dict[str, Any]] = []
    missing_refs: list[dict[str, str]] = []

    def add(
        kind: str,
        identifier: str,
        sha256: str,
        source_ref: object,
        body_present: bool,
        required: bool,
        reason: str,
        **details: Any,
    ) -> None:
        records.append(
            {
                "kind": kind,
                "id": identifier,
                "sha256": sha256,
                "source_ref": source_ref,
                "body_present": body_present,
                "required": required,
                "reason": reason,
                **details,
            }
        )

    article_ref = _as_text(prompt_data.get("article_source_ref"))
    article_id = _article_identity(prompt_data)
    article_sha = _as_text(prompt_data.get("article_sha256"))
    for item in prompt_data.get("article_passages", ()):
        if not isinstance(item, Mapping):
            continue
        claim_id = _as_text(item.get("claim_id"))
        add(
            "article",
            article_id or f"article:{article_sha}",
            article_sha,
            article_ref,
            bool(_as_text(item.get("text"))),
            bool(claim_id and claim_id.casefold() in claim_ids)
            or (
                consultation_type in {"plan_investigation", "pivot_strategy", "figure_plan"}
                and _relevance_score(
                    _terms(task),
                    f"{claim_id} {_as_text(item.get('heading'))} {_as_text(item.get('text'))}",
                )
                > 0
            ),
            "Question or target cites or selects this claim passage."
            if claim_id.casefold() in claim_ids
            or (
                consultation_type in {"plan_investigation", "pivot_strategy", "figure_plan"}
                and _relevance_score(
                    _terms(task),
                    f"{claim_id} {_as_text(item.get('heading'))} {_as_text(item.get('text'))}",
                )
                > 0
            )
            else "Passage is outside the explicit question references.",
            claim_id=claim_id,
            section_index=item.get("section_index"),
            offset=item.get("offset"),
            verified=bool(re.fullmatch(r"[0-9a-f]{64}", article_sha)),
        )

    hypotheses = [item for item in prompt_data.get("hypotheses", ()) if isinstance(item, Mapping)]
    if consultation_type in {"plan_investigation", "pivot_strategy"}:
        active = [item for item in hypotheses if _as_text(item.get("status")) != "abandoned"]
        if not active:
            add(
                "hypothesis",
                "working-state",
                _as_text(prompt_data.get("working_state_sha256")),
                prompt_data.get("working_state_ref"),
                False,
                True,
                "No current hypothesis was loaded from canonical working state.",
            )
        for item in active:
            identifier = _as_text(item.get("id"))
            state_sha = _as_text(prompt_data.get("working_state_sha256"))
            version = prompt_data.get("working_state_version")
            add(
                "hypothesis",
                identifier,
                state_sha,
                {"kind": "hypothesis", "id": identifier, "version": version, "sha256": state_sha},
                bool(_as_text(item.get("statement"))),
                True,
                "A current non-abandoned hypothesis is required for planning or pivot comparison.",
                state_version=version,
                verified=bool(re.fullmatch(r"[0-9a-f]{64}", state_sha)) and type(version) is int,
            )

    for key, kind, id_key in (
        ("evidence", "observation", "evidence_id"),
        ("findings", "finding", "finding_id"),
    ):
        rows = [item for item in prompt_data.get(key, ()) if isinstance(item, Mapping)]
        for item in rows:
            identifier = _as_text(item.get(id_key))
            sha = _as_text(item.get("sha256"))
            if kind == "observation":
                excerpts = item.get("content_excerpts", ())
                has_body = bool(
                    isinstance(excerpts, (list, tuple))
                    and any(
                        isinstance(row, Mapping) and _as_text(row.get("text")) for row in excerpts
                    )
                ) or any(
                    isinstance(row, Mapping)
                    and _as_text(row.get("evidence_id")) == identifier
                    and _as_text(row.get("sample_material", row.get("value")))
                    for row in prompt_data.get("measured_facts", ())
                )
            else:
                has_body = bool(_as_text(item.get("line", item.get("statement"))))
            explicitly_referenced = (kind, identifier) in references
            canonical_text = _as_text(item.get("line", item.get("statement")))
            if kind == "observation":
                canonical_text += " " + " ".join(
                    _as_text(row.get("text"))
                    for row in item.get("content_excerpts", ())
                    if isinstance(row, Mapping)
                )
            dossier_selected = any(
                row.get("kind") == kind and row.get("id") == identifier for row in dossier.manifest
            )
            relevant = _relevance_score(_terms(task), canonical_text) > 0
            selected_for_type = (
                consultation_type
                in {
                    "plan_investigation",
                    "pivot_strategy",
                    "figure_plan",
                }
                and has_body
                and (dossier_selected or relevant)
            )
            is_contradiction = item.get(
                "assessment"
            ) == "refuted" and _finding_matches_requested_claims(item, claim_ids)
            required = (
                explicitly_referenced
                or selected_for_type
                or (contradiction_required and kind == "finding" and is_contradiction)
            )
            add(
                kind,
                identifier,
                sha,
                {"kind": kind, "id": identifier, "sha256": sha},
                has_body,
                required,
                "Explicitly referenced by the question or target."
                if explicitly_referenced
                else "Canonical proof selected for this consultation type."
                if selected_for_type
                else "Refuted finding is required to represent the requested contradiction."
                if required
                else "Canonical record is not required by this question.",
                assessment=item.get("assessment"),
                claim_refs=list(item.get("claim_refs", ()))
                if isinstance(item.get("claim_refs", ()), (list, tuple))
                else [],
                verified=bool(re.fullmatch(r"[0-9a-f]{64}", sha)),
                blob_read_verified=item.get("blob_read_verified"),
                retrieval_available=(
                    kind == "observation"
                    and bool(identifier)
                    and bool(re.fullmatch(r"[0-9a-f]{64}", sha))
                    and item.get("blob_read_verified") is True
                ),
            )

    canonical_observation_ids = {item["id"] for item in records if item["kind"] == "observation"}
    for item in (
        *[row for row in prompt_data.get("measurements", ()) if isinstance(row, Mapping)],
        *[row for row in prompt_data.get("network_facts", ()) if isinstance(row, Mapping)],
    ):
        identifier = _as_text(item.get("evidence_id"))
        if not identifier or identifier in canonical_observation_ids:
            continue
        sha = _as_text(item.get("evidence_sha256", item.get("observation_sha256")))
        add(
            "observation",
            identifier,
            sha,
            {"kind": "observation", "id": identifier, "sha256": sha},
            bool(_as_text(item.get("value"))),
            ("observation", identifier) in references
            or consultation_type in {"pivot_strategy", "yara_draft", "suricata_draft"},
            "Canonical measured value carries this observation reference and SHA.",
            verified=bool(re.fullmatch(r"[0-9a-f]{64}", sha)),
        )
        canonical_observation_ids.add(identifier)

    opinion_targets: list[Mapping[str, Any]] = [
        item for item in prompt_data.get("units", ()) if isinstance(item, Mapping)
    ]
    legacy_target = prompt_data.get("review_text")
    if isinstance(legacy_target, Mapping):
        opinion_targets.append(legacy_target)
    if consultation_type == "review_text" and not opinion_targets:
        add(
            "opinion",
            "review-target",
            "",
            None,
            False,
            True,
            "No versioned opinion target was loaded for review.",
        )
    for item in opinion_targets:
        identifier = _as_text(item.get("opinion_id"))
        if not identifier:
            continue
        add(
            "opinion",
            identifier,
            _as_text(item.get("opinion_sha256")),
            item.get("source_ref"),
            bool(_as_text(item.get("text"))),
            consultation_type == "review_text",
            "Exact opinion version, SHA, and target text are required for review."
            if consultation_type == "review_text"
            else "Opinion target is not required by this question.",
            opinion_version=item.get("opinion_version"),
            block_type=item.get("block_type"),
            reviewed_text_sha256=item.get("reviewed_text_sha256"),
            verified=(
                type(item.get("opinion_version")) is int
                and bool(re.fullmatch(r"[0-9a-f]{64}", _as_text(item.get("opinion_sha256"))))
                and _as_text(item.get("reviewed_text_sha256"))
                == hashlib.sha256(_as_text(item.get("text")).encode("utf-8")).hexdigest()
            ),
        )

    measurements = [
        item for item in prompt_data.get("measurements", ()) if isinstance(item, Mapping)
    ]
    if consultation_type == "yara_draft" and not measurements:
        add(
            "measurement",
            "verified-measurement",
            "",
            None,
            False,
            True,
            "No verified measurement with sample SHA and offset was loaded for YARA drafting.",
        )
    for item in measurements:
        identifier = _as_text(item.get("measurement_id"))
        digest = hashlib.sha256(
            json.dumps(item, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
                "utf-8"
            )
        ).hexdigest()
        complete = (
            bool(_as_text(item.get("sample_sha256")))
            and type(item.get("offset")) is int
            and bool(_as_text(item.get("value")))
            and bool(_as_text(item.get("evidence_id")))
            and bool(_as_text(item.get("evidence_sha256")))
        )
        add(
            "measurement",
            identifier,
            digest,
            {"kind": "measurement", "id": identifier, "sha256": digest},
            bool(_as_text(item.get("value"))),
            consultation_type in {"yara_draft", "pivot_strategy"},
            "Sample SHA, offset, value, and source observation SHA are required for YARA drafting."
            if consultation_type == "yara_draft"
            else "Measurement is not required by this question.",
            sample_sha256=item.get("sample_sha256"),
            offset=item.get("offset"),
            evidence_id=item.get("evidence_id"),
            evidence_sha256=item.get("evidence_sha256"),
            verified=(
                bool(re.fullmatch(r"[0-9a-f]{64}", _as_text(item.get("sample_sha256"))))
                and type(item.get("offset")) is int
                and item.get("offset", -1) >= 0
                and bool(_as_text(item.get("value")))
                and bool(_as_text(item.get("evidence_id")))
                and bool(re.fullmatch(r"[0-9a-f]{64}", _as_text(item.get("evidence_sha256"))))
            ),
        )

    network_facts = [
        item for item in prompt_data.get("network_facts", ()) if isinstance(item, Mapping)
    ]
    if consultation_type == "suricata_draft" and not network_facts:
        add(
            "fact",
            "network-fact",
            "",
            None,
            False,
            True,
            "No measured network fact was loaded for Suricata drafting.",
        )
    for item in network_facts:
        identifier = _as_text(item.get("fact_id"))
        sha = _as_text(item.get("observation_sha256"))
        add(
            "fact",
            identifier,
            sha,
            {"kind": "observation", "id": _as_text(item.get("evidence_id")), "sha256": sha},
            bool(_as_text(item.get("value"))),
            consultation_type == "suricata_draft",
            "Measured network value and source observation SHA are required for Suricata drafting."
            if consultation_type == "suricata_draft"
            else "Network fact is not required by this question.",
            evidence_id=item.get("evidence_id"),
            verified=bool(re.fullmatch(r"[0-9a-f]{64}", sha)),
        )

    for item in prompt_data.get("candidate_rules", ()):
        if not isinstance(item, Mapping):
            continue
        identifier = _as_text(item.get("evidence_id"))
        sha = _as_text(item.get("sha256"))
        metadata_available = any(
            item.get(key) is not None
            for key in (
                "candidate_id",
                "candidate_version",
                "corpus_sha256",
                "corpus_sha256s_by_role",
            )
        )
        add(
            "rule",
            identifier,
            sha,
            {"kind": "observation", "id": identifier, "sha256": sha},
            bool(_as_text(item.get("status"))) or metadata_available,
            consultation_type == "yara_draft" and metadata_available,
            "Available rule version and corpus metadata accompany this YARA consultation."
            if metadata_available
            else "No canonical rule version or corpus fingerprint was available.",
            verified=bool(re.fullmatch(r"[0-9a-f]{64}", sha)),
            candidate_id=item.get("candidate_id"),
            candidate_version=item.get("candidate_version"),
            corpus_sha256=item.get("corpus_sha256"),
            corpus_sha256s_by_role=item.get("corpus_sha256s_by_role"),
        )

    assertion_checks: list[dict[str, Any]] = []
    if consultation_type == "draft_text":
        assertions = [
            item for item in prompt_data.get("assertions", ()) if isinstance(item, Mapping)
        ]
        for index, item in enumerate(assertions, start=1):
            refs = [
                ref
                for ref in item.get("refs", ())
                if isinstance(ref, Mapping)
                and ref.get("kind") in {"observation", "finding"}
                and isinstance(ref.get("sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", ref.get("sha256", ""))
            ]
            alias_visible = f"CIBLE [A{index}]" in prompt
            complete = bool(_as_text(item.get("id")) and _as_text(item.get("text")) and refs)
            sent = (
                submitted
                and complete
                and alias_visible
                and all(
                    (str(ref.get("kind")), _as_text(ref.get("id"))) in references for ref in refs
                )
            )
            assertion_checks.append(
                {
                    "id": _as_text(item.get("id")),
                    "required": True,
                    "present": complete,
                    "sent": sent,
                    "reference_count": len(refs),
                    "reason": (
                        "Validated assertion, text, and canonical references are present."
                        if complete
                        else "Assertion text or a SHA-verified canonical reference is missing."
                    ),
                }
            )
            if not complete or not alias_visible or (submitted and not sent):
                missing_refs.append(
                    {
                        "ref": f"assertion:{_as_text(item.get('id')) or index}",
                        "reason": (
                            "Validated assertion and its canonical references must be rendered "
                            "in the draft prompt."
                        ),
                    }
                )
        if not assertions:
            missing_refs.append(
                {
                    "ref": "assertions:draft_text",
                    "reason": (
                        "Draft text requires at least one validated assertion with canonical "
                        "references."
                    ),
                }
            )

    if consultation_type == "figure_plan" and not any(
        isinstance(item, Mapping) and _as_text(item.get("finding_id"))
        for item in prompt_data.get("findings", ())
    ):
        add(
            "finding",
            "figure-findings",
            "",
            None,
            False,
            True,
            "Figure planning requires at least one selected canonical finding.",
        )

    proof_kinds = {"observation", "finding", "article", "measurement", "fact"}
    if consultation_type in {"plan_investigation", "pivot_strategy", "figure_plan"} and not any(
        item["required"]
        and item["kind"] in proof_kinds
        and (item["body_present"] or item.get("retrieval_available"))
        for item in records
    ):
        add(
            "observation",
            "question-proof",
            "",
            None,
            False,
            True,
            (
                "No canonical observation, finding, or claim passage body is available for "
                "this question."
            ),
        )
    if contradiction_required:
        if claim_ids:
            covered_claims = {
                claim
                for item in records
                if item["kind"] == "finding" and item.get("assessment") == "refuted"
                for claim in claim_ids
                if _finding_matches_requested_claims(item, {claim})
            }
            for claim_id in sorted(claim_ids - covered_claims):
                add(
                    "contradiction",
                    f"required-contradiction:{claim_id}",
                    "",
                    {"kind": "claim", "id": claim_id},
                    False,
                    True,
                    f"No refuted finding is linked to requested claim passage {claim_id}.",
                    claim_id=claim_id,
                )
        elif not any(
            item["kind"] == "finding" and item.get("assessment") == "refuted" for item in records
        ):
            add(
                "contradiction",
                "required-contradiction",
                "",
                None,
                False,
                True,
                "No refuted finding was loaded for the requested contradiction.",
            )

    known = {(item["kind"], item["id"]) for item in records}
    for kind, identifier in sorted(references - known):
        add(
            kind,
            identifier,
            "",
            {"kind": kind, "id": identifier},
            False,
            True,
            "Explicit source reference has no canonical body in this consultation snapshot.",
        )
    for claim_id in sorted(claim_ids):
        if not any(item.get("claim_id", "").casefold() == claim_id for item in records):
            add(
                "article",
                article_id or f"article:{article_sha}",
                article_sha,
                article_ref or None,
                False,
                True,
                f"Claim passage {claim_id} is referenced but absent from the loaded snapshot.",
                claim_id=claim_id,
            )

    dossier_rows = list(dossier.manifest)
    for item in records:
        sha_valid = bool(re.fullmatch(r"[0-9a-f]{64}", item["sha256"]))
        matches = [
            row
            for row in dossier_rows
            if (
                (row.get("kind") == item["kind"] and row.get("id") == item["id"])
                or (
                    item["kind"] == "observation"
                    and row.get("kind") in {"measurement", "fact"}
                    and row.get("evidence_id") == item["id"]
                )
                or (item["kind"] == "fact" and row.get("fact_id") == item["id"])
            )
            and (not item.get("claim_id") or row.get("claim_id") == item.get("claim_id"))
        ]
        statuses: list[dict[str, str]] = []

        def mark(
            status: AdvisorEvidenceStatus,
            reason: str,
            *,
            output: list[dict[str, str]] = statuses,
        ) -> None:
            output.append({"status": status.value, "reason": reason})

        if item["required"]:
            mark(AdvisorEvidenceStatus.REQUIRED, item["reason"])
        verified = item.get("verified", True)
        if item["body_present"] and sha_valid and verified:
            mark(
                AdvisorEvidenceStatus.PRESENT_IN_CANONICAL,
                "Canonical body is available with a valid SHA-256 in this consultation snapshot.",
            )
        elif item["body_present"]:
            mark(
                AdvisorEvidenceStatus.NOT_VERIFIED,
                (
                    "Body is present but its canonical SHA-256 or required versioned metadata "
                    "is invalid."
                ),
            )
        elif item["required"]:
            mark(
                AdvisorEvidenceStatus.MISSING,
                "A required reference without its body is not a present proof.",
            )
        if matches:
            mark(
                AdvisorEvidenceStatus.IN_DOSSIER, "Source piece is present in the rendered dossier."
            )
            payload_contains_dossier = dossier.text in prompt
            if (
                submitted
                and payload_contains_dossier
                and any(
                    _as_text(row.get("alias"))
                    and f"[{row['alias']}] {row.get('label', '')}" in prompt
                    for row in matches
                )
            ):
                mark(
                    AdvisorEvidenceStatus.SENT_TO_BRIDGE,
                    (
                        "The exact rendered dossier and source alias occur in the submitted "
                        "transport payload."
                    ),
                )
            elif submitted and item["required"]:
                missing_refs.append(
                    {
                        "ref": _display_source_ref(item["source_ref"] or item["id"]),
                        "reason": (
                            "Required dossier piece is not visible in the exact transport payload."
                        ),
                    }
                )
        elif item["body_present"] and sha_valid and verified and item["required"]:
            mark(
                AdvisorEvidenceStatus.RETRIEVABLE,
                "Canonical proof is available but was not selected into this dossier.",
            )
            missing_refs.append(
                {
                    "ref": _display_source_ref(item["source_ref"] or item["id"]),
                    "reason": "Required canonical proof was omitted from the dossier.",
                }
            )
        elif item["body_present"] and sha_valid and not item["required"]:
            mark(
                AdvisorEvidenceStatus.NOT_VERIFIED,
                (
                    "The bounded lexical selection did not include this proof; semantic "
                    "relevance to the question was not verified."
                ),
            )
            mark(
                AdvisorEvidenceStatus.INTENTIONALLY_OMITTED,
                (
                    "Canonical proof was omitted by bounded lexical selection; this omission "
                    "does not establish semantic irrelevance."
                ),
            )
        elif item["required"] and item.get("retrieval_available") and not item["body_present"]:
            mark(
                AdvisorEvidenceStatus.RETRIEVABLE,
                (
                    "Canonical observation reference and SHA exist; retrieve and verify its "
                    "body before submission."
                ),
            )
            missing_refs.append(
                {
                    "ref": _display_source_ref(item["source_ref"] or item["id"]),
                    "reason": (
                        "Canonical reference is known but its content body was not loaded into "
                        "the dossier."
                    ),
                }
            )
        elif (
            item["required"]
            and item["kind"] == "observation"
            and item.get("blob_read_verified") is False
        ):
            mark(
                AdvisorEvidenceStatus.NOT_VERIFIED,
                "The canonical blob reader did not verify that this source can be read.",
            )
            missing_refs.append(
                {
                    "ref": _display_source_ref(item["source_ref"] or item["id"]),
                    "reason": (
                        "Required canonical blob is unreadable or its read capability was not "
                        "verified."
                    ),
                }
            )
        elif item["required"]:
            missing_refs.append(
                {
                    "ref": _display_source_ref(item["source_ref"] or item["id"]),
                    "reason": item["reason"],
                }
            )
        if item["required"] and any(
            row["status"] == AdvisorEvidenceStatus.NOT_VERIFIED.value for row in statuses
        ):
            missing_refs.append(
                {
                    "ref": _display_source_ref(item["source_ref"] or item["id"]),
                    "reason": "Required canonical proof lacks verifiable metadata.",
                }
            )
        elif (
            not item["required"]
            and item["kind"] == "observation"
            and item.get("blob_read_verified") is False
        ):
            mark(
                AdvisorEvidenceStatus.NOT_VERIFIED,
                "The optional observation blob reader failed; its body could not be verified.",
            )
            mark(
                AdvisorEvidenceStatus.INTENTIONALLY_OMITTED,
                "This optional reference was omitted after its canonical blob read failed.",
            )
        elif not item["required"] and not item["body_present"]:
            mark(
                AdvisorEvidenceStatus.INTENTIONALLY_OMITTED,
                "This unrequired reference has no body loaded in the consultation snapshot.",
            )
        item["statuses"] = statuses

    required_items = [item for item in records if item["required"]]
    retrievable_required = any(
        any(row["status"] == AdvisorEvidenceStatus.RETRIEVABLE.value for row in item["statuses"])
        for item in required_items
    )
    unverifiable_required = any(
        any(row["status"] == AdvisorEvidenceStatus.NOT_VERIFIED.value for row in item["statuses"])
        for item in required_items
    )
    if unverifiable_required:
        status = AdvisorPreflightStatus.TECHNICAL_BLOCKED
    elif retrievable_required:
        status = AdvisorPreflightStatus.NEEDS_RETRIEVAL
    elif missing_refs:
        status = AdvisorPreflightStatus.INSUFFICIENT_EVIDENCE
    else:
        status = AdvisorPreflightStatus.READY
    counts = {
        "required": len(required_items),
        "present": sum(
            any(
                row["status"] == AdvisorEvidenceStatus.PRESENT_IN_CANONICAL.value
                for row in item["statuses"]
            )
            for item in records
        ),
        "sent": sum(
            any(
                row["status"] == AdvisorEvidenceStatus.SENT_TO_BRIDGE.value
                for row in item["statuses"]
            )
            for item in records
        ),
        "omitted": sum(
            any(
                row["status"] == AdvisorEvidenceStatus.INTENTIONALLY_OMITTED.value
                for row in item["statuses"]
            )
            for item in records
        ),
        "missing": len(missing_refs),
        "assertions_required": len(assertion_checks),
        "assertions_present": sum(item["present"] for item in assertion_checks),
        "assertions_sent": sum(item["sent"] for item in assertion_checks),
    }
    items = [
        {key: value for key, value in item.items() if key not in {"body_present", "reason"}}
        for item in records
    ]
    return {
        "status": status.value,
        "missing_refs": missing_refs,
        "counts": counts,
        "assertions": assertion_checks,
        "items": items,
    }


def _candidate(
    *,
    kind: str,
    identifier: str,
    sha256: str,
    label: str,
    body: str,
    score: int,
    origin: str,
    opinion_version: object = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "kind": kind,
        "id": identifier,
        "sha256": sha256,
        "label": _clean_label(label),
        "body": body,
        "score": score,
        "origin": origin,
        "opinion_version": opinion_version,
        "extra": dict(extra or {}),
    }


def _target_rows(data: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    rows = [item for item in data.get("units", ()) if isinstance(item, Mapping)]
    legacy = data.get("review_text")
    if isinstance(legacy, Mapping):
        rows.append(legacy)
    return rows


def _explicit_reference_ids(data: Mapping[str, Any]) -> list[tuple[str, str]]:
    result: list[tuple[str, str]] = []
    for unit in _target_rows(data):
        for raw_id in unit.get("evidence_refs", ()):
            if isinstance(raw_id, str) and raw_id:
                row = ("observation", raw_id)
                if row not in result:
                    result.append(row)
        for raw_id in unit.get("finding_refs", ()):
            if isinstance(raw_id, str) and raw_id:
                row = ("finding", raw_id)
                if row not in result:
                    result.append(row)
    for item in data.get("assertions", ()):
        if not isinstance(item, Mapping):
            continue
        for ref in item.get("refs", ()):
            if isinstance(ref, Mapping) and ref.get("kind") in {"observation", "finding"}:
                row = (str(ref["kind"]), _as_text(ref.get("id")))
                if row not in result:
                    result.append(row)
    for item in data.get("findings", ()):
        if not isinstance(item, Mapping):
            continue
        finding_id = _as_text(item.get("finding_id"))
        if ("finding", finding_id) not in result:
            continue
        for raw_id in item.get("observation_refs", ()):
            if isinstance(raw_id, str) and raw_id:
                row = ("observation", raw_id)
                if row not in result:
                    result.append(row)
    for key, kind in (
        ("evidence_refs", "observation"),
        ("observation_refs", "observation"),
        ("finding_refs", "finding"),
    ):
        for raw_id in data.get(key, ()):
            if isinstance(raw_id, str) and raw_id:
                row = (kind, raw_id)
                if row not in result:
                    result.append(row)
    return result


def _article_identity(data: Mapping[str, Any]) -> str:
    source_ref = _as_text(data.get("article_source_ref"))
    return source_ref.partition(":")[2] if source_ref.startswith("article:") else source_ref


def _human_evidence_label(item: Mapping[str, Any], excerpts: Sequence[Mapping[str, Any]]) -> str:
    source = _as_text(item.get("source"))
    source_folded = source.casefold()
    if "ilspy" in source_folded or "dotnet" in source_folded:
        product = "ILSpy"
    elif "floss" in source_folded or "string" in source_folded:
        product = "Strings décodées"
    elif "local analysis" in source_folded or "local." in source_folded:
        product = "Analyse locale"
    elif source_folded == "vt.download_sample":
        product = "Échantillon téléchargé"
    elif source_folded == "virustotal" or source_folded.startswith("vt."):
        product = "VirusTotal"
    else:
        # Unknown provider/tool identifiers are provenance details, not task
        # facts. Keep them in the local manifest and show a neutral label.
        product = "Observation"
    title = next((_as_text(row.get("title")) for row in excerpts if _as_text(row.get("title"))), "")
    if title:
        title = re.sub(r"^(?:Méthode|Valeur décodée|Texte observé)\s*", "", title, flags=re.I)
        return _clean_label(f"{product} — {title}")
    object_type = _as_text(item.get("object_type"))
    suffix = "fichier" if object_type in {"file", "sample"} else "observation"
    return f"{product} — {suffix}"


def _quote(text: str) -> str:
    return "\n".join(f"| {line}" if line else "|" for line in text.split("\n"))


def _clean_label(value: str) -> str:
    value = _UUID.sub("", value)
    value = _INTERNAL_SHA.sub("empreinte interne", value)
    return " ".join(value.split()).strip(" —")


def _as_text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _display_source_ref(value: object) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _as_text(value) or str(value)


def _terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        token
        for token in _WORD.findall(normalized)
        if len(token) > 2 and token not in _STOP and not token.isdigit()
    }


def _relevance_score(terms: set[str], value: str) -> int:
    if not terms:
        return 0
    candidate = _terms(value)
    return sum(2 if term in candidate else 0 for term in terms)


def _numeric_targets_conflict(task: str, candidate: str) -> bool:
    requested = set(re.findall(r"\b\d{3,}\b", task))
    actual = set(re.findall(r"\b\d{3,}\b", candidate))
    return bool(requested and requested.isdisjoint(actual))


def _requires_contradiction(task: str) -> bool:
    terms = _terms(task)
    return bool(
        terms
        & {
            "contradiction",
            "contradictions",
            "contradictionnel",
            "contradictoire",
            "contredit",
            "infirm",
            "opposé",
            "oppose",
            "réfute",
            "refute",
            "réfutation",
            "refutation",
        }
    )


def _requested_claim_ids(data: Mapping[str, Any], task: str) -> set[str]:
    claims = {value.casefold() for value in _CLAIM_ID.findall(task)}
    for item in data.get("assertions", ()):
        if isinstance(item, Mapping) and item.get("nature") == "contradiction":
            claims.update(
                value.casefold() for value in _CLAIM_ID.findall(_as_text(item.get("text")))
            )
    return claims


def _contradiction_is_required(data: Mapping[str, Any], task: str) -> bool:
    return _requires_contradiction(task) or any(
        isinstance(item, Mapping) and item.get("nature") == "contradiction"
        for item in data.get("assertions", ())
    )


def _finding_matches_requested_claims(item: Mapping[str, Any], claims: set[str]) -> bool:
    if not claims:
        return True
    references = item.get("claim_refs", ())
    if not isinstance(references, (list, tuple)):
        return False
    return bool({_as_text(value).casefold() for value in references} & claims)


__all__ = ["AdvisorDossier", "build_advisor_dossier", "extract_content_excerpts"]
