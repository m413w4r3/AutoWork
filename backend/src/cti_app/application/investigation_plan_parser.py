"""Tolerant text parser for the canonical ``investigation-plan.v1`` content.

Bridge answers in free text: Markdown, bold labels, numbered or headed leads,
sometimes a fenced JSON object, often a mix of French and English, and rarely
every one of the twelve lead fields.  This module turns that material into
:class:`InvestigationLeadV1` values with a per-field partial result; it never
invents fields and never promotes silence into a plan.

Only an answer that yields at least one identifiable lead can become a plan.
Per ADR-0011 D2, ``unparsed`` stays advisory: :meth:`to_plan` returns ``None``
so the caller records no canonical version.  ``parsed`` means every lead has
the eleven mandatory fields; ``partially_parsed`` means at least one field of
at least one lead is missing.  ``depends_on`` is optional by contract.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import UUID

from cti_app.domain.investigation import canonical_json_bytes
from cti_app.domain.investigation_plan import InvestigationLeadV1, InvestigationPlanV1

LEAD_FIELDS: tuple[str, ...] = (
    "title",
    "objective",
    "cti_justification",
    "claim_refs",
    "start_evidence_refs",
    "sample_refs",
    "tools",
    "preconditions",
    "interest",
    "conclusion_criteria",
    "limits",
    "depends_on",
)
_MANDATORY_FIELDS = tuple(name for name in LEAD_FIELDS if name != "depends_on")
_TEXT_FIELDS = frozenset(
    {
        "title",
        "objective",
        "cti_justification",
        "interest",
        "conclusion_criteria",
        "limits",
    }
)
_REF_FIELDS = frozenset(
    {
        "claim_refs",
        "start_evidence_refs",
        "sample_refs",
        "tools",
        "preconditions",
        "depends_on",
    }
)
_ADVISORY_PREFIX = "[UNTRUSTED ADVISORY]"

_FIELD_ALIASES: Mapping[str, tuple[str, ...]] = {
    "title": ("title", "titre", "intitulé", "intitule", "heading"),
    "objective": ("objective", "objectif", "but", "goal", "aim"),
    "cti_justification": (
        "cti justification",
        "justification cti",
        "justification",
        "rationale",
        "cti rationale",
        "why",
        "pourquoi",
    ),
    "claim_refs": (
        "claim refs",
        "claim ref",
        "claims",
        "claim",
        "références de claims",
        "references de claims",
        "reference de claims",
        "référence de claims",
        "affirmations",
        "claim references",
    ),
    "start_evidence_refs": (
        "start evidence refs",
        "start evidence references",
        "start refs",
        "starting refs",
        "starting evidence",
        "preuves de départ",
        "preuves de depart",
        "observations de départ",
        "observations de depart",
        "références de preuve de départ",
        "references de preuve de depart",
        "evidence refs",
        "preuves initiales",
    ),
    "sample_refs": (
        "sample refs",
        "samples",
        "sample hashes",
        "échantillons",
        "echantillons",
        "hashes",
        "hash",
    ),
    "tools": ("tools", "outils", "tooling", "outils prévus", "outils prevus"),
    "preconditions": (
        "preconditions",
        "préconditions",
        "conditions préalables",
        "conditions prealables",
        "prerequis",
        "prérequis",
    ),
    "interest": (
        "interest",
        "intérêt",
        "interet",
        "priority",
        "priorité",
        "priorite",
        "value",
        "valeur",
    ),
    "conclusion_criteria": (
        "conclusion criteria",
        "critères de conclusion",
        "criteres de conclusion",
        "critère de conclusion",
        "critere de conclusion",
        "success criteria",
        "critères de succès",
        "criteres de succes",
        "critères de réussite",
        "criteres de reussite",
        "done when",
    ),
    "limits": (
        "limits",
        "limites",
        "limitations",
        "risks",
        "risques",
        "limites et risques",
        "constraints",
        "contraintes",
    ),
    "depends_on": (
        "depends on",
        "dependencies",
        "dependances",
        "dépendances",
        "dépend de",
        "depend de",
        "after",
        "après",
        "apres",
        "prerequisites",
    ),
}
_LEAD_ID_ALIASES = frozenset({"lead id", "leadid", "id", "identifiant", "piste id"})

_DOCUMENT_TITLES = frozenset(
    {
        "analyse",
        "analysis",
        "answer",
        "contexte",
        "context",
        "introduction",
        "investigation plan",
        "leads",
        "notes",
        "pistes",
        "plan",
        "plan d'investigation",
        "plan d investigation",
        "plan proposé",
        "plan propose",
        "priorités",
        "priorites",
        "proposal",
        "proposition",
        "proposed plan",
        "réponse",
        "reponse",
        "remarks",
        "résumé",
        "resume",
        "summary",
        "synthèse",
        "synthese",
    }
)

_CLAIM_RE = re.compile(r"(?i)\b(?:S\d+P\d+(?:\.\d+)?|L\d+(?:\.\d+)?|T\d+)\b")
_UUID_RE = re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b", re.I)
_HASH_RE = re.compile(r"\b(?:[0-9a-f]{32}|[0-9a-f]{40}|[0-9a-f]{64})\b", re.I)

# French/English plan labels use typographic separators (en dash, em dash,
# fullwidth colon). Named escapes keep this source ASCII while the compiled
# patterns still match them.
_EN_DASH = "\N{EN DASH}"
_EM_DASH = "\N{EM DASH}"
_FULLWIDTH_COLON = "\N{FULLWIDTH COLON}"
_DASH_CHARS = _EN_DASH + _EM_DASH
_FIELD_SEPARATOR_CHARS = ":" + _FULLWIDTH_COLON + "="
_FIELD_LINE_RE = re.compile(
    r"^(?P<label>.{1,80}?)\s*(?:["
    + _FIELD_SEPARATOR_CHARS
    + r"]|\s["
    + _DASH_CHARS
    + r"]\s)\s*(?P<value>.*)$"
)
_INLINE_FIELD_SPLIT_RE = re.compile(
    r"\s+[|;·]\s+(?=[^|;·]{2,60}?\s*(?:["
    + _FIELD_SEPARATOR_CHARS
    + r"]|\s["
    + _DASH_CHARS
    + r"]\s))"
)


_LEAD_MARKER_RE = re.compile(
    r"(?i)^(?:lead|piste|axe|étape|etape|step|task|tâche|tache)\s*[-_:#]?\s*"
    r"(?P<marker>[0-9]{1,2}[a-z]?|[a-z])\b\s*(?:[-" + _DASH_CHARS + r":.)]\s*)?(?P<title>.*)$"
)
_NUMBERED_HEADING_RE = re.compile(r"^(?P<number>\d{1,2})[.)]\s+(?P<title>.+)$")
_MARKDOWN_HEADING_RE = re.compile(r"^\s{0,3}(?P<hashes>#{1,6})\s+(?P<content>.*?)\s*#*\s*$")
_BOLD_LINE_RE = re.compile(r"^\s*(?:\*\*|__)(?P<content>.+?)(?:\*\*|__)\s*$")
_BULLET_RE = re.compile(r"^\s*(?:[-*+•]|\(?\d{1,2}[.)])\s+")
_MARKDOWN_CHARS = re.compile(r"[*_`]")


class PlanTextParseStatus(StrEnum):
    PARSED = "parsed"
    PARTIALLY_PARSED = "partially_parsed"
    UNPARSED = "unparsed"


@dataclass(frozen=True, slots=True, kw_only=True)
class ParsedLeadStatusV1:
    """Per-lead field coverage, so a partial plan is auditable field by field."""

    lead_id: str | None
    title: str
    present_fields: tuple[str, ...]
    missing_fields: tuple[str, ...]


@dataclass(frozen=True, slots=True, kw_only=True)
class InvestigationPlanParseResultV1:
    parse_status: PlanTextParseStatus
    leads: tuple[InvestigationLeadV1, ...]
    lead_statuses: tuple[ParsedLeadStatusV1, ...]
    reason: str | None = None

    def to_plan(self, *, investigation_id: UUID, version: int) -> InvestigationPlanV1 | None:
        """Build canonical content, or ``None`` when the answer stayed advice."""
        if self.parse_status is PlanTextParseStatus.UNPARSED or not self.leads:
            return None
        return InvestigationPlanV1(
            investigation_id=investigation_id,
            version=version,
            leads=self.leads,
        )


def _label_key(value: str) -> str:
    normalized = _MARKDOWN_CHARS.sub("", value)
    normalized = re.sub(r"[\s_\-]+", " ", normalized.casefold()).strip()
    return normalized.strip(" .:" + _FULLWIDTH_COLON + _DASH_CHARS)


_FIELD_BY_ALIAS: dict[str, str] = {
    alias: field_name
    for field_name, aliases in _FIELD_ALIASES.items()
    for alias in (_label_key(alias) for alias in (field_name, *aliases))
}


def _clean_text(value: str) -> str:
    normalized = re.sub(r"^[\s\-" + _DASH_CHARS + r":•]+", "", value.strip())
    normalized = re.sub(r"^(?:\*\*|__|`|\*|_)+", "", normalized)
    normalized = re.sub(r"(?:\*\*|__|`|\*|_)+$", "", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return normalized.strip("\"'")


def _clean_ref_text(value: str) -> str:
    """Strip bullets and surrounding emphasis without altering identifiers."""
    normalized = re.sub(r"^[\s\-" + _DASH_CHARS + r":•]+", "", value.strip())
    normalized = re.sub(r"^(?:\*\*|__|`)+|(?:\*\*|__|`)+$", "", normalized)
    return normalized.strip().strip("\"'")


def _match_field_line(line: str) -> tuple[str, str] | None:
    stripped = line.strip()
    if stripped.startswith("#"):
        stripped = re.sub(r"^#{1,6}\s+", "", stripped)
    stripped = _BULLET_RE.sub("", stripped, count=1)
    if not stripped:
        return None
    match = _FIELD_LINE_RE.match(stripped)
    if match is not None:
        field_name = _FIELD_BY_ALIAS.get(_label_key(match.group("label")))
        if field_name is not None:
            return field_name, match.group("value").strip()
    key = _label_key(stripped)
    field_name = _FIELD_BY_ALIAS.get(key)
    if field_name is not None and len(stripped) <= 80:
        return field_name, ""
    return None


def _split_inline_fields(value: str) -> list[tuple[str | None, str]]:
    if not re.search(r"\s[|;·]\s", value):
        return [(None, value)]
    parts = _INLINE_FIELD_SPLIT_RE.split(value)
    segments: list[tuple[str | None, str]] = []
    for part in parts:
        matched = _match_field_line(part)
        if matched is not None:
            segments.append(matched)
        else:
            segments.append((None, part))
    return segments


def _split_ref_values(field_name: str, value: str) -> list[str]:
    cleaned = _clean_ref_text(value)
    if not cleaned:
        return []
    if field_name == "claim_refs":
        claims = _CLAIM_RE.findall(cleaned)
        if claims:
            return list(dict.fromkeys(claims))
    if field_name in {"start_evidence_refs", "sample_refs"}:
        references = _UUID_RE.findall(cleaned) + _HASH_RE.findall(cleaned)
        if references:
            return list(dict.fromkeys(references))
    if field_name == "tools":
        parts = [token for part in re.split(r"[,;·]", cleaned) if (token := _clean_ref_text(part))]
        if len(parts) == 1:
            tokens = parts[0].split()
            if len(tokens) > 1 and all(
                re.fullmatch(r"[A-Za-z][\w.-]*[._][\w.-]*", token) for token in tokens
            ):
                return list(dict.fromkeys(tokens))
        return list(dict.fromkeys(parts))
    if field_name == "depends_on":
        parts = [part.strip() for part in re.split(r"[,;·]", cleaned) if part.strip()]
        return list(dict.fromkeys(parts))
    parts = [part.strip() for part in re.split(r"[,;·]", cleaned) if part.strip()]
    return list(dict.fromkeys(parts))


@dataclass(slots=True)
class _MutableLead:
    ordinal: int
    lead_id: str | None
    title: str
    values: dict[str, list[str]] = field(default_factory=dict)

    def add(self, field_name: str, value: str) -> None:
        if field_name == "title":
            cleaned = _clean_text(value)
            if cleaned and not self.title:
                self.title = cleaned
            return
        if field_name in _REF_FIELDS:
            self.values.setdefault(field_name, []).extend(_split_ref_values(field_name, value))
            return
        cleaned = _clean_text(value)
        if cleaned:
            self.values.setdefault(field_name, []).append(cleaned)


def _strip_advisory_prefix(text: str) -> str:
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = normalized.split("\n")
    while lines and (
        not lines[0].strip() or lines[0].strip().casefold() == _ADVISORY_PREFIX.casefold()
    ):
        lines.pop(0)
    return "\n".join(lines)


def _title_from_heading(text: str) -> str:
    return _clean_text(text).strip(" :-" + _DASH_CHARS)


def _lead_heading(text: str) -> tuple[str | None, str] | None:
    cleaned = _title_from_heading(text)
    if not cleaned:
        return None
    marker = _LEAD_MARKER_RE.match(cleaned)
    if marker is not None:
        lead_id = f"lead-{marker.group('marker').casefold()}"
        title = marker.group("title").strip(" :-" + _DASH_CHARS)
        return lead_id, title or cleaned
    numbered = _NUMBERED_HEADING_RE.match(cleaned)
    if numbered is not None:
        title = numbered.group("title").strip()
        words = [word for word in re.split(r"\s+", title) if word]
        if len(words) >= 2 or "*" in text or "_" in text:
            return f"lead-{numbered.group('number')}", title
    return None


def _match_lead_heading(
    line: str, *, has_current: bool, previous_blank: bool
) -> tuple[str | None, str, bool] | None:
    """Return ``(lead_id, title, explicit)`` for a heading-like line.

    ``explicit`` is true for a declared lead marker ("Lead 1 — …") or a
    numbered heading; a plain heading is only a title candidate the caller may
    attach to the next field block (or start immediately when a lead is open).
    """
    markdown = _MARKDOWN_HEADING_RE.match(line)
    if markdown is not None:
        content = markdown.group("content")
        if _match_field_line(content) is not None:
            return None
        matched = _lead_heading(content)
        if matched is not None:
            return matched[0], matched[1], True
        key = _label_key(content)
        if key in _DOCUMENT_TITLES:
            return None
        title = _title_from_heading(content)
        if title:
            return None, title, has_current
        return None
    bold = _BOLD_LINE_RE.match(line)
    if bold is not None:
        content = bold.group("content")
        if _match_field_line(content) is not None:
            return None
        matched = _lead_heading(content)
        if matched is not None:
            return matched[0], matched[1], True
        key = _label_key(content)
        title = _title_from_heading(content)
        if title and key not in _DOCUMENT_TITLES and len(title) <= 160:
            return None, title, has_current
        return None
    if _leads_marker_line(line):
        matched = _lead_heading(line)
        if matched is not None:
            return matched[0], matched[1], True
        return None
    if previous_blank and _NUMBERED_HEADING_RE.match(_clean_text(line)):
        matched = _lead_heading(_clean_text(line))
        if matched is not None:
            return matched[0], matched[1], True
    return None


def _leads_marker_line(line: str) -> bool:
    stripped = _BULLET_RE.sub("", line.strip(), count=1)
    return _LEAD_MARKER_RE.match(_title_from_heading(stripped)) is not None


def _json_candidates(text: str) -> list[Any]:
    candidates: list[Any] = []
    for block in re.findall(r"```(?:json)?\s*\n?(.*?)```", text, re.S | re.I):
        parsed = _decode_json(block)
        if parsed is not None:
            candidates.append(parsed)
    decoder = json.JSONDecoder()
    for opener in ("{", "["):
        start = text.find(opener)
        while start != -1:
            try:
                parsed, _end = decoder.raw_decode(text[start:])
            except json.JSONDecodeError:
                start = text.find(opener, start + 1)
                continue
            candidates.append(parsed)
            break
    return candidates


def _decode_json(candidate: str) -> Any:
    stripped = candidate.strip()
    if not stripped:
        return None
    for attempt in (stripped, re.sub(r",\s*([}\]])", r"\1", stripped)):
        try:
            return json.loads(attempt)
        except json.JSONDecodeError:
            continue
    return None


def _json_lead_rows(value: Any) -> list[Mapping[str, Any]] | None:
    if isinstance(value, Mapping):
        for key in ("leads", "pistes", "lead"):
            if key in value and isinstance(value[key], (list, tuple)):
                return [item for item in value[key] if isinstance(item, Mapping)]
        for key in ("plan", "investigation_plan", "proposal", "proposition"):
            if key in value:
                return _json_lead_rows(value[key])
        if any(
            _label_key(str(candidate)) in _FIELD_BY_ALIAS
            or _label_key(str(candidate)) in _LEAD_ID_ALIASES
            for candidate in value
        ):
            return [value]
        return None
    if isinstance(value, (list, tuple)):
        rows = [item for item in value if isinstance(item, Mapping)]
        return rows or None
    return None


def _json_scalar(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, (int, float)):
        return str(value)
    return ""


def _lead_from_json(row: Mapping[str, Any]) -> _MutableLead | None:
    values: dict[str, list[str]] = {}
    lead_id: str | None = None
    title = ""
    for raw_key, raw_value in row.items():
        key = _label_key(str(raw_key))
        if key in _LEAD_ID_ALIASES:
            lead_id = _clean_text(_json_scalar(raw_value)) or None
            continue
        field_name = _FIELD_BY_ALIAS.get(key)
        if field_name is None:
            continue
        if isinstance(raw_value, (list, tuple)):
            entries = [_json_scalar(item) for item in raw_value]
            entries = [entry for entry in entries if entry.strip()]
        else:
            entries = [_json_scalar(raw_value)]
        for entry in entries:
            if not entry.strip():
                continue
            if field_name == "title":
                title = _clean_text(entry) or title
            elif field_name in _REF_FIELDS:
                values.setdefault(field_name, []).extend(_split_ref_values(field_name, entry))
            else:
                cleaned = _clean_text(entry)
                if cleaned:
                    values.setdefault(field_name, []).append(cleaned)
    if not title:
        return None
    return _MutableLead(ordinal=0, lead_id=lead_id, title=title, values=values)


def _mutables_from_json(text: str) -> list[_MutableLead]:
    for candidate in _json_candidates(text):
        rows = _json_lead_rows(candidate)
        if not rows:
            continue
        leads: list[_MutableLead] = []
        for row in rows:
            lead = _lead_from_json(row)
            if lead is not None:
                lead.ordinal = len(leads) + 1
                leads.append(lead)
        if leads:
            return leads
    return []


def _mutables_from_text(text: str) -> list[_MutableLead]:
    leads: list[_MutableLead] = []
    current: _MutableLead | None = None
    current_field: str | None = None
    pending_title: str | None = None
    previous_blank = True
    for raw_line in text.split("\n"):
        line = raw_line.rstrip()
        if not line.strip():
            previous_blank = True
            continue
        matched_field = _match_field_line(line)
        if matched_field is not None:
            field_name, value = matched_field
            if current is None:
                current = _MutableLead(
                    ordinal=len(leads) + 1,
                    lead_id=None,
                    title=pending_title or "",
                )
                pending_title = None
                leads.append(current)
            active = field_name
            for index, (inline_field, inline_value) in enumerate(_split_inline_fields(value)):
                if index == 0 and inline_field is None:
                    current.add(active, inline_value)
                    continue
                target = inline_field or active
                active = target
                current.add(target, inline_value)
            current_field = active
            previous_blank = False
            continue
        heading = _match_lead_heading(
            line, has_current=current is not None, previous_blank=previous_blank
        )
        if heading is not None:
            lead_id, title, explicit = heading
            if explicit or current is not None:
                current = _MutableLead(
                    ordinal=len(leads) + 1,
                    lead_id=_lead_id_from_text(lead_id),
                    title=title,
                )
                leads.append(current)
                pending_title = None
                current_field = None
            else:
                pending_title = title
                current_field = None
            previous_blank = False
            continue
        if current is None:
            previous_blank = False
            continue
        if current_field is not None:
            current.add(current_field, line)
        previous_blank = False
    return leads


def _lead_id_from_text(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = _clean_ref_text(value)
    return cleaned or None


def normalize_dependency_reference(token: str) -> str:
    cleaned = re.sub(r"\s*\([^)]*\)\s*$", "", _clean_ref_text(token)).strip()
    marker = _LEAD_MARKER_RE.match(cleaned)
    if marker is not None and not marker.group("title").strip(" :-" + _DASH_CHARS):
        return f"lead-{marker.group('marker').casefold()}"
    if re.fullmatch(r"\d{1,2}", cleaned):
        return f"lead-{cleaned}"
    return cleaned


def _resolve_dependencies(
    leads: Sequence[_MutableLead],
) -> tuple[list[list[str]], bool]:
    by_lead_id: dict[str, _MutableLead] = {}
    by_title: dict[str, list[_MutableLead]] = {}
    for lead in leads:
        if lead.lead_id:
            by_lead_id.setdefault(lead.lead_id.casefold(), lead)
        title_key = _label_key(lead.title)
        if title_key:
            by_title.setdefault(title_key, []).append(lead)
    resolved: list[list[str]] = []
    dropped_self_dependency = False
    for lead in leads:
        tokens: list[str] = []
        for raw in lead.values.get("depends_on", ()):
            token = normalize_dependency_reference(raw)
            target = by_lead_id.get(token.casefold())
            if target is None:
                candidates = by_title.get(_label_key(token), [])
                target = candidates[0] if len(candidates) == 1 else None
            if target is lead:
                fallback = lead.title if lead.title and lead.title != lead.lead_id else None
                if fallback is not None and fallback not in tokens:
                    tokens.append(fallback)
                else:
                    dropped_self_dependency = True
                continue
            if target is not None:
                value = target.lead_id or target.title
                if value and value not in tokens:
                    tokens.append(value)
                continue
            if raw not in tokens:
                tokens.append(raw)
        resolved.append(tokens)
    return resolved, dropped_self_dependency


def _lead_status(lead: InvestigationLeadV1, present: frozenset[str]) -> ParsedLeadStatusV1:
    missing = tuple(name for name in _MANDATORY_FIELDS if name not in present)
    return ParsedLeadStatusV1(
        lead_id=lead.lead_id,
        title=lead.title,
        present_fields=tuple(name for name in LEAD_FIELDS if name in present),
        missing_fields=missing,
    )


def _finalize(
    mutables: Sequence[_MutableLead],
) -> tuple[tuple[InvestigationLeadV1, ...], tuple[ParsedLeadStatusV1, ...], str | None]:
    structural_issue = False
    prepared: list[tuple[_MutableLead, dict[str, Any]]] = []
    seen_lead_ids: set[str] = set()
    for mutable in mutables:
        title = _clean_text(mutable.title)
        lead_id = mutable.lead_id
        if lead_id is not None:
            if lead_id.casefold() in seen_lead_ids:
                lead_id = None
                structural_issue = True
            else:
                seen_lead_ids.add(lead_id.casefold())
        if not title:
            title = lead_id or ""
        if not title:
            structural_issue = True
            continue
        values: dict[str, Any] = {"title": title, "lead_id": lead_id}
        present: set[str] = {"title"}
        for field_name in LEAD_FIELDS:
            if field_name == "title":
                continue
            raw_values = mutable.values.get(field_name, [])
            if not raw_values:
                continue
            if field_name in _REF_FIELDS:
                deduped = tuple(dict.fromkeys(item for item in raw_values if item.strip()))
                values[field_name] = deduped
                if deduped:
                    present.add(field_name)
            else:
                joined = " ".join(item for item in raw_values if item.strip()).strip()
                values[field_name] = joined
                if joined:
                    present.add(field_name)
        prepared.append((mutable, {**values, "_present": frozenset(present)}))

    dependencies, dropped_self = _resolve_dependencies([item[0] for item in prepared])
    structural_issue = structural_issue or dropped_self
    leads: list[InvestigationLeadV1] = []
    statuses: list[ParsedLeadStatusV1] = []
    identities: set[tuple[Any, ...]] = set()
    for (mutable, values), depends_on in zip(prepared, dependencies, strict=True):
        del mutable
        values["depends_on"] = tuple(depends_on)
        present_fields = frozenset(values["_present"])
        if depends_on:
            present_fields = present_fields | {"depends_on"}
        try:
            lead = InvestigationLeadV1(
                title=values["title"],
                objective=values.get("objective", ""),
                cti_justification=values.get("cti_justification", ""),
                claim_refs=values.get("claim_refs", ()),
                start_evidence_refs=values.get("start_evidence_refs", ()),
                sample_refs=values.get("sample_refs", ()),
                tools=values.get("tools", ()),
                preconditions=values.get("preconditions", ()),
                interest=values.get("interest", ""),
                conclusion_criteria=values.get("conclusion_criteria", ""),
                limits=values.get("limits", ""),
                depends_on=values.get("depends_on", ()),
                lead_id=values.get("lead_id"),
            )
        except (TypeError, ValueError):
            structural_issue = True
            continue
        if lead.identity_key in identities:
            structural_issue = True
            continue
        identities.add(lead.identity_key)
        leads.append(lead)
        statuses.append(_lead_status(lead, present_fields))

    if structural_issue:
        reason = "some leads were deduplicated or could not be built"
    else:
        reason = None
    return tuple(leads), tuple(statuses), reason


def parse_investigation_plan_text(text: str) -> InvestigationPlanParseResultV1:
    """Parse Bridge's free-form answer; never raises on unstructured prose."""
    if not isinstance(text, str):
        raise TypeError("plan text must be a string")
    normalized = _strip_advisory_prefix(text)
    mutables = _mutables_from_json(normalized)
    if not mutables:
        mutables = _mutables_from_text(normalized)
    leads, statuses, reason = _finalize(mutables)
    if not leads:
        return InvestigationPlanParseResultV1(
            parse_status=PlanTextParseStatus.UNPARSED,
            leads=(),
            lead_statuses=(),
            reason="no identifiable lead",
        )
    complete = all(not status.missing_fields for status in statuses)
    if complete and reason is None:
        parse_status = PlanTextParseStatus.PARSED
    else:
        parse_status = PlanTextParseStatus.PARTIALLY_PARSED
    return InvestigationPlanParseResultV1(
        parse_status=parse_status,
        leads=leads,
        lead_statuses=statuses,
        reason=reason,
    )


def plan_lead_identity_bytes(lead: InvestigationLeadV1) -> bytes:
    """The E10 ``lead_identity`` bytes used by the deterministic task key."""
    identity = lead.identity_key
    if identity[0] == "lead_id":
        return str(identity[1]).encode("utf-8")
    _kind, title, start_refs, sample_refs = identity
    return canonical_json_bytes(
        {
            "start_evidence_refs": list(sorted(start_refs)),
            "sample_refs": list(sorted(sample_refs)),
            "title": title,
        }
    )
