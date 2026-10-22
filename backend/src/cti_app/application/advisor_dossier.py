"""Build compact, readable adviser dossiers from verified task facts.

This module deliberately produces prose rather than a serialization of the
internal consultation object.  Its manifest is local provenance metadata and
must never be appended to the Bridge prompt.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

_WORD = re.compile(r"[\w.+#/-]+", re.UNICODE)
_CLAIM_ID = re.compile(r"\b(?:S\d+P\d+(?:\.\d+)?|L\d+(?:\.\d+)?|T\d+)\b", re.I)
_UUID = re.compile(r"(?i)\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b")
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


def extract_content_excerpts(payload: Mapping[str, Any]) -> list[dict[str, str]]:
    """Extract complete methods, decoded values, and sentence-bounded text."""
    excerpts: list[dict[str, str]] = []
    seen: set[tuple[str, str]] = set()
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
        text = value.replace("\r\n", "\n").replace("\r", "\n").strip()
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
            identity = (kind, chunk)
            if identity in seen:
                continue
            seen.add(identity)
            suffix = f" {index}" if len(chunks) > 1 else ""
            excerpts.append(
                {"kind": kind, "title": f"{prefix}{title.strip()}{suffix}", "text": chunk}
            )

    def display_name(row: Mapping[str, Any], fallback: str) -> str:
        for key in ("name", "method", "function", "symbol", "member", "key"):
            value = row.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return fallback

    def walk(value: Any, path: tuple[str, ...] = ()) -> None:
        if isinstance(value, Mapping):
            name = display_name(value, path[-1] if path else "contenu observé")
            for raw_key, child in value.items():
                key = str(raw_key).casefold().replace("-", "_")
                if key in _OMIT_KEYS or "sha256" in key or key.endswith("_id"):
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
                if key in {"hex", "hex_bytes", "bytes_hex", "hexdump"} and isinstance(child, str):
                    offset = next(
                        (
                            offset_value
                            for name, offset_value in value.items()
                            if str(name).casefold() in {"offset", "file_offset"}
                            and type(offset_value) is int
                        ),
                        None,
                    )
                    suffix = f", offset {offset}" if type(offset) is int else ""
                    add("hex", f"Octets observés{suffix}", child)
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
                        offset = next(
                            (
                                offset_value
                                for offset_key, offset_value in value.items()
                                if str(offset_key).casefold() in {"offset", "file_offset"}
                                and type(offset_value) is int
                            ),
                            None,
                        )
                        suffix = f", offset {offset}" if type(offset) is int else ""
                        add("hex", f"Octets observés{suffix}", child)
                    elif any(token in content_path for token in ("string", "decoded", "config")):
                        add("decoded", f"Valeur décodée — {name}", child)
                    else:
                        add("text", f"Texte observé — {name}", child)
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
    return excerpts


def build_advisor_dossier(prompt_data: Mapping[str, Any]) -> AdvisorDossier:
    """Select pertinent whole facts and render a shared French task dossier."""
    consultation_type = _as_text(prompt_data.get("consultation_type"))
    task = _as_text(prompt_data.get("question")) or _DEFAULT_TASKS.get(consultation_type, "")
    targets = _target_rows(prompt_data)
    target_text = "\n".join(_as_text(item.get("text")) for item in targets)
    relevance_text = f"{task}\n{target_text}"
    task_terms = _terms(relevance_text)
    explicit_claims = {item.casefold() for item in _CLAIM_ID.findall(relevance_text)}
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
        if score > 0 or ("finding", finding_id) in explicitly_referenced:
            candidates.append(
                _candidate(
                    kind="finding",
                    identifier=finding_id,
                    sha256=_as_text(item.get("sha256")),
                    label=f"Constat — {_as_text(item.get('kind')) or 'observation'}",
                    body=statement,
                    score=max(score, 1 if ("finding", finding_id) in explicitly_referenced else 0),
                    origin="finding_statement",
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
        body = f"{kind}, offset {offset}: {value}" if type(offset) is int else f"{kind}: {value}"
        score = _relevance_score(task_terms, body)
        if _numeric_targets_conflict(relevance_text, body):
            score = 0
        if score > 0 or ("observation", evidence_id) in explicitly_referenced:
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
                        "evidence_sha256": _as_text(item.get("evidence_sha256")),
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
        if score > 0 or ("observation", evidence_id) in explicitly_referenced:
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
                    },
                )
            )

    if not candidates:
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

    # Keep a stable alias for each displayed fact. The mapping stays local and
    # carries only the data needed by output validation and provenance.
    manifest: list[dict[str, Any]] = []
    rendered: list[str] = []
    for index, item in enumerate(selected, start=1):
        alias = f"E{index}"
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
            }
        )
        rendered.append(f"[{alias}] {item['label']}\n{_quote(item['body'])}")

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


__all__ = ["AdvisorDossier", "build_advisor_dossier", "extract_content_excerpts"]
