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
from dataclasses import dataclass, field
from typing import Any

from cti_app.application.publication_secret_guard import is_operational_secret_field_name
from cti_app.domain.advisor_completeness import (
    AdvisorEvidenceStatus,
    AdvisorPreflightStatus,
)
from cti_app.domain.investigation import InvestigationEnvelope

_WORD = re.compile(r"[\w.+#/-]+", re.UNICODE)
_CLAIM_ID = re.compile(r"\b(?:S\d+P\d+(?:\.\d+)?|L\d+(?:\.\d+)?|T\d+)\b", re.I)
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
_FINDING_REFERENCE_ID = re.compile(
    r"(?i)\b(?:finding|constat|r[eé]futation|contradiction)\s*(?:id\s*)?[:#]?\s*"
    r"([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\b"
)
_INTERNAL_SHA = re.compile(r"(?i)\b(?:sha(?:-?256)?\s*[:=]\s*)?[0-9a-f]{64}\b")
_STOP = frozenset(
    "avec dans pour sans cette celui celle sont être avoir mais comme dans sur sous une des les "
    "aux par que qui quoi dont alors leur leurs elles ils cela ceci nous vous elle lui son sa "
    "ses from that this those have with which what where when then than into over under".split()
)


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
        "raw",
        "raw_bytes",
        "bytes",
        "byte",
        "base64",
        "base64_bytes",
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
_CTI_METADATA_KEYS = frozenset(
    {
        "sha256",
        "sha256_short",
        "sample_sha256",
        "md5",
        "sha1",
        "hashes",
        "mime_type",
        "content_type",
        "byte_length",
        "size",
        "size_bytes",
        "offset",
        "status",
        "analysis_status",
        "engine",
        "engines",
        "trace",
        "traces",
        "fragment",
        "fragments",
        "association",
        "related_sample_sha256",
        "related_sample_hashes",
    }
)
_HASH_METADATA_KEYS = frozenset(
    {"sha256", "sha256_short", "sample_sha256", "md5", "sha1", "hashes"}
)
_GENERIC_METADATA_KEYS = frozenset(
    {
        "name",
        "title",
        "source",
        "source_version",
        "object_type",
        "object_id",
        "type",
        "kind",
        "schema",
        "schema_version",
        "tool_name",
        "operation_key",
        "invocation_id",
        "tool_id",
        "id",
        "uuid",
        "blob_id",
        "status",
        "reason",
        "duration",
        "duration_ms",
        "byte_size",
        "size",
        "size_bytes",
        "mime_type",
        "content_type",
        "offset",
        "length",
        "byte_length",
        "encoding",
        "warnings",
        "limitations",
        "truncated",
        "observed_at",
        "timestamp",
        "engine",
        "engines",
        "trace",
        "traces",
        "fragment",
        "fragments",
        "association",
        "associations",
    }
)


@dataclass(frozen=True, slots=True)
class AdvisorDossier:
    text: str
    manifest: tuple[dict[str, Any], ...]
    _encoded: bytes = field(init=False, repr=False, compare=False)
    _digest: str = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        encoded = self.text.encode("utf-8")
        object.__setattr__(self, "_encoded", encoded)
        object.__setattr__(self, "_digest", hashlib.sha256(encoded).hexdigest())

    @property
    def byte_size(self) -> int:
        return len(self._encoded)

    @property
    def sha256(self) -> str:
        return self._digest


def extract_content_excerpts(
    payload: Mapping[str, Any], *, include_unclassified: bool = False
) -> list[dict[str, str]]:
    """Extract complete observed methods, text, decoded values, and byte values."""
    sample_context = _contains_sample_context(payload)
    excerpts: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()
    offset_rows: list[tuple[str, str, str]] = []
    offset_row_identities: set[tuple[str, int | None, str]] = set()
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

    def add(kind: str, title: str, value: object) -> None:
        if not isinstance(value, str) or not value.strip():
            return
        text = value
        prefix = (
            f"{assembly_name} — "
            if assembly_name and assembly_name.casefold() not in title.casefold()
            else ""
        )
        identity = (kind, title.strip(), text)
        if identity in seen:
            return
        rendered_title = f"{prefix}{title.strip()}"
        seen.add(identity)
        excerpts.append({"kind": kind, "title": rendered_title, "text": text})

    def add_unclassified_scalar(value: object, field_path: tuple[str, ...]) -> None:
        if isinstance(value, str):
            text = value
        elif isinstance(value, (bool, int, float)):
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        else:
            return
        title = ".".join(part.replace("_", " ") for part in field_path)
        add("text", f"Valeur observée — {title or 'contenu'}", text)

    def display_name(row: Mapping[str, Any], fallback: str) -> str:
        for key in ("name", "method", "function", "symbol", "member", "key"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    def add_short_hex_rows(
        value: object, *, kind: str = "hex", inherited_offset: object = None
    ) -> None:
        rows = (
            (value,)
            if _lossless_bytes(value) is not None
            else value
            if isinstance(value, (list, tuple))
            else (value,)
        )
        for row in rows:
            raw_container = (
                row.get(
                    "value",
                    row.get(
                        "content_base64",
                        row.get(
                            "base64",
                            row.get("bytes", row.get("hex", row.get("content", row.get("raw")))),
                        ),
                    ),
                )
                if isinstance(row, Mapping)
                else row
            )
            raw = raw_container
            nested_row = raw_container if isinstance(raw_container, Mapping) else None
            if nested_row is not None:
                raw = nested_row.get(
                    "value",
                    nested_row.get(
                        "content_base64",
                        nested_row.get(
                            "base64",
                            nested_row.get(
                                "bytes",
                                nested_row.get(
                                    "hex", nested_row.get("content", nested_row.get("raw"))
                                ),
                            ),
                        ),
                    ),
                )
            offset = (
                row.get("offset", inherited_offset)
                if isinstance(row, Mapping)
                else inherited_offset
            )
            if nested_row is not None:
                offset = nested_row.get("offset", offset)
            normalized_offset = offset if type(offset) is int and offset >= 0 else None
            encoding_value = (
                nested_row.get("encoding")
                if nested_row is not None
                else row.get("encoding")
                if isinstance(row, Mapping)
                else None
            )
            if not isinstance(encoding_value, str) and isinstance(row, Mapping):
                encoding_value = row.get("encoding")
            encoding = encoding_value.casefold() if isinstance(encoding_value, str) else ""
            byte_value = _lossless_bytes(raw)
            row_kind = kind
            if byte_value is not None:
                raw_text = base64.b64encode(byte_value).decode("ascii")
                encoding = "base64"
                row_kind = "base64"
            elif isinstance(raw, str):
                raw_text = raw
                compact = re.sub(r"\s+", "", raw)
                if not encoding and kind == "bytes":
                    encoding = (
                        "hex"
                        if compact and re.fullmatch(r"(?i)(?:[0-9a-f]{2})+", compact)
                        else "utf-8"
                    )
                if not encoding:
                    encoding = kind
                if encoding in {"hex", "hexdump"}:
                    if compact and re.fullmatch(r"(?i)(?:[0-9a-f]{2})+", compact):
                        try:
                            byte_value = bytes.fromhex(compact)
                        except ValueError:
                            byte_value = None
                    row_kind = "hex" if kind in {"hex", "hexdump"} else kind
                elif "base64" in encoding:
                    try:
                        byte_value = base64.b64decode(compact, validate=True)
                    except (ValueError, binascii.Error):
                        byte_value = None
                    row_kind = "base64"
                elif encoding == "utf-8":
                    byte_value = raw.encode("utf-8")
                    row_kind = "bytes"
            else:
                continue

            metadata_sources: tuple[Mapping[str, Any], ...] = tuple(
                source
                for source in (nested_row, row if isinstance(row, Mapping) else None, payload)
                if isinstance(source, Mapping)
            )

            def metadata_value(
                *keys: str, sources: tuple[Mapping[str, Any], ...] = metadata_sources
            ) -> object:
                for source in sources:
                    for key in keys:
                        candidate = source.get(key)
                        if candidate is not None and candidate != "":
                            return candidate
                return None

            declared_length = metadata_value("byte_length", "length")
            byte_length = (
                len(byte_value)
                if byte_value is not None
                else declared_length
                if type(declared_length) is int and declared_length >= 0
                else None
            )
            digest_value = metadata_value(
                "sha256", "byte_sha256", "sample_sha256", "evidence_sha256", "blob_sha256"
            )
            digest = (
                hashlib.sha256(byte_value).hexdigest()
                if byte_value is not None
                else digest_value
                if isinstance(digest_value, str) and re.fullmatch(r"(?i)[0-9a-f]{64}", digest_value)
                else None
            )
            byte_label_parts = [f"encoding={encoding or kind}"]
            if byte_length is not None:
                byte_label_parts.append(f"byte_length={byte_length}")
            if digest is not None:
                byte_label_parts.append(f"sha256={digest}")
            for key in ("evidence_id", "blob_id", "association_id"):
                candidate = metadata_value(key)
                if isinstance(candidate, (str, int)) and str(candidate):
                    byte_label_parts.append(f"{key}={candidate}")
            for key in ("evidence_ids", "blob_ids", "association_ids"):
                candidate = metadata_value(key)
                if isinstance(candidate, (list, tuple)) and candidate:
                    byte_label_parts.append(
                        f"{key}=" + ",".join(str(item) for item in candidate if item is not None)
                    )
            display_length = declared_length if declared_length is not None else byte_length
            if display_length is not None:
                byte_label_parts.append(f"length={display_length}")
            mime_type = metadata_value("mime_type", "mime")
            if isinstance(mime_type, str) and mime_type.strip():
                byte_label_parts.append(f"mime_type={mime_type.strip()}")
            byte_label = "; ".join(byte_label_parts)
            normalized_value = (
                re.sub(r"\s+", "", raw_text).casefold() if row_kind == "hex" else raw_text.strip()
            )
            identity = (row_kind, normalized_offset, normalized_value)
            if identity in offset_row_identities:
                continue
            offset_row_identities.add(identity)
            title = "Octets observés"
            if byte_label:
                title = f"{title}; {byte_label}"
            if normalized_offset is not None:
                title = f"{title} — offset {normalized_offset}"
            offset_rows.append((row_kind, title, raw_text))

    def content_kind(path: tuple[str, ...], key: str, encoding: object = None) -> str:
        names = (*path, key)
        folded = tuple(name.casefold().replace("_", " ") for name in names)
        if (isinstance(encoding, str) and "base64" in encoding.casefold()) or any(
            "base64" in name for name in folded
        ):
            return "base64"
        if any("hex" in name or "hexdump" in name for name in folded):
            return "hex"
        if any("byte" in name for name in folded):
            return "bytes"
        if any(token in name for name in folded for token in ("string", "decoded", "config")):
            return "decoded"
        return "text"

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, Mapping):
            name = display_name(value, path[-1] if path else "contenu observé")
            for raw_key, child in value.items():
                key = str(raw_key).casefold().replace("-", "_")
                if is_operational_secret_field_name(key):
                    # Never project an AutoWork operational credential into a
                    # dossier, even when it is nested under a neutral label.
                    continue
                if sample_context and key in _CTI_METADATA_KEYS:
                    if isinstance(child, (Mapping, list, tuple)):
                        metadata_value = json.dumps(
                            child, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                        )
                    elif child is not None:
                        metadata_value = str(child)
                    else:
                        metadata_value = ""
                    add("metadata", f"Métadonnée CTI — {name}", f"{key}={metadata_value}")
                    continue
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
                if key in {
                    "hex",
                    "hex_bytes",
                    "bytes_hex",
                    "hexdump",
                    "bytes",
                    "base64",
                    "base64_bytes",
                    "byte",
                    "raw_bytes",
                }:
                    kind = (
                        "hex"
                        if "hex" in key or key == "hexdump"
                        else "base64"
                        if "base64" in key
                        else "bytes"
                    )
                    add_short_hex_rows(
                        child,
                        kind=kind,
                        inherited_offset=value.get("offset"),
                    )
                    continue
                if key in _METHOD_CONTAINER_KEYS:
                    walk(child, (*path, key))
                    continue
                if key in _STRING_KEYS:
                    walk(child, (*path, key))
                    continue
                if key in _CONTENT_KEYS and isinstance(child, str):
                    kind = content_kind(path, key, value.get("encoding"))
                    title_prefix = {
                        "decoded": "Valeur décodée",
                        "base64": "Valeur Base64",
                        "hex": "Octets observés",
                        "bytes": "Octets observés",
                    }.get(kind, "Texte observé")
                    offset = value.get("offset")
                    offset_suffix = (
                        f" — offset {offset}" if type(offset) is int and offset >= 0 else ""
                    )
                    add(kind, f"{title_prefix} — {name}{offset_suffix}", child)
                    continue
                if isinstance(child, (Mapping, list, tuple)):
                    walk(child, (*path, key))
                elif include_unclassified and key not in _GENERIC_METADATA_KEYS:
                    add_unclassified_scalar(child, (*path, key))
            return
        if isinstance(value, (list, tuple)):
            for index, child in enumerate(value, start=1):
                if isinstance(child, str):
                    kind = content_kind(path, "")
                    title_prefix = {
                        "decoded": "Valeur décodée",
                        "base64": "Valeur Base64",
                        "hex": "Octets observés",
                        "bytes": "Octets observés",
                    }.get(kind, "Texte observé")
                    add(
                        kind,
                        f"{title_prefix} {index}",
                        child,
                    )
                else:
                    child_path = (*path, str(index))
                    if isinstance(child, (Mapping, list, tuple)):
                        walk(child, child_path)
                    elif include_unclassified:
                        add_unclassified_scalar(child, child_path)
            return
        if isinstance(value, str) and path:
            key = path[-1].casefold()
            if key in _METHOD_KEYS:
                add("method", f"Méthode {path[-2] if len(path) > 1 else 'observée'}", value)

    walk(payload)
    for kind, title, raw in offset_rows:
        add(kind, title, raw)
    return excerpts


def _lossless_bytes(value: object) -> bytes | None:
    if isinstance(value, bytes | bytearray | memoryview):
        return bytes(value)
    if isinstance(value, list | tuple) and all(
        type(item) is int and 0 <= item <= 255 for item in value
    ):
        return bytes(value)
    return None


def _contains_sample_context(value: object) -> bool:
    if isinstance(value, Mapping):
        if isinstance(value.get("sample_sha256"), str):
            return True
        schema = value.get("schema")
        if isinstance(schema, str) and schema.casefold().startswith("sample-"):
            return True
        return any(_contains_sample_context(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_sample_context(item) for item in value)
    return False


def _sample_disclosure_restriction_sources(
    sample: object,
    envelope: InvestigationEnvelope | None,
) -> tuple[str, ...]:
    """Return disclosure veto names for a sample under the current envelope.

    ``do_not_submit`` is not a Bridge disclosure veto. The sample's own TLP,
    external-LLM, and licence restrictions continue to apply.
    """

    def value(name: str, default: object = None) -> object:
        if isinstance(sample, Mapping):
            return sample.get(name, default)
        return getattr(sample, name, default)

    sources: list[str] = []
    tlp = value("tlp")
    raw_tlp = getattr(tlp, "value", tlp)
    if isinstance(raw_tlp, str):
        tlp_name = raw_tlp.strip().casefold()
        if tlp_name.startswith("tlp:"):
            tlp_name = tlp_name[4:].strip()
        tlp_ranks = {
            "white": 0,
            "clear": 1,
            "green": 2,
            "amber": 3,
            "amber+strict": 4,
            "red": 5,
        }
        if tlp_name == "red":
            sources.append("tlp_red")
        elif (
            envelope is not None
            and envelope.disclosure.tlp is not None
            and tlp_name in tlp_ranks
            and envelope.disclosure.tlp.value.casefold() in tlp_ranks
            and tlp_ranks[tlp_name] > tlp_ranks[envelope.disclosure.tlp.value.casefold()]
        ):
            sources.append("tlp_above_envelope_bound")

    if value("external_llm_allowed", True) is False:
        sources.append("external_llm_disallowed")
    license_restriction = value("license_restriction")
    if (license_restriction is not None and bool(str(license_restriction).strip())) or value(
        "license_restricted", False
    ) is True:
        sources.append("license_restriction")
    return tuple(sources)


def build_advisor_dossier(
    prompt_data: Mapping[str, Any],
    *,
    envelope: InvestigationEnvelope | None = None,
) -> AdvisorDossier:
    """Select pertinent whole facts and render a shared French task dossier."""
    consultation_type = _as_text(prompt_data.get("consultation_type"))
    task = _as_text(prompt_data.get("question")) or _DEFAULT_TASKS.get(consultation_type, "")
    targets = _target_rows(prompt_data)
    target_text = "\n".join(_as_text(item.get("text")) for item in targets)
    relevance_text = f"{task}\n{target_text}"
    task_terms = _terms(relevance_text)
    explicit_claims = _requested_claim_ids(prompt_data, relevance_text)
    contradiction_requested = _contradiction_is_requested(prompt_data, relevance_text)
    explicitly_referenced = set(_explicit_reference_ids(prompt_data))
    evidence_rows = [item for item in prompt_data.get("evidence", ()) if isinstance(item, Mapping)]
    evidence_by_id = {
        _as_text(item.get("evidence_id")): item
        for item in evidence_rows
        if _as_text(item.get("evidence_id"))
    }
    unavailable_observations: set[str] = set()
    omitted_historical_observations: set[str] = set()
    candidates: list[dict[str, Any]] = []
    omitted_samples: list[Mapping[str, Any]] = []
    omitted_historical_samples: set[str] = set()
    relevant_measurement_sample_hashes = {
        _as_text(item.get("sample_sha256"))
        for item in prompt_data.get("measurements", ())
        if isinstance(item, Mapping)
        and _as_text(item.get("sample_sha256"))
        and (
            item.get("range_verified") is True
            or prompt_data.get("explicit_measurement_selection") is True
            or _measurement_explicitly_targeted(item, relevance_text)
        )
    }

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
        if score > 0 or ("article", article_ref) in explicitly_referenced:
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
                        **(
                            {"anchor": _as_text(item.get("anchor"))}
                            if _as_text(item.get("anchor"))
                            else {}
                        ),
                    },
                )
            )

    if consultation_type in {"plan_investigation", "pivot_strategy"}:
        context_lines = ["Contexte de consultation, non probant:"]
        if consultation_type == "plan_investigation":
            article_summary = _as_text(prompt_data.get("article_summary"))
            if article_summary:
                context_lines.append(f"article_summary={article_summary}")
            open_questions = prompt_data.get("open_questions", ())
            if isinstance(open_questions, (list, tuple)) and open_questions:
                context_lines.append(
                    "open_questions="
                    + json.dumps(open_questions, ensure_ascii=False, separators=(",", ":"))
                )
            tools = prompt_data.get("tools", ())
            capabilities = (
                [
                    _as_text(item.get("name"))
                    for item in tools
                    if isinstance(item, Mapping) and _as_text(item.get("name"))
                ]
                if isinstance(tools, (list, tuple))
                else []
            )
            if capabilities:
                context_lines.append(
                    "available_capabilities="
                    + json.dumps(capabilities, ensure_ascii=False, separators=(",", ":"))
                )
        gaps = prompt_data.get("gaps", ())
        if isinstance(gaps, (list, tuple)) and gaps:
            context_lines.append(
                "coverage_gaps=" + json.dumps(gaps, ensure_ascii=False, separators=(",", ":"))
            )
        context_body = "\n".join(line for line in context_lines if not line.endswith("="))
        if len(context_lines) > 1:
            candidates.append(
                _candidate(
                    kind="context",
                    identifier=f"consultation-context:{consultation_type}",
                    sha256=hashlib.sha256(context_body.encode("utf-8")).hexdigest(),
                    label="Contexte et limites de la consultation",
                    body=context_body,
                    score=1,
                    origin="consultation_context_summary",
                    extra={"context_is_evidence": False},
                )
            )

    for sample in prompt_data.get("samples", ()):
        if not isinstance(sample, Mapping) or sample.get("verified") is not True:
            continue
        if envelope is not None and _sample_disclosure_restriction_sources(sample, envelope):
            continue
        sample_id = _as_text(sample.get("sample_id"))
        association_id = _as_text(sample.get("association_id"))
        sample_sha256 = _as_text(sample.get("sha256"))
        raw_sample_evidence_ids = sample.get("evidence_ids", ())
        sample_evidence_ids = (
            {_as_text(value) for value in raw_sample_evidence_ids if _as_text(value)}
            if isinstance(raw_sample_evidence_ids, (list, tuple))
            else set()
        )
        sample_evidence = [
            evidence_by_id[item] for item in sample_evidence_ids if item in evidence_by_id
        ]
        historical_only = (
            bool(sample_evidence)
            and all(
                _as_text(item.get("source")).casefold() == "investigation.subject_sample_reuse"
                for item in sample_evidence
            )
            and _as_text(sample.get("origin_kind", sample.get("origin"))).casefold()
            == "subject_seed"
        )
        sample_explicitly_referenced = (
            any(
                (kind, identifier) in explicitly_referenced
                for kind, identifier in (
                    ("sample", sample_id),
                    ("association", association_id),
                    *(("observation", item) for item in sample_evidence_ids),
                )
                if identifier
            )
            or sample_sha256 in relevant_measurement_sample_hashes
        )
        # Editorial work needs the selected section's facts, not a dump of the
        # subject's sample inventory. Other technical consultations rely on
        # measured facts and source references; sample rows are included only
        # when explicitly requested. Planning can still see currently usable
        # samples, while inherited subject history is summarized below.
        suppress_sample = (
            (
                consultation_type in {"draft_text", "review_text"}
                and not sample_explicitly_referenced
            )
            or (historical_only and not sample_explicitly_referenced)
            or (
                consultation_type in {"pivot_strategy", "yara_draft", "suricata_draft"}
                and not sample_explicitly_referenced
            )
        )
        if suppress_sample:
            omitted_samples.append(sample)
            if historical_only and sample_sha256:
                omitted_historical_samples.add(sample_sha256)
            continue
        blob_id = _as_text(sample.get("blob_id"))
        byte_length = sample.get("byte_length")
        mime_type = _as_text(sample.get("mime_type"))
        content = (
            _as_text(sample.get("content_base64")) if sample.get("range_verified") is True else ""
        )
        fragment_length = sample.get("fragment_length", byte_length)
        omitted_range = _as_text(sample.get("omitted_range"))
        lines = [
            "Verified sample provenance:",
            f"sample_id={sample_id}",
            f"association_id={association_id}",
            f"blob_id={blob_id}",
            f"blob_sha256={_as_text(sample.get('blob_sha256'))}",
            f"sha256={sample_sha256}",
            f"sample_sha256={sample_sha256}",
            f"expected_hash={_as_text(sample.get('expected_hash'))}",
            f"source_object_id={_as_text(sample.get('source_object_id'))}",
            f"byte_length={byte_length}; mime_type={mime_type}",
            f"association_status={_as_text(sample.get('association_status'))}",
            f"content_status={_as_text(sample.get('content_status'))}",
            f"state={_as_text(sample.get('state'))}",
            f"origin={_as_text(sample.get('origin'))}",
            f"original_name={_as_text(sample.get('original_name'))}",
            f"source_service={_as_text(sample.get('source_service'))}",
            f"tlp={_as_text(sample.get('tlp'))}",
            f"external_llm_allowed={str(sample.get('external_llm_allowed')).lower()}",
        ]
        if sample.get("covers_whole_sample") is True:
            lines.append("covers_whole_sample=true")
        evidence_ids = sample.get("evidence_ids")
        if isinstance(evidence_ids, (list, tuple)):
            lines.append(
                "evidence_ids=" + ",".join(sorted(_as_text(item) for item in evidence_ids))
            )
        evidence_blob_ids = sample.get("evidence_blob_ids")
        if isinstance(evidence_blob_ids, (list, tuple)):
            lines.append(
                "evidence_blob_ids="
                + ",".join(sorted(_as_text(item) for item in evidence_blob_ids))
            )
        evidence_hashes = sample.get("evidence_sha256s")
        if isinstance(evidence_hashes, (list, tuple)):
            lines.append(
                "evidence_sha256s=" + ",".join(sorted(_as_text(item) for item in evidence_hashes))
            )
        related = sample.get("related_sample_sha256")
        if isinstance(related, (list, tuple)):
            lines.append(
                "related_sample_sha256=" + ",".join(sorted(_as_text(item) for item in related))
            )
        engines = sample.get("analysis_engines")
        if isinstance(engines, (list, tuple)):
            lines.append("analysis_engines=" + ",".join(_as_text(item) for item in engines))
        engine_statuses = sample.get("analysis_status_by_engine")
        if isinstance(engine_statuses, (list, tuple)):
            lines.append(
                "analysis_status_by_engine="
                + ",".join(
                    f"{_as_text(item.get('engine'))}:{_as_text(item.get('status'))}"
                    for item in engine_statuses
                    if isinstance(item, Mapping)
                )
            )
        if content:
            lines.extend(
                [
                    f"encoding=base64; offset={sample.get('offset', 0)}; length={fragment_length}",
                    "content_base64:",
                    content,
                ]
            )
        if omitted_range:
            lines.append(f"omitted_range={omitted_range}")
        omission_reason = sample.get("content_omission_reason")
        if isinstance(omission_reason, str) and omission_reason:
            lines.append(f"content_omission_reason={omission_reason}")
        request_hint = sample.get("content_request_hint")
        if isinstance(request_hint, str) and request_hint:
            lines.append(f"content_request_hint={request_hint}")
        body = "\n".join(lines)
        candidates.append(
            _candidate(
                kind="sample",
                identifier=sample_id or association_id,
                sha256=sample_sha256,
                label="Échantillon CTI vérifié",
                body=body,
                score=10_000,
                origin="verified_sample_association",
                extra={
                    "sample_id": sample_id,
                    "association_id": association_id,
                    "blob_id": blob_id,
                    "evidence_ids": list(evidence_ids)
                    if isinstance(evidence_ids, (list, tuple))
                    else [],
                    "offset": 0,
                    "length": fragment_length,
                    "byte_length": byte_length,
                    "mime_type": mime_type,
                    "encoding": "base64",
                    "fragment_sha256": _as_text(sample.get("fragment_sha256")),
                    **(
                        {"covers_whole_sample": True}
                        if sample.get("covers_whole_sample") is True
                        else {}
                    ),
                    "omitted_range": omitted_range,
                    "content_status": _as_text(sample.get("content_status")),
                    "content_omission_reason": _as_text(omission_reason),
                    "content_request_hint": _as_text(request_hint),
                },
                body_occurrences=_body_occurrences_for_content(
                    body, content, sample_id or association_id
                ),
            )
        )

    if omitted_samples:
        omission_reason = (
            "editorial_section_does_not_need_sample_inventory"
            if consultation_type in {"draft_text", "review_text"}
            else "historical_subject_samples_not_selected_by_default"
            if len(omitted_historical_samples) == len(omitted_samples)
            else "sample_inventory_not_selected_for_consultation"
        )
        omission_body = "\n".join(
            (
                f"omitted_sample_count={len(omitted_samples)}",
                *(
                    (f"omitted_historical_sample_count={len(omitted_historical_samples)}",)
                    if omitted_historical_samples
                    else ()
                ),
                f"omission_reason={omission_reason}",
                "resource=sample inventory and canonical sample associations",
                "consultation ciblée complémentaire possible si un sample précis devient utile",
            )
        )
        omission_sha = hashlib.sha256(omission_body.encode("utf-8")).hexdigest()
        candidates.append(
            _candidate(
                kind="omission",
                identifier=f"sample-inventory:{consultation_type}",
                sha256=omission_sha,
                label="Inventaire d'échantillons omis",
                body=omission_body,
                score=1,
                origin="summarized_sample_inventory_omission",
                extra={
                    "omitted_sample_count": len(omitted_samples),
                    "omission_reason": omission_reason,
                    "resource": "sample inventory and canonical sample associations",
                    "targeted_follow_up_available": True,
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

    sample_backed_evidence_ids = {
        _as_text(item.get("evidence_id"))
        for item in evidence_rows
        if _as_text(item.get("evidence_id")) and _observation_is_sample_backed(item)
    }
    for item in evidence_rows:
        identifier = _as_text(item.get("evidence_id"))
        if (
            _as_text(item.get("source")).casefold() == "investigation.subject_sample_reuse"
            and ("observation", identifier) not in explicitly_referenced
        ):
            omitted_historical_observations.add(identifier)
            continue
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
        piece_contents: list[tuple[int, str, str, str]] = []
        explicitly_referenced_observation = ("observation", identifier) in explicitly_referenced
        sample_backed = _observation_is_sample_backed(item)
        label = _human_evidence_label(item, excerpts)
        source = _as_text(item.get("source")).casefold()
        requested_source_families = (
            ("virustotal", r"(?i)\b(?:vt|virustotal)\b", ("virustotal.", "vt.")),
            ("floss", r"(?i)\bfloss\b", ("autowork.floss", "floss.")),
            ("capa", r"(?i)\bcapa\b", ("capa.",)),
            ("ilspy", r"(?i)\bilspy\b", ("dotnet.ilspycmd", "ilspy.")),
            ("network", r"(?i)\b(?:network|dns|tls|capture)\b", ("network.",)),
        )
        provider_relevant = any(
            re.search(request_pattern, relevance_text) is not None
            and source.startswith(source_prefixes)
            for _, request_pattern, source_prefixes in requested_source_families
        )
        row_score = max(_relevance_score(task_terms, label), 1 if provider_relevant else 0)
        omitted_sample_measurement = False
        for excerpt in excerpts:
            if sample_backed and _is_byte_excerpt(excerpt):
                continue
            body = _as_text(excerpt.get("text"))
            title = _as_text(excerpt.get("title")) or "Extrait"
            score = _relevance_score(task_terms, _excerpt_relevance_text(excerpt))
            if body and (score > 0 or explicitly_referenced_observation or provider_relevant):
                quoted_body = _quote(body)
                pieces.append(f"{title}:\n{quoted_body}")
                piece_contents.append((len(pieces) - 1, quoted_body, body, identifier))
                row_score = max(row_score, score, 1 if explicitly_referenced_observation else 0)
        for fact in (*measurement_rows, *typed_measurements):
            if fact.get("sample_sha256"):
                if fact.get("range_verified") is True:
                    continue
                if fact.get("range_verified") is not True and not _canonical_tool_measurement(
                    fact, evidence_by_id.get(identifier)
                ):
                    omitted_sample_measurement = True
                    continue
            raw_value = fact.get("sample_material", fact.get("value"))
            value = _as_text(raw_value)
            if not value or value in {"…", "..."}:
                continue
            kind = _as_text(fact.get("kind")) or "mesure"
            offset = fact.get("offset")
            declared_encoding = _as_text(fact.get("encoding")).casefold().replace("-", "")
            if declared_encoding in {"raw", "legacytyped"} and kind.casefold() == "hex":
                declared_encoding = "hex"
            elif declared_encoding == "legacytyped" and kind.casefold() in {"hex", "base64"}:
                declared_encoding = kind.casefold()
            byte_encoding = (
                declared_encoding
                if declared_encoding in {"hex", "hexdump", "base64", "bytes"}
                else kind.casefold()
                if kind.casefold() in {"hex", "hexdump", "base64", "bytes"}
                else ""
            )
            fragment_bytes = _advisor_fragment_bytes(raw_value, byte_encoding)
            if isinstance(raw_value, (bytes, bytearray, memoryview)) or (
                isinstance(raw_value, (list, tuple))
                and all(type(byte) is int and 0 <= byte <= 255 for byte in raw_value)
            ):
                fragment_bytes = _advisor_fragment_bytes(raw_value, "bytes")
                byte_encoding = "base64"
                if fragment_bytes is not None:
                    value = base64.b64encode(fragment_bytes).decode("ascii")
            byte_length = (
                len(fragment_bytes)
                if fragment_bytes is not None
                else fact.get("byte_length", fact.get("length"))
            )
            declared_fragment_sha256 = _as_text(fact.get("fragment_sha256"))
            full_digest = (
                hashlib.sha256(fragment_bytes).hexdigest()
                if fragment_bytes is not None
                else declared_fragment_sha256
                if re.fullmatch(r"(?i)[0-9a-f]{64}", declared_fragment_sha256)
                else ""
            )
            is_byte_fragment = byte_encoding in {"hex", "hexdump", "base64", "bytes"} or (
                kind.casefold() in {"hex", "hexdump", "base64", "bytes"}
            )
            canonical_tool_measurement = _canonical_tool_measurement(
                fact, evidence_by_id.get(identifier)
            )
            if (
                sample_backed
                and is_byte_fragment
                and fact.get("range_verified") is not True
                and not canonical_tool_measurement
            ):
                omitted_sample_measurement = True
                continue
            provenance = []
            if is_byte_fragment and byte_encoding:
                provenance.append(f"encoding={byte_encoding}")
            if is_byte_fragment and type(byte_length) is int and byte_length >= 0:
                provenance.append(f"byte_length={byte_length}")
            declared_length = fact.get("length")
            if is_byte_fragment and type(declared_length) is int and declared_length >= 0:
                provenance.append(f"length={declared_length}")
            if is_byte_fragment and full_digest:
                provenance.extend((f"sha256={full_digest}", f"fragment_sha256={full_digest}"))
            if is_byte_fragment:
                for key in ("evidence_id", "blob_id", "association_id"):
                    identifier_value = _as_text(fact.get(key))
                    if identifier_value:
                        provenance.append(f"{key}={identifier_value}")
                sample_sha256 = _as_text(fact.get("sample_sha256"))
                if sample_sha256:
                    provenance.append(f"sample_sha256={sample_sha256}")
                mime_type = _as_text(fact.get("mime_type", fact.get("mime")))
                if mime_type:
                    provenance.append(f"mime_type={mime_type}")
            location = f"offset {offset}" if type(offset) is int and offset >= 0 else ""
            fact_text = f"{kind}, {location}: {value}" if location else f"{kind}: {value}"
            if canonical_tool_measurement:
                fact_text += (
                    " [canonical tool observation; range not reverified against canonical blob]"
                )
            if provenance:
                fact_text += " [" + "; ".join(provenance) + "]"
            score = _relevance_score(task_terms, fact_text)
            if _numeric_targets_conflict(relevance_text, fact_text):
                score = 0
            if score > 0:
                pieces.append(f"Mesure observée: {fact_text}")
                piece_contents.append((len(pieces) - 1, value, value, identifier))
                row_score = max(row_score, score, 1)
        if omitted_sample_measurement:
            pieces.append(
                "Contenu omis: sample_range_not_verified_against_canonical_blob\n"
                "Sélectionner une mesure vérifiée avec blob_id, type, offset et longueur; "
                "la plage doit correspondre aux octets canoniques."
            )
        omission_reason = _as_text(item.get("content_omission_reason"))
        request_hint = _as_text(item.get("content_request_hint"))
        if pieces and omission_reason:
            omission_note = f"Contenu omis: {omission_reason}"
            if request_hint:
                omission_note += f"\n{request_hint}"
            pieces.append(omission_note)
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
                        **({"content_omission_reason": omission_reason} if omission_reason else {}),
                    },
                    body_occurrences=_joined_body_occurrences(pieces, piece_contents),
                )
            )
        else:
            omission_reason = _as_text(item.get("content_omission_reason")) or (
                "sample_bytes_not_included_without_targeted_range"
                if _observation_is_sample_backed(item)
                else (
                    "Canonical content was omitted by task relevance selection; this does not "
                    "establish semantic irrelevance because relevance to the question was not "
                    "verified."
                )
            )
            request_hint = _as_text(item.get("content_request_hint")) or (
                "Select a verified measurement by blob_id, kind, offset, and value; its canonical "
                "length must match the sample bytes."
                if _observation_is_sample_backed(item)
                else ""
            )
            body = "\n".join(
                part
                for part in (
                    f"evidence_id={identifier}",
                    f"sha256={_as_text(item.get('sha256'))}",
                    f"source={_as_text(item.get('source'))}",
                    f"object_type={_as_text(item.get('object_type'))}",
                    f"content_omission_reason={omission_reason}",
                    f"content_request_hint={request_hint}" if request_hint else "",
                )
                if part
            )
            candidates.append(
                _candidate(
                    kind="observation",
                    identifier=identifier,
                    sha256=_as_text(item.get("sha256")),
                    label="Référence d'observation — contenu omis",
                    body=body,
                    score=1 if explicitly_referenced_observation else 0,
                    origin="observation_reference_only",
                    extra={
                        "source_ref": {
                            "kind": "observation",
                            "id": identifier,
                            "sha256": _as_text(item.get("sha256")),
                        },
                        "content_omission_reason": omission_reason,
                        **({"content_request_hint": request_hint} if request_hint else {}),
                    },
                )
            )

    if omitted_historical_observations:
        omission_body = "\n".join(
            (
                f"omitted_historical_observation_count={len(omitted_historical_observations)}",
                "omission_reason=historical_subject_sample_observations_not_selected_by_default",
                "resource=historical sample analysis and canonical observation details",
                "consultation ciblée possible si une observation précise devient utile",
            )
        )
        candidates.append(
            _candidate(
                kind="omission",
                identifier="historical-sample-observations:editorial",
                sha256=hashlib.sha256(omission_body.encode("utf-8")).hexdigest(),
                label="Observations historiques omises",
                body=omission_body,
                score=1,
                origin="summarized_historical_observation_omission",
                extra={
                    "omitted_historical_observation_count": len(omitted_historical_observations),
                    "omission_reason": (
                        "historical_subject_sample_observations_not_selected_by_default"
                    ),
                    "resource": "historical sample analysis and canonical observation details",
                    "targeted_follow_up_available": True,
                },
            )
        )

    for item in prompt_data.get("findings", ()):
        if not isinstance(item, Mapping):
            continue
        statement = _as_text(item.get("line", item.get("statement")))
        if not statement:
            continue
        finding_id = _as_text(item.get("finding_id"))
        assessment = _as_text(item.get("assessment"))
        claim_refs = item.get("claim_refs", ())
        claim_text = (
            ", ".join(str(value) for value in claim_refs)
            if isinstance(claim_refs, (list, tuple))
            else ""
        )
        limitations = item.get("limitations", ())
        limitation_text = (
            "Limitations: " + "; ".join(_as_text(value) for value in limitations if _as_text(value))
            if isinstance(limitations, (list, tuple))
            else ""
        )
        score = _relevance_score(
            task_terms,
            " ".join(
                part
                for part in (statement, claim_text, assessment, _as_text(item.get("kind")))
                if part
            ),
        )
        if (
            assessment == "refuted"
            and contradiction_requested
            and _finding_matches_requested_claims(item, explicit_claims)
        ):
            score += 100
        finding_body = "\n".join(
            part
            for part in (
                f"Évaluation: {assessment}" if assessment else "",
                f"Claims: {claim_text}" if claim_text else "",
                "Observations: "
                + ", ".join(str(value) for value in item.get("observation_refs", ()))
                if isinstance(item.get("observation_refs", ()), (list, tuple))
                and item.get("observation_refs")
                else "",
                limitation_text,
                statement,
            )
            if part
        )
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
                    body=finding_body,
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
                    body_occurrences=_body_occurrences_for_content(
                        finding_body, statement, finding_id
                    ),
                )
            )

    for item in prompt_data.get("measurements", ()):
        if not isinstance(item, Mapping):
            continue
        evidence_id = _as_text(item.get("evidence_id"))
        measurement_evidence = evidence_by_id.get(evidence_id)
        is_sample_backed_measurement = bool(item.get("sample_sha256")) or (
            evidence_id in sample_backed_evidence_ids
        )
        canonical_tool_measurement = _canonical_tool_measurement(item, measurement_evidence)
        measurement_value_status, sample_range_status = _measurement_value_status(
            item, measurement_evidence, sample_backed=is_sample_backed_measurement
        )
        if (
            is_sample_backed_measurement
            and item.get("range_verified") is not True
            and not canonical_tool_measurement
        ):
            measurement_id = _as_text(item.get("measurement_id"))
            if measurement_id:
                digest = hashlib.sha256(
                    json.dumps(
                        item,
                        ensure_ascii=False,
                        sort_keys=True,
                        separators=(",", ":"),
                    ).encode("utf-8")
                ).hexdigest()
                omission_reason = _as_text(item.get("content_omission_reason")) or (
                    "sample_range_not_verified_against_canonical_blob"
                )
                candidates.append(
                    _candidate(
                        kind="measurement",
                        identifier=measurement_id,
                        sha256=digest,
                        label=(
                            "Référence de mesure — donnée externe non vérifiée"
                            if measurement_value_status == "external_unverified"
                            else "Référence de mesure — provenance insuffisante"
                        ),
                        body="\n".join(
                            part
                            for part in (
                                f"measurement_id={measurement_id}",
                                f"evidence_id={evidence_id}",
                                f"evidence_sha256={_as_text(item.get('evidence_sha256'))}",
                                f"blob_id={_as_text(item.get('blob_id'))}",
                                f"sample_sha256={_as_text(item.get('sample_sha256'))}",
                                f"offset={item.get('offset')}",
                                f"length={item.get('length')}",
                                f"fragment_sha256={_as_text(item.get('fragment_sha256'))}",
                                f"measurement_value_status={measurement_value_status}",
                                f"sample_blob_range_status={sample_range_status}",
                                f"content_omission_reason={omission_reason}",
                                "ressource: observation canonique et contexte d'extraction",
                                "consultation ciblée possible pour mesurer la plage canonique",
                            )
                            if part
                        ),
                        score=1 if prompt_data.get("explicit_measurement_selection") is True else 0,
                        origin="measurement_reference_only",
                        extra={
                            "measurement_id": measurement_id,
                            "evidence_id": _as_text(item.get("evidence_id")),
                            "sample_sha256": _as_text(item.get("sample_sha256")),
                            "offset": item.get("offset"),
                            "length": item.get("length"),
                            "fragment_sha256": _as_text(item.get("fragment_sha256")),
                            "encoding": _as_text(item.get("encoding")),
                            "range_verified": item.get("range_verified"),
                            "measurement_value_status": measurement_value_status,
                            "sample_blob_range_status": sample_range_status,
                            "content_omission_reason": omission_reason,
                            "resource": "canonical observation and extraction context",
                            "targeted_follow_up_available": True,
                        },
                    )
                )
            continue
        measurement_id = _as_text(item.get("measurement_id"))
        evidence_id = _as_text(item.get("evidence_id"))
        raw_value = item.get("value")
        value = _as_text(raw_value)
        rendered_as_base64 = False
        if not value and (
            isinstance(raw_value, (bytes, bytearray, memoryview))
            or (
                isinstance(raw_value, (list, tuple))
                and all(type(byte) is int and 0 <= byte <= 255 for byte in raw_value)
            )
        ):
            value = base64.b64encode(bytes(raw_value)).decode("ascii")
            rendered_as_base64 = True
        if not measurement_id or not value:
            continue
        if is_sample_backed_measurement:
            if item.get("range_verified") is True:
                label_status = " — sample blob range verified against canonical blob"
            else:
                label_status = (
                    " — canonical tool observation; range not reverified against canonical blob"
                )
        else:
            label_status = ""
        kind = _as_text(item.get("kind")) or "mesure"
        offset = item.get("offset")
        sample_sha256 = _as_text(item.get("sample_sha256"))
        source_evidence_sha256 = _as_text(item.get("evidence_sha256"))
        declared_encoding = _as_text(item.get("encoding")).casefold()
        byte_encoding = (
            "base64"
            if rendered_as_base64
            else declared_encoding
            if declared_encoding
            in {"ascii", "utf8", "utf16le", "hex", "hexdump", "base64", "bytes"}
            else kind.casefold()
            if kind.casefold() in {"hex", "base64", "bytes"}
            else ""
        )
        fragment_bytes = _advisor_fragment_bytes(raw_value, byte_encoding)
        if fragment_bytes is None:
            fragment_bytes = _advisor_fragment_bytes(value, byte_encoding)
        if fragment_bytes is not None and not byte_encoding:
            byte_encoding = "base64"
        byte_length = (
            len(fragment_bytes)
            if fragment_bytes is not None
            else item.get("byte_length", item.get("length"))
        )
        byte_metadata = []
        if byte_encoding:
            byte_metadata.append(f"encoding={byte_encoding}")
            if type(byte_length) is int and byte_length >= 0:
                byte_metadata.append(f"byte_length={byte_length}; length={byte_length}")
            if item.get("covers_whole_sample") is True:
                byte_metadata.append("covers_whole_sample=true")
            declared_fragment_sha256 = _as_text(item.get("fragment_sha256"))
            full_digest = (
                hashlib.sha256(fragment_bytes).hexdigest()
                if fragment_bytes is not None
                else declared_fragment_sha256
                if re.fullmatch(r"(?i)[0-9a-f]{64}", declared_fragment_sha256)
                else ""
            )
            if full_digest:
                byte_metadata.append(f"sha256={full_digest}")
                byte_metadata.append(f"fragment_sha256={full_digest}")
            for key in ("evidence_id", "blob_id", "association_id"):
                identifier = _as_text(item.get(key))
                if identifier:
                    byte_metadata.append(f"{key}={identifier}")
            mime_type = _as_text(item.get("mime_type", item.get("mime")))
            if mime_type:
                byte_metadata.append(f"mime_type={mime_type}")
        offset_label = f"offset {offset}" if type(offset) is int else ""
        if offset_label and byte_metadata:
            offset_label += "; " + "; ".join(byte_metadata)
        elif byte_metadata:
            offset_label = "; ".join(byte_metadata)
        body = f"{kind}, {offset_label}: {value}" if offset_label else f"{kind}: {value}"
        score = _relevance_score(task_terms, body)
        if _numeric_targets_conflict(relevance_text, body):
            score = 0
        explicitly_selected_measurement = prompt_data.get(
            "explicit_measurement_selection"
        ) is True or _measurement_explicitly_targeted(item, relevance_text)
        if (
            score > 0
            or ("measurement", measurement_id) in explicitly_referenced
            or explicitly_selected_measurement
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
                    + (
                        f", offset {offset}"
                        + (f"; {'; '.join(byte_metadata)}" if byte_metadata else "")
                        if type(offset) is int
                        else f"; {'; '.join(byte_metadata)}"
                        if byte_metadata
                        else ""
                    )
                    + label_status,
                    body=body,
                    score=max(
                        score,
                        1 if ("measurement", measurement_id) in explicitly_referenced else 0,
                    ),
                    origin="typed_measurement",
                    extra={
                        "measurement_id": measurement_id,
                        "evidence_id": evidence_id,
                        "evidence_sha256": source_evidence_sha256,
                        "sample_sha256": sample_sha256,
                        "offset": offset if type(offset) is int else None,
                        "length": byte_length,
                        "byte_length": byte_length,
                        "encoding": byte_encoding,
                        "fragment_sha256": _as_text(item.get("fragment_sha256"))
                        or (
                            hashlib.sha256(fragment_bytes).hexdigest()
                            if fragment_bytes is not None
                            else ""
                        ),
                        "range_verified": item.get("range_verified"),
                        "measurement_value_status": measurement_value_status,
                        "sample_blob_range_status": sample_range_status,
                        **(
                            {"covers_whole_sample": True}
                            if item.get("covers_whole_sample") is True
                            else {}
                        ),
                        "blob_id": item.get("blob_id"),
                        "association_id": item.get("association_id"),
                        "mime_type": item.get("mime_type", item.get("mime")),
                        "extractor_version": _as_text(item.get("extractor_version")),
                        "source_ref": {
                            "kind": "measurement",
                            "id": measurement_id,
                            "sha256": measurement_sha,
                        },
                    },
                    body_occurrences=_body_occurrences_for_content(
                        body, value, evidence_id or measurement_id
                    ),
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
        rule_score = _relevance_score(task_terms, body)
        candidates.append(
            _candidate(
                kind="rule",
                identifier=evidence_id,
                sha256=digest,
                label="État mesuré de règle et corpus",
                body=body,
                score=max(1, rule_score),
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
                    body_occurrences=_body_occurrences_for_content(
                        body, value, evidence_id or fact_id
                    ),
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
                    key=lambda row: (
                        _relevance_score(
                            task_terms,
                            _excerpt_relevance_text(row),
                        ),
                        row.get("kind") != "metadata",
                    ),
                )
                body = f"{_as_text(best.get('title'))}:\n{_quote(_as_text(best.get('text')))}"
                candidates.append(
                    _candidate(
                        kind="observation",
                        identifier=identifier,
                        sha256=_as_text(item.get("sha256")),
                        label=_human_evidence_label(item, excerpts),
                        body=body,
                        score=0,
                        origin="fallback_best_observation",
                        body_occurrences=_body_occurrences_for_content(
                            body,
                            _as_text(best.get("text")),
                            identifier,
                            rendered_content=_quote(_as_text(best.get("text"))),
                        ),
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
                    body_occurrences=_body_occurrences_for_content(
                        (
                            f"{kind}, offset {offset}: {value}"
                            if type(offset) is int
                            else f"{kind}: {value}"
                        ),
                        value,
                        _as_text(item.get("evidence_id")) or measurement_id,
                    ),
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
                    body_occurrences=_body_occurrences_for_content(
                        f"{field}: {value}" if field else value,
                        value,
                        _as_text(item.get("evidence_id")) or fact_id,
                    ),
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
        candidate_key = (item["kind"], item["id"], item["body"])
        if candidate_key not in seen:
            seen.add(candidate_key)
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

    # Keep a stable alias for each displayed fact. The mapping stays local and
    # carries only the data needed by output validation and provenance.
    selected = _prioritize_duplicate_measurements(selected)
    manifest: list[dict[str, Any]] = []
    rendered: list[str] = []
    review_target_index = 0
    evidence_index = 0
    seen_bodies: dict[str, list[tuple[str, str, str]]] = {}
    for item in selected:
        if item["origin"] == "versioned_review_target":
            review_target_index += 1
            alias = f"T{review_target_index}"
        else:
            evidence_index += 1
            alias = f"E{evidence_index}"
        body, body_references = _render_deduplicated_body(item, alias, seen_bodies)
        piece = f"[{alias}] {item['label']}\n{_quote(body)}"
        body_bytes = body.encode("utf-8")
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
                **({"body_references": body_references} if body_references else {}),
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

    if contradiction_requested:
        selected_claims = {
            _as_text(item.get("extra", {}).get("claim_id")).casefold()
            for item in selected
            if item["kind"] == "article" and _as_text(item.get("extra", {}).get("claim_id"))
        }
        contradiction_claims = explicit_claims or selected_claims
        refuted_claims = {
            _as_text(claim_id).casefold()
            for item in prompt_data.get("findings", ())
            if isinstance(item, Mapping) and _as_text(item.get("assessment")) == "refuted"
            for claim_id in item.get("claim_refs", ())
        }
        uncovered_claims = sorted(contradiction_claims - refuted_claims)
        if uncovered_claims:
            lines.append(
                "Aucune réfutation connue pour les passages "
                f"{', '.join(uncovered_claims)} ; la recherche de contradictions fait l'objet "
                "de cette consultation. Cette absence ne prouve pas qu'aucune contradiction "
                "n'existe."
            )
        elif not any(
            item["kind"] == "finding" and item.get("assessment") == "refuted" for item in selected
        ):
            lines.append(
                "Aucun finding évalué comme réfuté n'a été chargé dans le dossier ; la recherche "
                "de contradictions fait l'objet de cette consultation. Cette absence ne prouve "
                "pas qu'aucune contradiction n'existe."
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
    contradiction_requested = _contradiction_is_requested(prompt_data, task)
    contradiction_required = _contradiction_is_required(
        prompt_data, _as_text(prompt_data.get("question"))
    )
    requested_finding_ids = _requested_contradiction_finding_ids(
        prompt_data, _as_text(prompt_data.get("question"))
    )
    references = set(_explicit_reference_ids(prompt_data))
    evidence_rows = [item for item in prompt_data.get("evidence", ()) if isinstance(item, Mapping)]
    evidence_by_id = {
        _as_text(item.get("evidence_id")): item
        for item in evidence_rows
        if _as_text(item.get("evidence_id"))
    }
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
            or ("article", article_id) in references
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
            or ("article", article_id) in references
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
                False,
                "Current hypotheses are optional planning context.",
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
            sample_provenance_present = False
            if kind == "observation":
                excerpts = item.get("content_excerpts", ())
                eligible_excerpts = (
                    [
                        row
                        for row in excerpts
                        if isinstance(row, Mapping)
                        and (not _observation_is_sample_backed(item) or not _is_byte_excerpt(row))
                    ]
                    if isinstance(excerpts, (list, tuple))
                    else []
                )
                sample_provenance_present = any(
                    isinstance(sample, Mapping)
                    and sample.get("verified") is True
                    and item.get("blob_read_verified") is not False
                    and isinstance(sample.get("evidence_ids"), (list, tuple))
                    and identifier in {str(value) for value in sample.get("evidence_ids", ())}
                    for sample in prompt_data.get("samples", ())
                )
                has_body = (
                    bool(any(_as_text(row.get("text")) for row in eligible_excerpts))
                    or any(
                        isinstance(row, Mapping)
                        and _as_text(row.get("evidence_id")) == identifier
                        and _as_text(row.get("sample_material", row.get("value")))
                        for row in prompt_data.get("measured_facts", ())
                    )
                    or sample_provenance_present
                )
            else:
                has_body = bool(_as_text(item.get("line", item.get("statement"))))
            explicitly_referenced = (kind, identifier) in references
            dossier_content_selected = any(
                row.get("kind") == kind
                and row.get("id") == identifier
                and row.get("origin") != "observation_reference_only"
                for row in dossier.manifest
            )
            content_omission_reason = _as_text(item.get("content_omission_reason"))
            reference_only_reason = next(
                (
                    _as_text(row.get("content_omission_reason"))
                    for row in dossier.manifest
                    if row.get("kind") == kind
                    and row.get("id") == identifier
                    and row.get("origin") == "observation_reference_only"
                ),
                "",
            )
            omission_reason = content_omission_reason or reference_only_reason
            sample_content_omitted = omission_reason.startswith(("sample_", "subject_sample_"))
            selected_for_type = (
                consultation_type
                in {
                    "plan_investigation",
                    "pivot_strategy",
                    "yara_draft",
                    "figure_plan",
                }
                and has_body
                and dossier_content_selected
            )
            is_contradiction = item.get(
                "assessment"
            ) == "refuted" and _finding_matches_requested_claims(item, claim_ids)
            required = (
                (
                    explicitly_referenced
                    and (
                        not omission_reason
                        or item.get("blob_read_verified") is False
                        or not sample_content_omitted
                    )
                )
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
                if explicitly_referenced and not omission_reason
                else omission_reason
                if content_omission_reason
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
                blob_read_failure_reason=_as_text(item.get("blob_read_failure_reason")),
                retrieval_available=(
                    kind == "observation"
                    and bool(identifier)
                    and bool(re.fullmatch(r"[0-9a-f]{64}", sha))
                    and item.get("blob_read_verified") is True
                ),
                content_omission_reason=content_omission_reason or reference_only_reason,
            )

    for item in prompt_data.get("_disclosure_omissions", ()):
        if not isinstance(item, Mapping):
            continue
        identifier = _as_text(item.get("id"))
        if not identifier or ("observation", identifier) in {
            (row["kind"], row["id"]) for row in records
        }:
            continue
        reason = _as_text(item.get("reason")) or (
            "Intentionally omitted: not disclosable under current policy."
        )
        add(
            "observation",
            identifier,
            _as_text(item.get("sha256")),
            item.get("source_ref"),
            False,
            False,
            reason,
            policy_omission=True,
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
    for item in measurements:
        identifier = _as_text(item.get("measurement_id"))
        explicitly_selected = prompt_data.get(
            "explicit_measurement_selection"
        ) is True or _measurement_explicitly_targeted(item, task)
        selected_in_dossier = any(
            row.get("kind") == "measurement"
            and row.get("id") == identifier
            and row.get("origin") != "measurement_reference_only"
            for row in dossier.manifest
        )
        type_requires_measurements = consultation_type in {"yara_draft", "pivot_strategy"}
        measurement_evidence = evidence_by_id.get(_as_text(item.get("evidence_id")))
        canonical_tool_measurement = _canonical_tool_measurement(item, measurement_evidence)
        sample_backed_measurement = bool(_as_text(item.get("sample_sha256"))) or (
            measurement_evidence is not None and _observation_is_sample_backed(measurement_evidence)
        )
        sample_value_available = (
            not sample_backed_measurement
            or item.get("range_verified") is True
            or canonical_tool_measurement
        )
        measurement_value_status, sample_range_status = _measurement_value_status(
            item,
            measurement_evidence,
            sample_backed=sample_backed_measurement,
        )
        required = sample_value_available and (
            ("measurement", identifier) in references
            or (type_requires_measurements and (explicitly_selected or selected_in_dossier))
        )
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
            bool(_as_text(item.get("value"))) and sample_value_available,
            required,
            _as_text(item.get("content_omission_reason"))
            if item.get("range_verified") is False
            else "Measurement is explicitly selected by the invocation or named in the question."
            if explicitly_selected
            else "Measurement was selected for this consultation."
            if required
            else "Measurement was not selected or explicitly targeted for this question.",
            sample_sha256=item.get("sample_sha256"),
            offset=item.get("offset"),
            evidence_id=item.get("evidence_id"),
            evidence_sha256=item.get("evidence_sha256"),
            measurement_value_status=measurement_value_status,
            sample_blob_range_status=sample_range_status,
            range_verified=item.get("range_verified"),
            resource="canonical observation and extraction context",
            targeted_follow_up_available=not sample_value_available,
            informational=type_requires_measurements and not required,
            verified=(
                bool(re.fullmatch(r"[0-9a-f]{64}", _as_text(item.get("sample_sha256"))))
                and type(item.get("offset")) is int
                and item.get("offset", -1) >= 0
                and bool(_as_text(item.get("value")))
                and bool(_as_text(item.get("evidence_id")))
                and bool(re.fullmatch(r"[0-9a-f]{64}", _as_text(item.get("evidence_sha256"))))
                and (item.get("range_verified") is True or canonical_tool_measurement)
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
            False,
            "No measured network fact is available; a Suricata candidate remains untested.",
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
                    "required": False,
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

    proof_kinds = {"observation", "finding", "article", "measurement", "fact"}
    if consultation_type == "yara_draft":
        proof_kinds = {"observation", "measurement"}
    if consultation_type in {
        "plan_investigation",
        "pivot_strategy",
        "yara_draft",
        "figure_plan",
    } and not any(
        item["required"]
        and item["kind"] in proof_kinds
        and (item["body_present"] or item.get("retrieval_available"))
        for item in records
    ):
        proof_reason = (
            "No canonical static-analysis observation or verified measurement body was loaded "
            "for this YARA draft; add an observation reference with its SHA-256 or provide a "
            "verified sample offset."
            if consultation_type == "yara_draft"
            else "No canonical observation, finding, or claim passage body is available for "
            "this question."
        )
        add(
            "observation",
            "question-proof",
            "",
            None,
            False,
            True,
            proof_reason,
        )
    useful_text_kinds = {"observation", "finding", "article"}
    if consultation_type == "draft_text":
        useful_text_kinds.add("measurement")
    if consultation_type == "review_text":
        useful_text_kinds.add("opinion")
    if consultation_type in {"draft_text", "review_text"} and not any(
        item["kind"] in useful_text_kinds and item["body_present"] for item in records
    ):
        add(
            "observation",
            "question-proof",
            "",
            None,
            False,
            True,
            "No canonical observation, finding, or claim passage body is available "
            "for this text consultation.",
        )

    if contradiction_requested:
        if contradiction_required and claim_ids:
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
        elif contradiction_required and requested_finding_ids:
            covered_finding_ids = {
                item["id"].casefold()
                for item in records
                if item["kind"] == "finding" and item.get("assessment") == "refuted"
            }
            for finding_id in sorted(requested_finding_ids - covered_finding_ids):
                add(
                    "contradiction",
                    f"required-contradiction:finding:{finding_id}",
                    "",
                    {"kind": "finding", "id": finding_id},
                    False,
                    True,
                    f"Finding {finding_id} is cited as a contradiction target but no refuted "
                    "finding was loaded.",
                    finding_id=finding_id,
                )
        elif contradiction_required:
            add(
                "contradiction",
                "required-contradiction",
                "",
                None,
                False,
                True,
                "No refuted finding was loaded for the requested contradiction.",
            )
        else:
            relevant_claims = {
                _as_text(item.get("claim_id")).casefold()
                for item in records
                if item["kind"] == "article"
                and item["required"]
                and item["body_present"]
                and _as_text(item.get("claim_id"))
            }
            covered_claims = {
                _as_text(claim).casefold()
                for item in records
                if item["kind"] == "finding" and item.get("assessment") == "refuted"
                for claim in item.get("claim_refs", ())
            }
            uncovered_claims = sorted(relevant_claims - covered_claims)
            for claim_id in uncovered_claims:
                add(
                    "contradiction",
                    f"informational-contradiction:{claim_id}",
                    "",
                    {"kind": "claim", "id": claim_id},
                    False,
                    False,
                    "Aucune réfutation connue pour ce passage ; la recherche de contradictions "
                    "fait l'objet de la consultation.",
                    claim_id=claim_id,
                    informational=True,
                )
            if not relevant_claims and not any(
                item["kind"] == "finding" and item.get("assessment") == "refuted"
                for item in records
            ):
                add(
                    "contradiction",
                    "informational-contradiction",
                    "",
                    None,
                    False,
                    False,
                    "Aucun finding réfuté n'a été chargé ; cette absence ne prouve pas qu'aucune "
                    "contradiction n'existe.",
                    informational=True,
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
            and not (
                item["kind"] == "observation" and row.get("origin") == "observation_reference_only"
            )
        ]
        if item["kind"] == "observation":
            matches.extend(
                row
                for row in dossier_rows
                if row.get("kind") == "sample"
                and item.get("blob_read_verified") is not False
                and isinstance(row.get("evidence_ids"), (list, tuple))
                and item["id"] in {str(value) for value in row.get("evidence_ids", ())}
                and row not in matches
            )
        statuses: list[dict[str, str]] = []

        def mark(
            status: AdvisorEvidenceStatus,
            reason: str,
            *,
            output: list[dict[str, str]] = statuses,
        ) -> None:
            output.append({"status": status.value, "reason": reason})

        policy_omission = item.get("policy_omission") is True
        if policy_omission:
            mark(AdvisorEvidenceStatus.INTENTIONALLY_OMITTED, item["reason"])
        elif item["required"]:
            mark(AdvisorEvidenceStatus.REQUIRED, item["reason"])
        elif item.get("informational"):
            mark(AdvisorEvidenceStatus.INFORMATIONAL, item["reason"])
        if item.get("content_omission_reason"):
            mark(
                AdvisorEvidenceStatus.INTENTIONALLY_OMITTED,
                _as_text(item.get("content_omission_reason")),
            )
        verified = item.get("verified", True)
        if policy_omission:
            pass
        elif item["body_present"] and sha_valid and verified:
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
                    "The task relevance selection did not include this proof; semantic "
                    "relevance to the question was not verified."
                ),
            )
            mark(
                AdvisorEvidenceStatus.INTENTIONALLY_OMITTED,
                (
                    "Canonical proof was omitted by task relevance selection; this omission "
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
                    "reason": item.get("blob_read_failure_reason")
                    or (
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
        if (
            not policy_omission
            and item["required"]
            and not item.get("blob_read_failure_reason")
            and any(row["status"] == AdvisorEvidenceStatus.NOT_VERIFIED.value for row in statuses)
        ):
            missing_refs.append(
                {
                    "ref": _display_source_ref(item["source_ref"] or item["id"]),
                    "reason": "Required canonical proof lacks verifiable metadata.",
                }
            )
        elif (
            not policy_omission
            and not item["required"]
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
        elif (
            not policy_omission
            and not item["required"]
            and not item["body_present"]
            and not item.get("informational")
        ):
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
        "assertions_required": sum(bool(item.get("required")) for item in assertion_checks),
        "assertions_present": sum(item["present"] for item in assertion_checks),
        "assertions_sent": sum(item["sent"] for item in assertion_checks),
    }
    items = [
        {key: value for key, value in item.items() if key not in {"body_present", "reason"}}
        for item in records
    ]
    missing_kinds = {item.get("ref") for item in missing_refs}
    has_no_useful_context = bool(
        missing_kinds.intersection(
            {
                "measurement:verified-measurement",
                "fact:network-fact",
                "observation:question-proof",
                "question-proof",
            }
        )
    )
    next_action = (
        _advisor_preflight_next_action(consultation_type, prompt_data)
        if has_no_useful_context
        else ""
    )
    return {
        "status": status.value,
        "missing_refs": missing_refs,
        "counts": counts,
        "assertions": assertion_checks,
        "items": items,
        **({"next_action": next_action} if next_action else {}),
    }


def _advisor_preflight_next_action(consultation_type: str, prompt_data: Mapping[str, Any]) -> str:
    sample_sha = next(
        (
            _as_text(item.get("sha256"))
            for item in prompt_data.get("samples", ())
            if isinstance(item, Mapping)
            and item.get("verified") is True
            and re.fullmatch(r"[0-9a-f]{64}", _as_text(item.get("sha256")))
        ),
        "",
    )
    if consultation_type == "yara_draft":
        return (
            "Aucune preuve canonique exploitable n'est présente pour ce dossier YARA. Ajoute "
            "une observation d'analyse statique avec son identifiant et son SHA-256, ou fournis "
            "des offsets d'échantillon vérifiés, puis relance yara_draft."
        )
    if consultation_type == "suricata_draft":
        return (
            "Aucun fait réseau mesuré n'est disponible. Lance network.capture.inspect sur la "
            "capture PCAP associée, puis relance suricata_draft."
        )
    if sample_sha:
        return (
            f"Aucune matière canonique pertinente n'est disponible. Lance sample.analyze sur "
            f"{sample_sha}, puis relance cette consultation."
        )
    return (
        "Aucune matière canonique utile n'est disponible. Associe un échantillon ou une capture "
        "réseau à l'enquête, lance l'analyse locale correspondante, puis relance "
        "cette consultation."
    )


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
    body_occurrences: Sequence[tuple[int, int, str, str]] = (),
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
        "body_occurrences": tuple(body_occurrences),
    }


def _body_occurrences_for_content(
    body: str,
    content: str,
    reference_id: str,
    *,
    rendered_content: str | None = None,
) -> tuple[tuple[int, int, str, str], ...]:
    if not content:
        return ()
    displayed = content if rendered_content is None else rendered_content
    start = body.rfind(displayed)
    if start < 0:
        return ()
    return ((start, start + len(displayed), content, reference_id),)


def _joined_body_occurrences(
    pieces: Sequence[str],
    piece_contents: Sequence[tuple[int, str, str, str]],
) -> tuple[tuple[int, int, str, str], ...]:
    by_piece: dict[int, list[tuple[str, str, str]]] = {}
    for index, rendered_content, body_content, reference_id in piece_contents:
        by_piece.setdefault(index, []).append((rendered_content, body_content, reference_id))
    occurrences: list[tuple[int, int, str, str]] = []
    offset = 0
    for index, piece in enumerate(pieces):
        for rendered_content, body_content, reference_id in by_piece.get(index, ()):
            start = piece.rfind(rendered_content)
            if start >= 0:
                absolute_start = offset + start
                occurrences.append(
                    (
                        absolute_start,
                        absolute_start + len(rendered_content),
                        body_content,
                        reference_id,
                    )
                )
        offset += len(piece) + 2
    return tuple(occurrences)


def _render_deduplicated_body(
    item: Mapping[str, Any],
    alias: str,
    seen_bodies: dict[str, list[tuple[str, str, str]]],
) -> tuple[str, list[dict[str, str]]]:
    body = _as_text(item.get("body"))
    raw_occurrences = item.get("body_occurrences")
    occurrences = (
        list(raw_occurrences)
        if isinstance(raw_occurrences, (list, tuple)) and raw_occurrences
        else [(0, len(body), body, _body_reference_id(item))]
        if body
        else []
    )
    rendered: list[str] = []
    references: list[dict[str, str]] = []
    cursor = 0
    for start, end, content, reference_id in sorted(occurrences):
        if (
            type(start) is not int
            or type(end) is not int
            or start < cursor
            or end <= start
            or end > len(body)
        ):
            continue
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        prior = next(
            (entry for entry in seen_bodies.get(digest, ()) if entry[0] == content),
            None,
        )
        rendered.append(body[cursor:start])
        if prior is None:
            rendered.append(body[start:end])
            seen_bodies.setdefault(digest, []).append((content, str(reference_id), alias))
        else:
            rendered.append(f"même corps que ci-dessus : {prior[2]}/{digest}")
            references.append({"alias": prior[2], "id": prior[1], "sha256": digest})
        cursor = end
    rendered.append(body[cursor:])
    return "".join(rendered), references


def _body_reference_id(item: Mapping[str, Any]) -> str:
    extra = item.get("extra")
    if isinstance(extra, Mapping):
        evidence_id = _as_text(extra.get("evidence_id"))
        if evidence_id:
            return evidence_id
    return _as_text(item.get("id"))


def _prioritize_duplicate_measurements(
    selected: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    first_non_measurement: dict[str, int] = {}
    for index, item in enumerate(selected):
        if item.get("kind") == "measurement":
            continue
        for content in _candidate_body_contents(item):
            first_non_measurement.setdefault(content, index)

    moved_before: dict[int, list[dict[str, Any]]] = {}
    moved_ids: set[int] = set()
    for index, item in enumerate(selected):
        if item.get("kind") != "measurement":
            continue
        matching_indexes = [
            first_non_measurement[content]
            for content in _candidate_body_contents(item)
            if content in first_non_measurement
        ]
        if matching_indexes and index > min(matching_indexes):
            target_index = min(matching_indexes)
            moved_before.setdefault(target_index, []).append(item)
            moved_ids.add(id(item))

    ordered: list[dict[str, Any]] = []
    for index, item in enumerate(selected):
        ordered.extend(moved_before.get(index, ()))
        if id(item) not in moved_ids:
            ordered.append(item)
    return ordered


def _candidate_body_contents(item: Mapping[str, Any]) -> tuple[str, ...]:
    raw_occurrences = item.get("body_occurrences")
    if isinstance(raw_occurrences, (list, tuple)) and raw_occurrences:
        return tuple(
            occurrence[2]
            for occurrence in raw_occurrences
            if isinstance(occurrence, (list, tuple))
            and len(occurrence) == 4
            and isinstance(occurrence[2], str)
            and occurrence[2]
        )
    body = _as_text(item.get("body"))
    return (body,) if body else ()


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
            if isinstance(ref, Mapping) and ref.get("kind") in {
                "observation",
                "finding",
                "article",
                "measurement",
            }:
                identifier = _as_text(ref.get("id"))
                if ref.get("kind") == "article" and identifier.startswith("article:"):
                    identifier = identifier.partition(":")[2]
                row = (str(ref["kind"]), identifier)
                if row not in result:
                    result.append(row)
    reference_groups = [data.get("_explicit_assertion_refs", ())]
    reference_groups.extend(
        assertion.get("refs", ())
        for unit in _target_rows(data)
        for assertion in unit.get("assertions", ())
        if isinstance(assertion, Mapping)
    )
    for references in reference_groups:
        if not isinstance(references, (list, tuple)):
            continue
        for ref in references:
            if not isinstance(ref, Mapping) or ref.get("kind") not in {
                "observation",
                "finding",
                "article",
                "measurement",
            }:
                continue
            identifier = _as_text(ref.get("id"))
            if ref.get("kind") == "article" and identifier.startswith("article:"):
                identifier = identifier.partition(":")[2]
            row = (str(ref["kind"]), identifier)
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
    elif (
        source_folded == "virustotal"
        or source_folded.startswith("virustotal.")
        or source_folded.startswith("vt.")
    ):
        product = "VirusTotal"
    else:
        # Unknown provider/tool identifiers are provenance details, not task
        # facts. Keep them in the local manifest and show a neutral label.
        product = "Observation"
    title = next(
        (
            _as_text(row.get("title"))
            for row in excerpts
            if row.get("kind") != "metadata" and _as_text(row.get("title"))
        ),
        "",
    )
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


def _advisor_fragment_bytes(value: object, encoding: str) -> bytes | None:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return bytes(value)
    if isinstance(value, (list, tuple)) and all(
        type(byte) is int and 0 <= byte <= 255 for byte in value
    ):
        return bytes(value)
    if not isinstance(value, str):
        return None
    compact = re.sub(r"\s+", "", value)
    if encoding in {"hex", "hexdump", "bytes"}:
        if compact and len(compact) % 2 == 0 and re.fullmatch(r"(?i)[0-9a-f]+", compact):
            try:
                return bytes.fromhex(compact)
            except ValueError:
                return None
        if encoding != "bytes":
            return None
    if encoding in {"base64", "bytes"}:
        try:
            return base64.b64decode(compact, validate=True)
        except (ValueError, binascii.Error):
            return None
    return None


def _display_source_ref(value: object) -> str:
    if isinstance(value, Mapping):
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _as_text(value) or str(value)


def _terms(value: str) -> set[str]:
    normalized = unicodedata.normalize("NFKD", value.casefold())
    normalized = "".join(char for char in normalized if not unicodedata.combining(char))
    return {
        token.strip(".,:;!?")
        for token in _WORD.findall(normalized)
        if len(token.strip(".,:;!?")) > 2
        and token.strip(".,:;!?") not in _STOP
        and not token.strip(".,:;!?").isdigit()
    }


def _relevance_score(terms: set[str], value: str) -> int:
    if not terms:
        return 0
    candidate = _terms(value)
    return sum(2 if term in candidate else 0 for term in terms)


def _excerpt_relevance_text(excerpt: Mapping[str, Any]) -> str:
    title = _as_text(excerpt.get("title"))
    body = _as_text(excerpt.get("text"))
    if excerpt.get("kind") == "metadata":
        metadata_key, separator, metadata_value = body.partition("=")
        if separator and metadata_key.casefold() in _HASH_METADATA_KEYS:
            return metadata_value
    return f"{title} {body}"


def _is_byte_excerpt(excerpt: Mapping[str, Any]) -> bool:
    return _as_text(excerpt.get("kind")).casefold() in {"hex", "hexdump", "base64", "bytes"}


def _observation_is_sample_backed(item: Mapping[str, Any]) -> bool:
    if (
        _as_text(item.get("source")).casefold() in {"virustotal", "vt"}
        and _as_text(item.get("object_type")).casefold() == "file"
    ):
        return True
    if _as_text(item.get("sample_sha256")):
        return True
    omission_reason = _as_text(item.get("content_omission_reason"))
    if omission_reason.startswith(("sample_", "subject_sample_")):
        return True
    excerpts = item.get("content_excerpts", ())
    if not isinstance(excerpts, (list, tuple)):
        return False
    return any(
        isinstance(excerpt, Mapping)
        and excerpt.get("kind") == "metadata"
        and _as_text(excerpt.get("text")).casefold().startswith("sample_sha256=")
        for excerpt in excerpts
    )


def _canonical_tool_measurement(
    measurement: Mapping[str, Any], evidence: Mapping[str, Any] | None
) -> bool:
    """Identify tool-observed sample values without claiming blob-range proof."""
    if not _as_text(measurement.get("sample_sha256")):
        return False
    if evidence is None:
        # The observation row is not part of this dossier, but the measurement itself names the
        # observation (id and digest) and its extraction context; external data never does.
        measurement_source = _as_text(measurement.get("source")).casefold()
        return bool(
            _as_text(measurement.get("evidence_id"))
            and _as_text(measurement.get("evidence_sha256"))
            and _as_text(measurement.get("extractor_version"))
            and type(measurement.get("offset")) is int
            and measurement.get("offset", -1) >= 0
            and type(measurement.get("length")) is int
            and measurement.get("length", 0) > 0
            and bool(_as_text(measurement.get("value")))
            and measurement.get("content_omission_reason")
            not in {"sample_blob_hash_mismatch", "sample_content_hash_mismatch"}
            and not (
                measurement_source in {"virustotal", "vt"}
                or measurement_source.startswith(("virustotal.", "vt."))
            )
        )
    integrity_failure_reasons = {
        "sample_blob_hash_mismatch",
        "sample_content_hash_mismatch",
    }
    if any(
        _as_text(item.get("content_omission_reason")) in integrity_failure_reasons
        for item in (measurement, evidence)
    ):
        return False
    evidence_id = _as_text(measurement.get("evidence_id"))
    evidence_sha256 = _as_text(measurement.get("evidence_sha256"))
    evidence_sample_sha256 = _as_text(evidence.get("sample_sha256"))
    evidence_sample_sha256s = evidence.get("sample_sha256s", ())
    observed_hashes = {evidence_sample_sha256} if evidence_sample_sha256 else set()
    if isinstance(evidence_sample_sha256s, (list, tuple)):
        observed_hashes.update(
            _as_text(value) for value in evidence_sample_sha256s if _as_text(value)
        )
    # Archived and production observation rows do not always repeat the sample digest, the
    # tool-name ``source`` or a blob-read flag: the tool-derived ``kind`` is enough to separate
    # our own measurement from VT or other external data. A listed digest must still agree.
    evidence_source = _as_text(evidence.get("source")).casefold()
    tool_derived = evidence_source.startswith("autowork.") or (
        not evidence_source and _as_text(evidence.get("kind")).startswith("binary_")
    )
    return bool(
        evidence_id
        and evidence_id == _as_text(evidence.get("evidence_id"))
        and evidence_sha256
        and evidence_sha256 == _as_text(evidence.get("sha256"))
        and (not observed_hashes or _as_text(measurement.get("sample_sha256")) in observed_hashes)
        and tool_derived
        and evidence.get("blob_read_verified") is not False
        and type(measurement.get("offset")) is int
        and measurement.get("offset", -1) >= 0
        and type(measurement.get("length")) is int
        and measurement.get("length", 0) > 0
        and bool(_as_text(measurement.get("value")) or _as_text(measurement.get("sample_material")))
    )


def _measurement_value_status(
    measurement: Mapping[str, Any],
    evidence: Mapping[str, Any] | None,
    *,
    sample_backed: bool,
) -> tuple[str, str]:
    if not sample_backed:
        return "non_sample_measurement", "not_applicable"
    if measurement.get("range_verified") is True:
        return "verified_sample_range", "verified_against_canonical_blob"
    if _canonical_tool_measurement(measurement, evidence):
        return "canonical_tool_observation", "not_reverified"
    source = _as_text(
        evidence.get("source") if evidence is not None else measurement.get("source")
    ).casefold()
    if source in {"virustotal", "vt"} or source.startswith(("virustotal.", "vt.")):
        return "external_unverified", "not_verified"
    return "provenance_insufficient", "not_verified"


def _numeric_targets_conflict(task: str, candidate: str) -> bool:
    requested = set(re.findall(r"\b\d{3,}\b", task))
    actual = set(re.findall(r"\b\d{3,}\b", candidate))
    return bool(requested and requested.isdisjoint(actual))


def _measurement_explicitly_targeted(measurement: Mapping[str, Any], task: str) -> bool:
    if re.search(
        r"\b(?:all|every|each|tous?|toutes|chaque)\b.{0,64}\b(?:verified|measured|measurements?|mesures?)\b",
        task,
        re.IGNORECASE,
    ) or re.search(
        r"\b(?:verified|measured|measurements?|mesures?)\b.{0,64}\b(?:all|every|each|tous?|toutes|chaque)\b",
        task,
        re.IGNORECASE,
    ):
        return True
    measurement_id = _as_text(measurement.get("measurement_id"))
    if measurement_id and re.search(
        rf"(?<![\w]){re.escape(measurement_id)}(?![\w])", task, re.IGNORECASE
    ):
        return True
    offset = measurement.get("offset")
    if (
        type(offset) is int
        and offset >= 0
        and re.search(
            rf"\b(?:offset|address|position)\s*(?:[:=#]\s*)?{offset}\b", task, re.IGNORECASE
        )
    ):
        return True
    value = _as_text(measurement.get("value"))
    return bool(value and value.casefold() in task.casefold())


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


def _contradiction_is_requested(data: Mapping[str, Any], task: str) -> bool:
    return _requires_contradiction(task) or any(
        isinstance(item, Mapping) and item.get("nature") == "contradiction"
        for item in data.get("assertions", ())
    )


def _requested_contradiction_finding_ids(data: Mapping[str, Any], task: str) -> set[str]:
    finding_ids = {value.casefold() for value in _FINDING_REFERENCE_ID.findall(task)}
    for item in data.get("assertions", ()):
        if not isinstance(item, Mapping) or item.get("nature") != "contradiction":
            continue
        finding_id = _as_text(item.get("finding_id"))
        if finding_id:
            finding_ids.add(finding_id.casefold())
        refs = item.get("refs", ())
        if isinstance(refs, (list, tuple)):
            finding_ids.update(
                _as_text(ref.get("id")).casefold()
                for ref in refs
                if isinstance(ref, Mapping)
                and ref.get("kind") == "finding"
                and _as_text(ref.get("id"))
            )
    return finding_ids


def _contradiction_is_required(data: Mapping[str, Any], task: str) -> bool:
    # A refutation is never a hard prerequisite: a missing refuted finding is information
    # (recorded as an informational manifest entry and stated in the dossier), not missing
    # evidence. A finding id merely mentioned in the question does not make it blocking.
    return False


def _finding_matches_requested_claims(item: Mapping[str, Any], claims: set[str]) -> bool:
    if not claims:
        return True
    references = item.get("claim_refs", ())
    if not isinstance(references, (list, tuple)):
        return False
    return bool({_as_text(value).casefold() for value in references} & claims)


__all__ = ["AdvisorDossier", "build_advisor_dossier", "extract_content_excerpts"]
