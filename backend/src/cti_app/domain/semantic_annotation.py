"""Versioned, renderer-independent semantic text annotations."""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType

SEMANTIC_ANNOTATION_SCHEMA_VERSION = "1"
SEMANTIC_ANNOTATION_POLICY_VERSION = "semantic-annotation-policy-v2-document-lexicon"


class SemanticRole(StrEnum):
    TEXT = "text"
    ACTOR = "actor"
    CAMPAIGN = "campaign"
    MALWARE = "malware"
    TOOL = "tool"
    PRODUCT = "product"
    ENGLISH_TERM = "english_term"
    TECHNICAL = "technical"
    TECHNICAL_LITERAL = "technical_literal"
    IOC = "ioc"
    PATH = "path"
    COMMAND = "command"
    PROTOCOL_FIELD = "protocol_field"
    SOURCE = "source"
    PROOF = "proof"


# Higher values win when candidate ranges overlap. Ties use longer range,
# earlier start, then the role's lexical value for deterministic resolution.
SEMANTIC_ROLE_PRIORITY = MappingProxyType(
    {
        SemanticRole.TEXT: 0,
        SemanticRole.ENGLISH_TERM: 10,
        SemanticRole.TECHNICAL: 20,
        SemanticRole.TECHNICAL_LITERAL: 40,
        SemanticRole.ACTOR: 30,
        SemanticRole.CAMPAIGN: 30,
        SemanticRole.MALWARE: 30,
        SemanticRole.TOOL: 30,
        SemanticRole.PRODUCT: 30,
        SemanticRole.IOC: 40,
        SemanticRole.PATH: 40,
        SemanticRole.COMMAND: 40,
        SemanticRole.PROTOCOL_FIELD: 40,
        SemanticRole.SOURCE: 50,
        SemanticRole.PROOF: 50,
    }
)


# This mapping is part of the annotation policy. The functions are defined in
# chpTypst/UTILS/helpers.typ; model output can select only typed roles above.
SEMANTIC_ROLE_TYPST_FUNCTION_V1 = MappingProxyType(
    {
        SemanticRole.TEXT: "semantic-plain",
        SemanticRole.ACTOR: "semantic-actor",
        SemanticRole.CAMPAIGN: "semantic-campaign",
        SemanticRole.MALWARE: "semantic-malware",
        SemanticRole.TOOL: "semantic-tool",
        SemanticRole.PRODUCT: "semantic-product",
        SemanticRole.ENGLISH_TERM: "semantic-english-term",
        SemanticRole.TECHNICAL: "semantic-technical",
        SemanticRole.TECHNICAL_LITERAL: "semantic-technical-literal",
        SemanticRole.IOC: "semantic-ioc",
        SemanticRole.PATH: "semantic-path",
        SemanticRole.COMMAND: "semantic-command",
        SemanticRole.PROTOCOL_FIELD: "semantic-protocol-field",
        SemanticRole.SOURCE: "semantic-source",
        SemanticRole.PROOF: "semantic-proof",
    }
)

_ANCHOR = re.compile(r"^[a-z][a-z0-9:_-]{0,127}$")


def lead_paragraph_anchor(index: int) -> str:
    return f"lead:{index:04d}"


def section_paragraph_anchor(section_index: int, paragraph_index: int) -> str:
    return f"section:{section_index}:paragraph:{paragraph_index:04d}"


def timeline_anchor(index: int) -> str:
    return f"timeline:{index:04d}"


@dataclass(frozen=True, slots=True)
class SemanticAnnotationProposalV1:
    """An exact text proposal anchored to one stable paragraph identity."""

    paragraph_anchor: str
    role: SemanticRole
    text: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.paragraph_anchor, str)
            or _ANCHOR.fullmatch(self.paragraph_anchor) is None
        ):
            raise ValueError("Semantic annotation paragraph anchor is invalid")
        if not isinstance(self.role, SemanticRole) or self.role is SemanticRole.TEXT:
            raise ValueError("Semantic annotation role is invalid")
        if not isinstance(self.text, str) or not self.text:
            raise ValueError("Semantic annotation text must be non-empty")


@dataclass(frozen=True, slots=True)
class SemanticTextSpanV1:
    role: SemanticRole
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.role, SemanticRole):
            raise ValueError("Semantic text span role is invalid")
        if not isinstance(self.text, str) or (not self.text and self.role is not SemanticRole.TEXT):
            raise ValueError("Only unstyled semantic text spans may be empty")


@dataclass(frozen=True, slots=True)
class SemanticParagraphV1:
    anchor: str
    spans: tuple[SemanticTextSpanV1, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.anchor, str) or _ANCHOR.fullmatch(self.anchor) is None:
            raise ValueError("Semantic paragraph anchor is invalid")
        if not isinstance(self.spans, tuple) or not self.spans:
            raise ValueError("Semantic paragraph spans must be a non-empty tuple")
        if any(not isinstance(span, SemanticTextSpanV1) for span in self.spans):
            raise ValueError("Semantic paragraph contains an invalid span")

    @property
    def text(self) -> str:
        return "".join(span.text for span in self.spans)


@dataclass(frozen=True, slots=True)
class SemanticTextV1:
    schema_version: str
    policy_version: str
    paragraphs: tuple[SemanticParagraphV1, ...]

    def __post_init__(self) -> None:
        if self.schema_version != SEMANTIC_ANNOTATION_SCHEMA_VERSION:
            raise ValueError("Semantic text schema version is unsupported")
        if self.policy_version != SEMANTIC_ANNOTATION_POLICY_VERSION:
            raise ValueError("Semantic text policy version is unsupported")
        if not isinstance(self.paragraphs, tuple) or any(
            not isinstance(paragraph, SemanticParagraphV1) for paragraph in self.paragraphs
        ):
            raise ValueError("Semantic text paragraphs have an invalid type")
        anchors = tuple(paragraph.anchor for paragraph in self.paragraphs)
        if len(set(anchors)) != len(anchors):
            raise ValueError("Semantic text paragraph anchors must be unique")


def semantic_annotation_proposal_to_json(
    proposal: SemanticAnnotationProposalV1,
) -> dict[str, str]:
    return {
        "paragraph_anchor": proposal.paragraph_anchor,
        "role": proposal.role.value,
        "text": proposal.text,
    }


def semantic_annotation_proposal_from_json(raw: object) -> SemanticAnnotationProposalV1:
    if not isinstance(raw, dict) or set(raw) != {"paragraph_anchor", "role", "text"}:
        raise ValueError("Semantic annotation proposal has missing or extra fields")
    if any(not isinstance(raw[key], str) for key in ("paragraph_anchor", "role", "text")):
        raise ValueError("Semantic annotation proposal fields must be text")
    try:
        role = SemanticRole(raw["role"])
    except ValueError as exc:
        raise ValueError("Semantic annotation role is unknown") from exc
    return SemanticAnnotationProposalV1(raw["paragraph_anchor"], role, raw["text"])


def semantic_text_to_json(value: SemanticTextV1) -> dict[str, object]:
    return {
        "schema_version": value.schema_version,
        "policy_version": value.policy_version,
        "paragraphs": [
            {
                "anchor": paragraph.anchor,
                "spans": [{"role": span.role.value, "text": span.text} for span in paragraph.spans],
            }
            for paragraph in value.paragraphs
        ],
    }


def semantic_text_from_json(raw: object) -> SemanticTextV1:
    if not isinstance(raw, dict) or set(raw) != {"schema_version", "policy_version", "paragraphs"}:
        raise ValueError("Semantic text has missing or extra fields")
    version = raw["schema_version"]
    policy = raw["policy_version"]
    paragraphs_raw = raw["paragraphs"]
    if not isinstance(version, str) or not isinstance(policy, str):
        raise ValueError("Semantic text versions must be text")
    if not isinstance(paragraphs_raw, list):
        raise ValueError("Semantic text paragraphs must be an array")
    paragraphs: list[SemanticParagraphV1] = []
    for paragraph_raw in paragraphs_raw:
        if not isinstance(paragraph_raw, dict) or set(paragraph_raw) != {"anchor", "spans"}:
            raise ValueError("Semantic paragraph has missing or extra fields")
        anchor = paragraph_raw["anchor"]
        spans_raw = paragraph_raw["spans"]
        if not isinstance(anchor, str) or not isinstance(spans_raw, list):
            raise ValueError("Semantic paragraph fields have invalid types")
        spans: list[SemanticTextSpanV1] = []
        for span_raw in spans_raw:
            if not isinstance(span_raw, dict) or set(span_raw) != {"role", "text"}:
                raise ValueError("Semantic text span has missing or extra fields")
            role_raw = span_raw["role"]
            text = span_raw["text"]
            if not isinstance(role_raw, str) or not isinstance(text, str):
                raise ValueError("Semantic text span fields must be text")
            try:
                role = SemanticRole(role_raw)
            except ValueError as exc:
                raise ValueError("Semantic text span role is unknown") from exc
            spans.append(SemanticTextSpanV1(role, text))
        paragraphs.append(SemanticParagraphV1(anchor, tuple(spans)))
    return SemanticTextV1(version, policy, tuple(paragraphs))
