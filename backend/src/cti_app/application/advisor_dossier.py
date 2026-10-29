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
import logging
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
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
_ADVISOR_DOSSIER_SOFT_LIMIT_BYTES = 400_000
_ADVISOR_DOSSIER_HARD_LIMIT_BYTES = 2_000_000
_ADVISOR_DOSSIER_EXCERPT_LIMIT_BYTES = 12 * 1024
_BASE64_RUN = re.compile(r"[A-Za-z0-9+/]{64,}={0,2}")
logger = logging.getLogger(__name__)


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

    @property
    def byte_size(self) -> int:
        return len(self.text.encode("utf-8"))

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


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

    for sample in prompt_data.get("samples", ()):
        if not isinstance(sample, Mapping) or sample.get("verified") is not True:
            continue
        if envelope is not None and _sample_disclosure_restriction_sources(sample, envelope):
            continue
        sample_id = _as_text(sample.get("sample_id"))
        association_id = _as_text(sample.get("association_id"))
        sample_sha256 = _as_text(sample.get("sha256"))
        blob_id = _as_text(sample.get("blob_id"))
        byte_length = sample.get("byte_length")
        mime_type = _as_text(sample.get("mime_type"))
        content = _as_text(sample.get("content_base64"))
        fragment_length = sample.get("fragment_length", byte_length)
        omitted_range = _as_text(sample.get("omitted_range"))
        lines = [
            "Verified sample provenance:",
            f"sample_id={sample_id}",
            f"association_id={association_id}",
            f"blob_id={blob_id}",
            f"blob_sha256={_as_text(sample.get('blob_sha256'))}",
            f"sha256={sample_sha256}",
            f"expected_hash={_as_text(sample.get('expected_hash'))}",
            f"source_object_id={_as_text(sample.get('source_object_id'))}",
            f"encoding=base64; byte_length={byte_length}; offset=0; "
            f"length={fragment_length}; mime_type={mime_type}",
            f"association_status={_as_text(sample.get('association_status'))}",
            f"content_status={_as_text(sample.get('content_status'))}",
            f"state={_as_text(sample.get('state'))}",
            f"origin={_as_text(sample.get('origin'))}",
            f"original_name={_as_text(sample.get('original_name'))}",
            f"source_service={_as_text(sample.get('source_service'))}",
            f"tlp={_as_text(sample.get('tlp'))}",
            f"external_llm_allowed={str(sample.get('external_llm_allowed')).lower()}",
        ]
        evidence_ids = sample.get("evidence_ids")
        if isinstance(evidence_ids, (list, tuple)):
            lines.append("evidence_ids=" + ",".join(_as_text(item) for item in evidence_ids))
        evidence_blob_ids = sample.get("evidence_blob_ids")
        if isinstance(evidence_blob_ids, (list, tuple)):
            lines.append(
                "evidence_blob_ids=" + ",".join(_as_text(item) for item in evidence_blob_ids)
            )
        evidence_hashes = sample.get("evidence_sha256s")
        if isinstance(evidence_hashes, (list, tuple)):
            lines.append("evidence_sha256s=" + ",".join(_as_text(item) for item in evidence_hashes))
        related = sample.get("related_sample_sha256")
        if isinstance(related, (list, tuple)):
            lines.append("related_sample_sha256=" + ",".join(_as_text(item) for item in related))
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
            lines.extend(["encoding=base64", "content_base64:", content])
        if omitted_range:
            lines.append(f"omitted_range={omitted_range}")
        omission_reason = sample.get("content_omission_reason")
        if isinstance(omission_reason, str) and omission_reason:
            lines.append(f"content_omission_reason={omission_reason}")
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
                    "omitted_range": omitted_range,
                    "content_status": _as_text(sample.get("content_status")),
                    "content_omission_reason": _as_text(omission_reason),
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
        explicitly_referenced_observation = ("observation", identifier) in explicitly_referenced
        label = _human_evidence_label(item, excerpts)
        row_score = _relevance_score(task_terms, label)
        for excerpt in excerpts:
            body = _as_text(excerpt.get("text"))
            title = _as_text(excerpt.get("title")) or "Extrait"
            score = _relevance_score(task_terms, _excerpt_relevance_text(excerpt))
            if score > 0 or explicitly_referenced_observation:
                pieces.append(f"{title}:\n{_quote(body)}")
                row_score = max(row_score, score, 1 if explicitly_referenced_observation else 0)
        for fact in (*measurement_rows, *typed_measurements):
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
            if provenance:
                fact_text += " [" + "; ".join(provenance) + "]"
            score = _relevance_score(task_terms, fact_text)
            if _numeric_targets_conflict(relevance_text, fact_text):
                score = 0
            if score > 0 or explicitly_referenced_observation:
                pieces.append(f"Mesure observée: {fact_text}")
                row_score = max(row_score, score, 1)
        if explicitly_referenced_observation and not pieces and excerpts:
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
            and contradiction_requested
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
        kind = _as_text(item.get("kind")) or "mesure"
        offset = item.get("offset")
        sample_sha256 = _as_text(item.get("sample_sha256"))
        source_evidence_sha256 = _as_text(item.get("evidence_sha256"))
        declared_encoding = _as_text(item.get("encoding")).casefold()
        byte_encoding = (
            "base64"
            if rendered_as_base64
            else declared_encoding
            if declared_encoding in {"hex", "hexdump", "base64", "bytes"}
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
                    + (
                        f", offset {offset}"
                        + (f"; {'; '.join(byte_metadata)}" if byte_metadata else "")
                        if type(offset) is int
                        else f"; {'; '.join(byte_metadata)}"
                        if byte_metadata
                        else ""
                    ),
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
                        "length": byte_length,
                        "byte_length": byte_length,
                        "encoding": byte_encoding,
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
                    key=lambda row: (
                        _relevance_score(
                            task_terms,
                            _excerpt_relevance_text(row),
                        ),
                        row.get("kind") != "metadata",
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

    # Keep task-relevant facts first; explicit size limits are applied after
    # this relevance selection so ordinary small dossiers retain full bodies.
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

    dossier = _render_advisor_dossier(
        selected,
        unavailable_observations=unavailable_observations,
        contradiction_requested=contradiction_requested,
        explicit_claims=explicit_claims,
        prompt_data=prompt_data,
    )
    initial_dossier_bytes = dossier.byte_size
    compacted_body_count = 0
    reference_only_body_count = 0
    hard_omitted_count = 0

    if initial_dossier_bytes <= _ADVISOR_DOSSIER_SOFT_LIMIT_BYTES and any(
        item.get("kind") in {"observation", "measurement"}
        and len(_as_text(item.get("body")).encode("utf-8")) > _ADVISOR_DOSSIER_EXCERPT_LIMIT_BYTES
        for item in selected
    ):
        selected = _compact_advisor_candidates(selected, only_large_observation_bodies=True)
        dossier = _render_advisor_dossier(
            selected,
            unavailable_observations=unavailable_observations,
            contradiction_requested=contradiction_requested,
            explicit_claims=explicit_claims,
            prompt_data=prompt_data,
        )
        compacted_body_count = sum(
            bool(item.get("_compaction", {}).get("body_truncated")) for item in selected
        )

    if initial_dossier_bytes > _ADVISOR_DOSSIER_SOFT_LIMIT_BYTES:
        selected = _compact_advisor_candidates(selected)
        volume_note = (
            "TRONQUÉ explicitement par la limite volumétrique du dossier: "
            f"taille initiale={initial_dossier_bytes} octets; contenu et références conservés."
        )
        dossier = _render_advisor_dossier(
            selected,
            unavailable_observations=unavailable_observations,
            contradiction_requested=contradiction_requested,
            explicit_claims=explicit_claims,
            prompt_data=prompt_data,
            volume_note=volume_note,
        )
        compacted_body_count = sum(
            bool(item.get("_compaction", {}).get("body_truncated")) for item in selected
        )

        estimated_bytes = dossier.byte_size
        reference_candidates = sorted(
            (
                item
                for item in selected
                if not item.get("_compaction", {}).get("body_reused_from_alias")
            ),
            key=lambda item: len(_advisor_candidate_piece(item).encode("utf-8")),
            reverse=True,
        )
        for item in reference_candidates:
            if estimated_bytes <= _ADVISOR_DOSSIER_SOFT_LIMIT_BYTES - 1_024:
                break
            previous_size = len(_advisor_candidate_piece(item).encode("utf-8"))
            previous_meta = dict(item.get("_compaction", {}))
            referenced_body, reference_meta = _reference_advisor_body(
                item, alias=None, reason="limite souple de 400 Ko"
            )
            reference_size = len(
                _advisor_candidate_piece({**item, "body": referenced_body}).encode("utf-8")
            )
            if reference_size >= previous_size:
                continue
            item["body"] = referenced_body
            _carry_advisor_compaction_provenance(reference_meta, previous_meta)
            item["_compaction"] = reference_meta
            estimated_bytes -= previous_size - reference_size
            reference_only_body_count += 1
        if reference_only_body_count:
            volume_note = (
                "TRONQUÉ explicitement par la limite volumétrique du dossier: "
                f"taille initiale={initial_dossier_bytes} octets; "
                f"{reference_only_body_count} corps remplacé(s) par des références."
            )
            dossier = _render_advisor_dossier(
                selected,
                unavailable_observations=unavailable_observations,
                contradiction_requested=contradiction_requested,
                explicit_claims=explicit_claims,
                prompt_data=prompt_data,
                volume_note=volume_note,
            )

        while dossier.byte_size > _ADVISOR_DOSSIER_HARD_LIMIT_BYTES:
            changed = False
            for item in selected:
                if (
                    item.get("_hard_reference_only")
                    or item.get("_compaction", {}).get("reference_only")
                    or item.get("_compaction", {}).get("body_reused_from_alias")
                ):
                    continue
                previous_meta = dict(item.get("_compaction", {}))
                referenced_body, reference_meta = _reference_advisor_body(
                    item, alias=None, reason="limite dure de 2 Mo"
                )
                if len(referenced_body.encode("utf-8")) >= len(
                    _as_text(item.get("body")).encode("utf-8")
                ):
                    continue
                item["body"] = referenced_body
                _carry_advisor_compaction_provenance(reference_meta, previous_meta)
                item["_compaction"] = reference_meta
                item["_hard_reference_only"] = True
                reference_only_body_count += 1
                changed = True
            if not changed:
                # Extremely high card counts are bounded by shortening labels;
                # IDs, source hashes, and full-body references remain in each card.
                for item in selected:
                    label = _as_text(item.get("label"))
                    if len(label) > 64:
                        item["label"] = _clip_utf8(label, 64) + "…"
                        changed = True
                if not changed:
                    break
            volume_note = (
                "TRONQUÉ explicitement par la limite dure de 2 Mo: "
                f"taille initiale={initial_dossier_bytes} octets; "
                f"{reference_only_body_count} corps remplacé(s) par des références."
            )
            dossier = _render_advisor_dossier(
                selected,
                unavailable_observations=unavailable_observations,
                contradiction_requested=contradiction_requested,
                explicit_claims=explicit_claims,
                prompt_data=prompt_data,
                volume_note=volume_note,
            )

        if dossier.byte_size > _ADVISOR_DOSSIER_HARD_LIMIT_BYTES:
            selected, dossier, hard_omitted_count = _trim_advisor_dossier_to_limit(
                selected,
                limit_bytes=_ADVISOR_DOSSIER_HARD_LIMIT_BYTES,
                unavailable_observations=unavailable_observations,
                contradiction_requested=contradiction_requested,
                explicit_claims=explicit_claims,
                prompt_data=prompt_data,
                initial_bytes=initial_dossier_bytes,
                required_references=explicitly_referenced,
            )

    logger.info(
        "Advisor dossier volume",
        extra={
            "advisor_dossier_input_bytes": initial_dossier_bytes,
            "advisor_dossier_output_bytes": dossier.byte_size,
            "advisor_dossier_candidate_count": len(selected),
            "advisor_dossier_compacted_body_count": compacted_body_count,
            "advisor_dossier_reference_only_body_count": reference_only_body_count,
            "advisor_dossier_hard_omitted_candidate_count": hard_omitted_count,
            "advisor_dossier_hard_limit_reached": (
                dossier.byte_size >= _ADVISOR_DOSSIER_HARD_LIMIT_BYTES
            ),
        },
    )
    if dossier.byte_size > _ADVISOR_DOSSIER_HARD_LIMIT_BYTES:
        logger.error(
            "Advisor dossier remains above hard byte limit",
            extra={
                "advisor_dossier_input_bytes": initial_dossier_bytes,
                "advisor_dossier_output_bytes": dossier.byte_size,
                "advisor_dossier_hard_limit_bytes": _ADVISOR_DOSSIER_HARD_LIMIT_BYTES,
                "advisor_dossier_candidate_count": len(selected),
            },
        )
    return dossier


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
                blob_read_failure_reason=_as_text(item.get("blob_read_failure_reason")),
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
        elif item.get("informational"):
            mark(AdvisorEvidenceStatus.INFORMATIONAL, item["reason"])
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
            item["required"]
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
        elif not item["required"] and not item["body_present"] and not item.get("informational"):
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


def _advisor_source_body_ref(item: Mapping[str, Any]) -> str:
    kind = _as_text(item.get("kind"))
    identifier = _as_text(item.get("id"))
    extra = item.get("extra")
    if kind == "observation":
        return f"observation {identifier}"
    if isinstance(extra, Mapping):
        observation_id = _as_text(extra.get("evidence_id"))
        if observation_id:
            return f"observation {observation_id}"
        source_ref = extra.get("source_ref")
        if isinstance(source_ref, Mapping):
            source_kind = _as_text(source_ref.get("kind"))
            source_id = _as_text(source_ref.get("id"))
            if source_kind and source_id:
                return f"{source_kind} {source_id}"
    return f"{kind or 'source'} {identifier}".strip()


def _advisor_base64_payload(body: str) -> tuple[str, bytes] | None:
    if "base64" not in body.casefold():
        return None
    matches = _BASE64_RUN.findall(body)
    for payload in sorted(matches, key=len, reverse=True):
        try:
            return payload, base64.b64decode(payload, validate=True)
        except (ValueError, binascii.Error):
            continue
    return None


def _clip_utf8(value: str, max_bytes: int, *, tail: bool = False) -> str:
    encoded = value.encode("utf-8")
    selected = encoded[-max_bytes:] if tail else encoded[:max_bytes]
    return selected.decode("utf-8", errors="ignore")


def _compact_advisor_body(
    item: Mapping[str, Any], body: str, *, max_bytes: int
) -> tuple[str, dict[str, Any]]:
    """Return an explicit, source-addressable excerpt of a large dossier body."""
    encoded = body.encode("utf-8")
    body_sha256 = hashlib.sha256(encoded).hexdigest()
    source_sha256 = _as_text(item.get("sha256")) or "UNKNOWN"
    source_ref = _advisor_source_body_ref(item)
    extra = item.get("extra")
    source_offset = extra.get("offset") if isinstance(extra, Mapping) else None
    source_length = (
        extra.get("byte_length", extra.get("length")) if isinstance(extra, Mapping) else None
    )
    if source_offset is None:
        offset_match = re.search(r"\boffset(?:_bytes)?\s*(?:=|:|\s)\s*(\d+)", body, re.I)
        source_offset = int(offset_match.group(1)) if offset_match else None
    if source_length is None:
        length_match = re.search(r"\bbyte_length\s*=\s*(\d+)", body, re.I)
        source_length = int(length_match.group(1)) if length_match else None
    source_location = (
        f"; source byte offset={source_offset}"
        if type(source_offset) is int and source_offset >= 0
        else ""
    )
    if type(source_length) is int and source_length >= 0:
        source_location += f"; source byte length={source_length}"
    marker = (
        f"TRONQUÉ explicitement: offset UTF-8 0..{len(encoded)}, longueur={len(encoded)} octets; "
        f"sha256_corps={body_sha256}; sha256={source_sha256}; "
        f"sha256_source={source_sha256}{source_location}; "
        f"corps complet : {source_ref}"
    )

    base64_payload = _advisor_base64_payload(body)
    if base64_payload is not None:
        payload, raw_payload = base64_payload
        payload_matches = _BASE64_RUN.findall(body)
        decoded_bytes = 0
        for match in payload_matches:
            try:
                decoded_bytes += len(base64.b64decode(match, validate=True))
            except (ValueError, binascii.Error):
                continue
        payload_sha256 = hashlib.sha256(raw_payload).hexdigest()
        sample_size = min(192, max(32, max_bytes // 48), len(payload) // 2)
        prefix = payload[:sample_size]
        suffix = payload[-sample_size:]
        payload_start_char = body.find(payload)
        payload_start = len(body[:payload_start_char].encode("utf-8"))
        payload_end = payload_start + len(payload.encode("ascii"))
        fixed_text = (
            f"Bruit base64 résumé: {len(payload_matches)} occurrence(s), "
            f"{sum(len(value) for value in payload_matches)} caractères base64, "
            f"{decoded_bytes} octets décodés au total; "
            f"sha256_payload_échantillonné={payload_sha256}."
        )
        payload_matches_with_offsets = list(_BASE64_RUN.finditer(body))
        context_segments: list[tuple[str, int, int]] = []
        cursor = 0
        cursor_bytes = 0
        for match in payload_matches_with_offsets:
            if match.start() > cursor:
                segment = body[cursor : match.start()]
                start_byte = cursor_bytes
                segment_bytes = len(segment.encode("utf-8"))
                context_segments.append((segment, start_byte, start_byte + segment_bytes))
                cursor_bytes += segment_bytes
            cursor_bytes += len(match.group().encode("ascii"))
            cursor = match.end()
        if cursor < len(body):
            segment = body[cursor:]
            start_byte = cursor_bytes
            context_segments.append(
                (segment, start_byte, start_byte + len(segment.encode("utf-8")))
            )
        fixed_bytes = (
            len(fixed_text.encode("utf-8"))
            + len(prefix.encode("ascii"))
            + len(suffix.encode("ascii"))
            + len(marker.encode("utf-8"))
            + 512
        )
        context_budget = max(0, max_bytes - fixed_bytes)
        context_size = sum(
            len(segment.encode("utf-8")) for segment, _start, _end in context_segments
        )
        context_was_truncated = context_size > context_budget
        shown_context: list[tuple[str, int, int]] = []
        if not context_was_truncated:
            shown_context = context_segments
        elif context_segments:
            first_text, first_start, _first_end = context_segments[0]
            last_text, _last_start, last_end = context_segments[-1]
            side = max(0, context_budget // 2)
            first_excerpt = _clip_utf8(first_text, side)
            last_excerpt = _clip_utf8(last_text, side, tail=True)
            first_excerpt_bytes = len(first_excerpt.encode("utf-8"))
            last_excerpt_bytes = len(last_excerpt.encode("utf-8"))
            if first_excerpt:
                shown_context.append(
                    (first_excerpt, first_start, first_start + first_excerpt_bytes)
                )
            if last_excerpt:
                last_excerpt_start = last_end - last_excerpt_bytes
                if shown_context and last_excerpt_start < shown_context[-1][2]:
                    shown_context[-1] = (
                        first_excerpt,
                        first_start,
                        first_start + first_excerpt_bytes,
                    )
                else:
                    shown_context.append((last_excerpt, last_excerpt_start, last_end))
        lines = [fixed_text]
        ranges: list[list[int]] = []
        for context, context_start, context_end in shown_context:
            lines.append(f"Contexte UTF-8 {context_start}..{context_end}:")
            lines.append(context)
            ranges.append([context_start, context_end])
        if context_was_truncated:
            lines.append(
                "Contexte adjacent tronqué explicitement; les autres séquences base64 "
                "sont résumées par les statistiques ci-dessus."
            )
        if prefix == suffix:
            sample_lines = [
                f"Échantillon début/fin identique (offsets UTF-8 "
                f"{payload_start}..{payload_start + len(prefix)} et "
                f"{payload_start + len(payload) - len(suffix)}..{payload_end}): {prefix}"
            ]
        else:
            sample_lines = [
                f"Échantillon début (offset UTF-8 "
                f"{payload_start}..{payload_start + len(prefix)}): {prefix}",
                f"Échantillon fin (offset UTF-8 "
                f"{payload_start + len(payload) - len(suffix)}..{payload_end}): {suffix}",
            ]
        lines.extend(sample_lines)
        lines.append(marker)
        body = "\n".join(lines)
        ranges.extend(
            [
                [payload_start, payload_start + len(prefix)],
                [payload_start + len(payload) - len(suffix), payload_end],
            ]
        )
        if len(body.encode("utf-8")) > max_bytes:
            lines = [
                fixed_text,
                "Contexte adjacent tronqué explicitement pour respecter la taille maximale "
                "de l'extrait; les séquences base64 supplémentaires sont résumées ci-dessus.",
                *sample_lines,
                marker,
            ]
            body = "\n".join(lines)
            ranges = [
                [payload_start, payload_start + len(prefix)],
                [payload_start + len(payload) - len(suffix), payload_end],
            ]
        return body, {
            "body_truncated": True,
            "source_body_sha256": body_sha256,
            "source_body_bytes": len(encoded),
            "displayed_excerpt_ranges_utf8": ranges,
            "full_body_ref": source_ref,
            "payload_sha256": payload_sha256,
            "payload_bytes": len(raw_payload),
        }

    folded = f"{_as_text(item.get('label'))} {body}".casefold()
    if any(
        token in folded for token in ("strings", "decoded", "décod", "chaînes", "valeurs décodées")
    ):
        unique_line_digests: set[bytes] = set()
        line_count = 0
        longest_line = 0
        unique_count_capped = False
        for match in re.finditer(r"[^\n]+", body):
            line = match.group().strip().removeprefix("|").strip()
            if not line:
                continue
            line_count += 1
            longest_line = max(longest_line, len(line.encode("utf-8")))
            if len(unique_line_digests) < 8_192:
                unique_line_digests.add(hashlib.sha256(line.encode("utf-8")).digest())
            else:
                unique_count_capped = True
        if line_count >= 5:
            indices = {
                0,
                line_count // 4,
                line_count // 2,
                (3 * line_count) // 4,
                line_count - 1,
            }
            samples: list[str] = []
            position = 0
            for match in re.finditer(r"[^\n]+", body):
                line = match.group().strip().removeprefix("|").strip()
                if line:
                    if position in indices:
                        samples.append(_clip_utf8(line, min(384, max_bytes // 32)))
                    position += 1
            distinct = (
                f">={len(unique_line_digests)}"
                if unique_count_capped
                else str(len(unique_line_digests))
            )
            compacted = "\n".join(
                (
                    f"Sortie strings résumée: {line_count} ligne(s), {distinct} distincte(s), "
                    f"{len(encoded)} octets UTF-8, ligne maximale={longest_line} octets; "
                    f"sha256_corps={body_sha256}.",
                    "Échantillon de lignes (positions réparties):",
                    *(f"- {sample}" for sample in samples),
                    marker,
                )
            )
            return compacted, {
                "body_truncated": True,
                "source_body_sha256": body_sha256,
                "source_body_bytes": len(encoded),
                "displayed_excerpt_ranges_utf8": [],
                "full_body_ref": source_ref,
                "sampled_line_count": len(samples),
                "unique_line_count": distinct,
            }

    marker_bytes = len(marker.encode("utf-8")) + 4
    side_bytes = max(64, (max_bytes - marker_bytes) // 2)
    head = _clip_utf8(body, side_bytes)
    tail = _clip_utf8(body, side_bytes, tail=True)
    head_bytes = len(head.encode("utf-8"))
    tail_start = max(0, len(encoded) - len(tail.encode("utf-8")))
    compacted = f"{head}\n\n[{marker}]\n\n{tail}"
    return compacted, {
        "body_truncated": True,
        "source_body_sha256": body_sha256,
        "source_body_bytes": len(encoded),
        "displayed_excerpt_ranges_utf8": [[0, head_bytes], [tail_start, len(encoded)]],
        "full_body_ref": source_ref,
    }


def _reference_advisor_body(
    item: Mapping[str, Any], *, alias: str | None, reason: str
) -> tuple[str, dict[str, Any]]:
    original = _as_text(item.get("body"))
    encoded = original.encode("utf-8")
    body_sha256 = hashlib.sha256(encoded).hexdigest()
    source_ref = _advisor_source_body_ref(item)
    identity = f"{_as_text(item.get('kind'))} {_as_text(item.get('id'))}".strip()
    reference = f" déjà présenté sous [{alias}]" if alias else " non recopié"
    body = (
        f"CORPS TRONQUÉ explicitement:{reference}; {reason}; "
        f"identité={identity}; "
        f"longueur={len(encoded)} octets UTF-8; sha256_corps={body_sha256}; "
        f"sha256_source={_as_text(item.get('sha256')) or 'UNKNOWN'}; "
        f"corps complet : {source_ref}"
    )
    return body, {
        "body_truncated": True,
        "reference_only": True,
        "source_body_sha256": body_sha256,
        "source_body_bytes": len(encoded),
        "displayed_excerpt_ranges_utf8": [],
        "full_body_ref": source_ref,
        **({"body_reused_from_alias": alias} if alias else {}),
    }


def _advisor_candidate_piece(item: Mapping[str, Any]) -> str:
    alias = _as_text(item.get("_alias")) or "E"
    return f"[{alias}] {_as_text(item.get('label'))}\n{_quote(_as_text(item.get('body')))}"


def _carry_advisor_compaction_provenance(
    target: dict[str, Any], previous: Mapping[str, Any]
) -> None:
    for key in (
        "source_body_sha256",
        "source_body_bytes",
        "full_body_ref",
        "payload_sha256",
        "payload_bytes",
    ):
        if key in previous:
            target[key] = previous[key]


def _reuse_duplicate_payload_body(
    item: Mapping[str, Any],
    original_body: str,
    payload: tuple[str, bytes],
    *,
    alias: str,
    max_bytes: int,
) -> tuple[str, dict[str, Any]]:
    payload_text, raw_payload = payload
    payload_start_char = original_body.find(payload_text)
    payload_end_char = payload_start_char + len(payload_text)
    payload_start = len(original_body[:payload_start_char].encode("utf-8"))
    payload_end = payload_start + len(payload_text.encode("ascii"))
    body_bytes = original_body.encode("utf-8")
    body_sha256 = hashlib.sha256(body_bytes).hexdigest()
    payload_sha256 = hashlib.sha256(raw_payload).hexdigest()
    source_ref = _advisor_source_body_ref(item)
    source_sha256 = _as_text(item.get("sha256")) or "UNKNOWN"
    identity = f"{_as_text(item.get('kind'))} {_as_text(item.get('id'))}".strip()
    prefix = original_body[:payload_start_char]
    suffix = original_body[payload_end_char:]
    marker = (
        f"PAYLOAD TRONQUÉ explicitement: base64 déjà présenté sous [{alias}]; "
        f"offset UTF-8 {payload_start}..{payload_end}; "
        f"longueur du corps={len(body_bytes)} octets; {len(raw_payload)} octets décodés; "
        f"sha256_payload={payload_sha256}; "
        f"identité={identity}; sha256_corps={body_sha256}; sha256_source={source_sha256}; "
        f"corps complet : {source_ref}"
    )
    available_bytes = max(0, max_bytes - len(marker.encode("utf-8")) - 8)
    prefix_bytes = len(prefix.encode("utf-8"))
    suffix_bytes = len(suffix.encode("utf-8"))
    if prefix_bytes + suffix_bytes > available_bytes:
        side = max(0, available_bytes // 2 - 64)
        shown_prefix = _clip_utf8(prefix, side)
        shown_suffix = _clip_utf8(suffix, side, tail=True)
        prefix_shown_bytes = len(shown_prefix.encode("utf-8"))
        suffix_shown_bytes = len(shown_suffix.encode("utf-8"))
        prefix_range = [0, prefix_shown_bytes]
        suffix_start = payload_end + suffix_bytes - suffix_shown_bytes
        suffix_range = [suffix_start, payload_end + suffix_bytes]
        context_note = (
            f"Contexte autour du payload également tronqué: offsets UTF-8 "
            f"{prefix_range[0]}..{prefix_range[1]} et "
            f"{suffix_range[0]}..{suffix_range[1]}; corps={len(body_bytes)} octets."
        )
        body = f"{shown_prefix}\n[{context_note}]\n{marker}\n{shown_suffix}"
        ranges = [prefix_range, suffix_range]
    else:
        body = f"{prefix}\n[{marker}]\n{suffix}"
        ranges = [[0, prefix_bytes], [payload_end, payload_end + suffix_bytes]]
    return body, {
        "body_truncated": True,
        "source_body_sha256": body_sha256,
        "source_body_bytes": len(body_bytes),
        "displayed_excerpt_ranges_utf8": ranges,
        "full_body_ref": source_ref,
        "payload_sha256": payload_sha256,
        "payload_bytes": len(raw_payload),
        "body_reused_from_alias": alias,
    }


def _advisor_hard_limit_note(
    initial_bytes: int, omitted: Sequence[Mapping[str, Any]], *, index_limit_bytes: int = 450_000
) -> str:
    ref_lines = [
        f"{_as_text(item.get('kind'))} {_as_text(item.get('id'))} "
        f"sha256={_as_text(item.get('sha256'))}"
        for item in omitted
    ]
    digest = hashlib.sha256("\n".join(ref_lines).encode("utf-8")).hexdigest()
    index_lines: list[str] = []
    index_bytes = 0
    included = 0
    for line in ref_lines:
        size = len((line + "\n").encode("utf-8"))
        if index_bytes + size > index_limit_bytes:
            break
        index_lines.append(line)
        index_bytes += size
        included += 1
    omitted_from_index = len(ref_lines) - included
    note = (
        "TRONQUÉ explicitement par la limite dure de 2 Mo: "
        f"taille initiale={initial_bytes} octets; {len(omitted)} carte(s) résumée(s) "
        f"dans l'index des sources ci-dessous; sha256_liste_sources={digest}."
    )
    if index_lines:
        note += "\nRéférences canoniques résumées (kind id sha256):\n" + "\n".join(index_lines)
    if omitted_from_index:
        note += (
            f"\n{omitted_from_index} référence(s) supplémentaires non détaillées dans l'index; "
            f"elles sont comprises dans sha256_liste_sources={digest}."
        )
    return note


def _trim_advisor_dossier_to_limit(
    selected: list[dict[str, Any]],
    *,
    limit_bytes: int,
    unavailable_observations: set[str],
    contradiction_requested: bool,
    explicit_claims: set[str],
    prompt_data: Mapping[str, Any],
    initial_bytes: int,
    required_references: set[tuple[str, str]],
) -> tuple[list[dict[str, Any]], AdvisorDossier, int]:
    """Keep a bounded, source-addressable dossier when card overhead dominates."""
    required_indices = {
        index
        for index, item in enumerate(selected)
        if (item.get("kind"), item.get("id")) in required_references
    }
    alias_indices = {
        _as_text(item.get("_alias")): index
        for index, item in enumerate(selected)
        if _as_text(item.get("_alias"))
    }
    pending = list(required_indices)
    while pending:
        index = pending.pop()
        metadata = selected[index].get("_compaction")
        reused_alias = (
            _as_text(metadata.get("body_reused_from_alias"))
            if isinstance(metadata, Mapping)
            else ""
        )
        dependency = alias_indices.get(reused_alias)
        if dependency is not None and dependency not in required_indices:
            required_indices.add(dependency)
            pending.append(dependency)

    def render(indices: set[int], omitted: list[dict[str, Any]]) -> AdvisorDossier:
        rows = [item for index, item in enumerate(selected) if index in indices]
        note = _advisor_hard_limit_note(initial_bytes, omitted)
        return _render_advisor_dossier(
            rows,
            unavailable_observations=unavailable_observations,
            contradiction_requested=contradiction_requested,
            explicit_claims=explicit_claims,
            prompt_data=prompt_data,
            volume_note=note,
        )

    optional_indices = [index for index in range(len(selected)) if index not in required_indices]
    base_dossier = render(required_indices, [selected[index] for index in optional_indices])
    if base_dossier.byte_size > limit_bytes:
        # If mandatory references alone exceed the transport cap, keep the cap
        # and let completeness preflight report the omitted required proof.
        required_indices = set()
        optional_indices = list(range(len(selected)))

    low = 0
    high = len(optional_indices)
    best_indices: set[int] = set(required_indices)
    best_omitted = [selected[index] for index in range(len(selected)) if index not in best_indices]
    best_dossier = render(best_indices, best_omitted)
    while low <= high:
        count = (low + high) // 2
        indices = set(required_indices)
        indices.update(optional_indices[:count])
        omitted = [item for index, item in enumerate(selected) if index not in indices]
        dossier = render(indices, omitted)
        if dossier.byte_size <= limit_bytes:
            best_indices = indices
            best_omitted = omitted
            best_dossier = dossier
            low = count + 1
        else:
            high = count - 1

    if best_dossier.byte_size > limit_bytes:
        best_indices = set()
        best_omitted = list(selected)
        note = (
            "TRONQUÉ explicitement par la limite dure de 2 Mo: "
            f"taille initiale={initial_bytes} octets; toutes les cartes sont représentées "
            "par un index de références borné et une empreinte de la liste complète."
        )
        best_dossier = _render_advisor_dossier(
            [],
            unavailable_observations=unavailable_observations,
            contradiction_requested=contradiction_requested,
            explicit_claims=explicit_claims,
            prompt_data=prompt_data,
            volume_note=note,
        )
    return (
        [item for index, item in enumerate(selected) if index in best_indices],
        best_dossier,
        len(best_omitted),
    )


def _advisor_candidate_aliases(selected: Sequence[Mapping[str, Any]]) -> list[str]:
    aliases: list[str] = []
    review_target_index = 0
    evidence_index = 0
    for item in selected:
        if item.get("origin") == "versioned_review_target":
            review_target_index += 1
            aliases.append(f"T{review_target_index}")
        else:
            evidence_index += 1
            aliases.append(f"E{evidence_index}")
    return aliases


def _compact_advisor_candidates(
    selected: Sequence[dict[str, Any]], *, only_large_observation_bodies: bool = False
) -> list[dict[str, Any]]:
    """Deduplicate repeated observation content, then cap each displayed body."""
    grouped: list[dict[str, Any]] = []
    observation_by_identity: dict[tuple[str, str], dict[str, Any]] = {}
    for original in selected:
        item = dict(original)
        item["extra"] = dict(item.get("extra", {}))
        item["_compact_eligible"] = not item.get("_compaction") and (
            not only_large_observation_bodies
            or (
                item.get("kind") in {"observation", "measurement"}
                and len(_as_text(item.get("body")).encode("utf-8"))
                > _ADVISOR_DOSSIER_EXCERPT_LIMIT_BYTES
            )
        )
        if not item["_compact_eligible"]:
            grouped.append(item)
            continue
        if item.get("kind") == "observation":
            key = (_as_text(item.get("id")), _as_text(item.get("sha256")))
            existing = observation_by_identity.get(key)
            if existing is not None:
                if item["body"] != existing["body"]:
                    existing["body"] = f"{existing['body']}\n\n{item['body']}"
                existing["score"] = max(existing["score"], item["score"])
                continue
            observation_by_identity[key] = item
        grouped.append(item)

    grouped = [
        item
        for _index, item in sorted(
            enumerate(grouped),
            key=lambda pair: (
                -pair[1]["score"],
                0 if pair[1].get("kind") == "observation" else 1,
                pair[0],
            ),
        )
    ]
    aliases = _advisor_candidate_aliases(grouped)
    payload_groups: dict[str, list[int]] = {}
    exact_body_groups: dict[str, list[int]] = {}
    for index, item in enumerate(grouped):
        if not item.get("_compact_eligible"):
            continue
        payload = _advisor_base64_payload(_as_text(item.get("body")))
        if payload is not None:
            payload_sha256 = hashlib.sha256(payload[1]).hexdigest()
            payload_groups.setdefault(payload_sha256, []).append(index)
        body_sha256 = hashlib.sha256(_as_text(item.get("body")).encode("utf-8")).hexdigest()
        exact_body_groups.setdefault(body_sha256, []).append(index)

    canonical_index_by_payload: dict[str, int] = {}
    for payload_sha256, indices in payload_groups.items():
        canonical_index_by_payload[payload_sha256] = indices[0]
    canonical_index_by_body = {
        body_sha256: indices[0] for body_sha256, indices in exact_body_groups.items()
    }

    for index, item in enumerate(grouped):
        if not item.get("_compact_eligible"):
            continue
        original_body = _as_text(item.get("body"))
        item["_original_body"] = original_body
        payload = _advisor_base64_payload(original_body)
        duplicate_payload_index: int | None = None
        if payload is not None:
            payload_sha256 = hashlib.sha256(payload[1]).hexdigest()
            canonical_index = canonical_index_by_payload[payload_sha256]
            if canonical_index != index:
                duplicate_payload_index = canonical_index
        body_sha256 = hashlib.sha256(original_body.encode("utf-8")).hexdigest()
        duplicate_body_index = canonical_index_by_body[body_sha256]
        if duplicate_body_index != index:
            item["body"], item["_compaction"] = _reference_advisor_body(
                item,
                alias=aliases[duplicate_body_index],
                reason=f"contenu identique à l'alias [{aliases[duplicate_body_index]}]",
            )
            continue
        if duplicate_payload_index is not None and payload is not None:
            item["body"], item["_compaction"] = _reuse_duplicate_payload_body(
                item,
                original_body,
                payload,
                alias=aliases[duplicate_payload_index],
                max_bytes=_ADVISOR_DOSSIER_EXCERPT_LIMIT_BYTES,
            )
            continue
        if len(original_body.encode("utf-8")) > _ADVISOR_DOSSIER_EXCERPT_LIMIT_BYTES:
            item["body"], item["_compaction"] = _compact_advisor_body(
                item,
                original_body,
                max_bytes=_ADVISOR_DOSSIER_EXCERPT_LIMIT_BYTES,
            )
    for item, alias in zip(grouped, aliases, strict=True):
        item.pop("_compact_eligible", None)
        item["_alias"] = alias
    return grouped


def _render_advisor_dossier(
    selected: Sequence[dict[str, Any]],
    *,
    unavailable_observations: set[str],
    contradiction_requested: bool,
    explicit_claims: set[str],
    prompt_data: Mapping[str, Any],
    volume_note: str = "",
) -> AdvisorDossier:
    manifest: list[dict[str, Any]] = []
    rendered: list[str] = []
    aliases = _advisor_candidate_aliases(selected)
    for item, alias in zip(selected, aliases, strict=True):
        alias = _as_text(item.get("_alias")) or alias
        piece = f"[{alias}] {item['label']}\n{_quote(item['body'])}"
        body_bytes = item["body"].encode("utf-8")
        piece_bytes = piece.encode("utf-8")
        row = {
            "alias": alias,
            "kind": item["kind"],
            "id": item["id"],
            "sha256": item["sha256"],
            "label": item["label"],
            "origin": item["origin"],
            **({"opinion_version": item["opinion_version"]} if item.get("opinion_version") else {}),
            **item.get("extra", {}),
            "body_sha256": hashlib.sha256(body_bytes).hexdigest(),
            "body_bytes": len(body_bytes),
            "rendered_sha256": hashlib.sha256(piece_bytes).hexdigest(),
            "rendered_bytes": len(piece_bytes),
        }
        if isinstance(item.get("_compaction"), Mapping):
            row.update(item["_compaction"])
        manifest.append(row)
        rendered.append(piece)

    lines = ["DOSSIER DE FAITS"]
    lines.extend(rendered or ["Aucun fait source pertinent n\u2019a été chargé pour cette tâche."])
    if volume_note:
        lines.append(volume_note)
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
