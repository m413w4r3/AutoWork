"""Strict, non-executable P10 invariant proposal contracts."""

from __future__ import annotations

import hashlib
import json
from enum import StrEnum
from typing import Any, ClassVar
from uuid import UUID

from pydantic import (
    AliasChoices,
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from cti_app.domain.invariants import InvariantCategory, InvariantType

_SHA256_PATTERN = r"^[0-9a-f]{64}$"
_MAX_CONTEXT_ITEMS = 256
_ESTIMATE_FIELDS = frozenset(
    {
        "frequency",
        "frequency_estimate",
        "frequency_score",
        "selectivity",
        "selectivity_estimate",
        "selectivity_score",
        "prevalence",
        "prevalence_estimate",
        "prevalence_score",
        "hit_count",
        "hit_volume",
        "hit_volume_estimate",
        "hit_volume_score",
        "estimated_hit_volume",
        "estimated_frequency",
        "estimated_selectivity",
        "estimated_prevalence",
    }
)


class ProposalOperator(StrEnum):
    """Closed descriptive operators; none of these is executable."""

    EXACT = "exact"
    EQUALS = "exact"
    CONTAINS = "contains"
    HEX_PATTERN = "hex_pattern"
    MASKED_HEX_PATTERN = "hex_pattern"
    CODE_NGRAM = "code_ngram"
    OPCODE_SEQUENCE = "opcode_sequence"
    IMPORT_NAME = "import_name"
    EXPORT_NAME = "export_name"
    SECTION_NAME = "section_name"
    CAPABILITY = "capability"
    SIMILARITY_HASH = "similarity_hash"
    STRUCTURAL_METADATA = "structural_metadata"
    RELATION = "relation"
    LITERAL_STRING = "exact"
    HEX = "hex_pattern"
    MASKED_HEX = "hex_pattern"
    HASH = "similarity_hash"


# Stable public aliases make the contract convenient for callers without
# introducing another operator vocabulary.
InvariantProposalOperator = ProposalOperator
CandidateInvariantOperator = ProposalOperator


class YaraConditionOperator(StrEnum):
    ALL_OF = "all_of"
    ANY_OF = "any_of"
    AT_LEAST = "at_least"
    ALL = "all_of"
    ANY = "any_of"


class _StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        populate_by_name=True,
        str_strip_whitespace=True,
    )


class YaraConditionProposal(_StrictModel):
    operator: YaraConditionOperator
    references: list[str] = Field(min_length=1, max_length=64)
    minimum: int | None = Field(default=None, ge=1, le=64)

    @field_validator("references")
    @classmethod
    def _references_are_bounded(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 256 for item in value):
            raise ValueError("YARA references must be non-empty and bounded")
        if len(value) != len(set(value)):
            raise ValueError("YARA references must be unique")
        return value

    @field_validator("minimum")
    @classmethod
    def _minimum_only_for_at_least(cls, value: int | None, info: Any) -> int | None:
        operator = info.data.get("operator")
        if operator is not None and operator is not YaraConditionOperator.AT_LEAST and value:
            raise ValueError("minimum is only valid for at_least")
        return value


class CandidateInvariantProposal(_StrictModel):
    proposal_id: str = Field(
        default="",
        max_length=128,
        validation_alias=AliasChoices("proposal_id", "candidate_id"),
    )
    operator: ProposalOperator
    invariant_type: InvariantType
    pattern: str = Field(max_length=4096)
    category: InvariantCategory
    semantic_justification: str = Field(min_length=1, max_length=2000)
    provenance_refs: list[str] = Field(min_length=1, max_length=64)

    @field_validator("proposal_id")
    @classmethod
    def _proposal_id_is_bounded(cls, value: str) -> str:
        if value and any(ord(char) < 32 for char in value):
            raise ValueError("proposal_id contains a control character")
        return value

    @field_validator("pattern")
    @classmethod
    def _pattern_is_bounded(cls, value: str) -> str:
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ValueError("pattern contains a control character")
        return value

    @field_validator("provenance_refs")
    @classmethod
    def _provenance_refs_are_bounded(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 256 for item in value):
            raise ValueError("provenance references must be non-empty and bounded")
        if len(value) != len(set(value)):
            raise ValueError("provenance references must be unique")
        return value

    @property
    def candidate_id(self) -> str:
        return self.proposal_id


class YaraDraftProposal(_StrictModel):
    name: str = Field(
        min_length=1,
        max_length=128,
        validation_alias=AliasChoices("name", "rule_name"),
    )
    description: str = Field(min_length=1, max_length=2000)
    condition: YaraConditionProposal = Field(
        validation_alias=AliasChoices("condition", "proposed_condition")
    )
    data: dict[str, Any] = Field(default_factory=dict)
    proposal_refs: list[str] = Field(default_factory=list, max_length=64)
    provenance_refs: list[str] = Field(default_factory=list, max_length=64)

    _forbidden_data_keys: ClassVar[frozenset[str]] = frozenset(
        {
            "code",
            "source",
            "compiled",
            "compile_result",
            "compile",
            "validation",
            "validation_flag",
            "validated",
            "execute",
            "execution",
            "query",
            "query_result",
            "executable",
        }
    )

    @field_validator("name", "description")
    @classmethod
    def _text_is_bounded(cls, value: str) -> str:
        if any(ord(char) < 32 and char not in "\t\n\r" for char in value):
            raise ValueError("YARA text contains a control character")
        return value

    @field_validator("proposal_refs", "provenance_refs")
    @classmethod
    def _draft_refs_are_bounded(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 256 for item in value):
            raise ValueError("YARA references must be non-empty and bounded")
        if len(value) != len(set(value)):
            raise ValueError("YARA references must be unique")
        return value

    @field_validator("data")
    @classmethod
    def _data_is_structured(cls, value: dict[str, Any]) -> dict[str, Any]:
        _validate_yara_data(value, forbidden_keys=cls._forbidden_data_keys)
        return value

    @property
    def rule_name(self) -> str:
        return self.name

    @property
    def proposed_condition(self) -> YaraConditionProposal:
        return self.condition

    @model_validator(mode="after")
    def _condition_has_valid_minimum(self) -> YaraDraftProposal:
        if (
            self.condition.operator is YaraConditionOperator.AT_LEAST
            and self.condition.minimum is None
        ):
            raise ValueError("at_least requires minimum")
        return self


class ProposalResponse(_StrictModel):
    candidate_invariants: list[CandidateInvariantProposal] = Field(
        default_factory=list, max_length=64
    )
    yara_draft: YaraDraftProposal | None = None
    false_positive_risks: list[str] = Field(default_factory=list, max_length=64)
    needed_validations: list[str] = Field(default_factory=list, max_length=64)
    next_questions: list[str] = Field(default_factory=list, max_length=64)

    @field_validator("false_positive_risks", "needed_validations", "next_questions")
    @classmethod
    def _bounded_text_list(cls, value: list[str]) -> list[str]:
        if any(not item or len(item) > 2000 for item in value):
            raise ValueError("response text items must be non-empty and bounded")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return self.model_dump(mode="json")


class ProposalInputSnapshot(_StrictModel):
    """The immutable P10 input identity and its bounded structured context."""

    input_pack_sha256: str = Field(pattern=_SHA256_PATTERN)
    corpus_snapshot_sha256: str = Field(pattern=_SHA256_PATTERN)
    feature_pack_sha256: str = Field(pattern=_SHA256_PATTERN)
    code_feature_sha256: str = Field(pattern=_SHA256_PATTERN)
    capability_set_sha256: str = Field(pattern=_SHA256_PATTERN)
    goodware_baseline_id: UUID
    context: dict[str, Any] = Field(
        default_factory=dict,
        validation_alias=AliasChoices("context", "canonical_context"),
    )

    @field_validator("context")
    @classmethod
    def _context_is_bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > _MAX_CONTEXT_ITEMS:
            raise ValueError("proposal context is too large")
        return value

    def canonical_dict(self) -> dict[str, Any]:
        return _canonical_json_value(self.model_dump(mode="json"))

    def canonical_serialization(self) -> str:
        return json.dumps(
            self.canonical_dict(), ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )

    @property
    def proposal_snapshot_sha256(self) -> str:
        return hashlib.sha256(self.canonical_serialization().encode("utf-8")).hexdigest()

    @property
    def snapshot_sha256(self) -> str:
        return self.proposal_snapshot_sha256

    @property
    def immutable_references(self) -> dict[str, str]:
        value = self.model_dump(mode="json", exclude={"context"})
        return {str(key): str(item) for key, item in value.items()}


InvariantProposalResponse = ProposalResponse
ProposalSnapshot = ProposalInputSnapshot


def strip_known_estimate_fields(value: Any) -> Any:
    """Remove only explicitly known model-estimate fields before strict parsing."""

    if isinstance(value, dict):
        return {
            key: strip_known_estimate_fields(item)
            for key, item in value.items()
            if key.lower() not in _ESTIMATE_FIELDS
        }
    if isinstance(value, list):
        return [strip_known_estimate_fields(item) for item in value]
    return value


def _canonical_json_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise ValueError("non-finite numbers are not canonical JSON")
        return value
    return value


def _validate_yara_data(value: Any, *, forbidden_keys: frozenset[str], depth: int = 0) -> None:
    if depth > 8:
        raise ValueError("YARA data is too deeply nested")
    if isinstance(value, dict):
        if len(value) > _MAX_CONTEXT_ITEMS:
            raise ValueError("YARA data is too large")
        for key, item in value.items():
            if not isinstance(key, str) or key.lower() in forbidden_keys:
                raise ValueError("YARA data cannot contain executable authority")
            _validate_yara_data(item, forbidden_keys=forbidden_keys, depth=depth + 1)
        return
    if isinstance(value, list):
        if len(value) > _MAX_CONTEXT_ITEMS:
            raise ValueError("YARA data is too large")
        for item in value:
            _validate_yara_data(item, forbidden_keys=forbidden_keys, depth=depth + 1)
        return
    if isinstance(value, str):
        if len(value) > 2048:
            raise ValueError("YARA data text is too long")
        return
    if not isinstance(value, (int, float, bool)) and value is not None:
        raise ValueError("YARA data must be JSON scalar/array/object data")
