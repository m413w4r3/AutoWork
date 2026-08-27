from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, ClassVar, Literal
from uuid import UUID, uuid4

from cti_app.domain.code_features import CodeNgram, PackingSignals
from cti_app.domain.goodware import Banality
from cti_app.domain.reference_corpus import ReferenceCorpusVerdict


class InvariantType(StrEnum):
    LITERAL_STRING = "literal_string"
    HEX_PATTERN = "hex_pattern"
    CODE_NGRAM = "code_ngram"
    OPCODE_SEQUENCE = "opcode_sequence"
    IMPORT_NAME = "import_name"
    EXPORT_NAME = "export_name"
    SECTION_NAME = "section_name"
    CAPABILITY = "capability"
    SIMILARITY_HASH = "similarity_hash"
    STRUCTURAL_METADATA = "structural_metadata"
    RELATION = "relation"


class InvariantCategory(StrEnum):
    C2_INDICATOR = "c2_indicator"
    MUTEX_OR_EVENT = "mutex_or_event"
    PDB_OR_BUILD_PATH = "pdb_or_build_path"
    CONFIG_MARKER = "config_marker"
    CRYPTO_CONSTANT = "crypto_constant"
    CUSTOM_PROTOCOL = "custom_protocol"
    RANSOM_OR_UI_TEXT = "ransom_or_ui_text"
    CODE_SEQUENCE = "code_sequence"
    CAPABILITY_PATTERN = "capability_pattern"
    SIMILARITY_KEY = "similarity_key"
    LIBRARY_NOISE = "library_noise"
    PACKER_ARTIFACT = "packer_artifact"
    COMPILER_ARTIFACT = "compiler_artifact"
    GENERIC_WINAPI = "generic_winapi"
    UNKNOWN = "unknown"


class InvariantStatus(StrEnum):
    PROPOSED = "proposed"
    APPROVED_FOR_PIVOT = "approved_for_pivot"
    VALIDATED = "validated"
    REJECTED = "rejected"
    UNSELECTIVE = "unselective"
    SHARED_COMPONENT = "shared_component"


class InvariantRejectionCause(StrEnum):
    PROVENANCE_INVALID = "provenance_invalid"
    INVALID_CATEGORY = "invalid_category"
    LIBRARY_NOISE = "library_noise"
    PACKER_ARTIFACT = "packer_artifact"
    COMPILER_ARTIFACT = "compiler_artifact"
    GENERIC_WINAPI = "generic_winapi"
    BANAL = "banal"
    MULTI_FAMILY = "multi_family"
    EMPTY_PATTERN = "empty_pattern"
    PATTERN_TOO_LONG = "pattern_too_long"
    CODE_NGRAM_MASK_RATIO = "code_ngram_mask_ratio"
    CODE_NGRAM_CONTIGUOUS_FIXED_RUN = "code_ngram_contiguous_fixed_run"


def _require_text(value: str, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} cannot be empty")
    return value


def _require_sha256(value: str, field_name: str = "sample_sha256") -> None:
    if not isinstance(value, str) or len(value) != 64 or any(
        char not in "0123456789abcdef" for char in value
    ):
        raise ValueError(f"{field_name} must be a lowercase SHA-256")


def _require_aware(value: datetime, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")


def _canonical_address(value: int | str) -> str:
    if isinstance(value, int):
        if value < 0:
            raise ValueError("function_address must be non-negative")
        return f"0x{value:x}"
    value = _require_text(value, "function_address").strip().lower()
    try:
        return f"0x{int(value, 0):x}"
    except ValueError as exc:
        raise ValueError("function_address must be a numeric address") from exc


def _canonical_datetime(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds")


@dataclass(frozen=True, slots=True, kw_only=True)
class SampleFeatureProvenance:
    sample_sha256: str
    feature_id: str
    offsets: tuple[int, ...]
    kind: ClassVar[str] = "sample_feature"

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256)
        _require_text(self.feature_id, "feature_id")
        offsets = tuple(self.offsets)
        if not offsets or any(not isinstance(offset, int) or offset < 0 for offset in offsets):
            raise ValueError("offsets must contain non-negative integers")
        object.__setattr__(self, "offsets", offsets)

    def as_canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sample_sha256": self.sample_sha256,
            "feature_id": self.feature_id,
            "offsets": sorted(self.offsets),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CodeFeatureProvenance:
    sample_sha256: str
    function_address: int | str
    offset: int
    disassembler_version: str
    kind: ClassVar[str] = "code_feature"

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256)
        _canonical_address(self.function_address)
        if not isinstance(self.offset, int) or self.offset < 0:
            raise ValueError("offset must be a non-negative integer")
        _require_text(self.disassembler_version, "disassembler_version")

    def as_canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sample_sha256": self.sample_sha256,
            "function_address": _canonical_address(self.function_address),
            "offset": self.offset,
            "disassembler_version": self.disassembler_version,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ToolOutputProvenance:
    sample_sha256: str
    tool: str
    version: str
    internal_id: str
    kind: ClassVar[str] = "tool_output"

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256)
        _require_text(self.tool, "tool")
        _require_text(self.version, "version")
        _require_text(self.internal_id, "internal_id")

    def as_canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sample_sha256": self.sample_sha256,
            "tool": self.tool,
            "version": self.version,
            "internal_id": self.internal_id,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class CapabilityProvenance:
    sample_sha256: str
    capability_id: str
    addresses: tuple[str, ...]
    kind: ClassVar[str] = "capability"

    def __post_init__(self) -> None:
        _require_sha256(self.sample_sha256)
        _require_text(self.capability_id, "capability_id")
        addresses = tuple(_require_text(address, "addresses item") for address in self.addresses)
        if not addresses:
            raise ValueError("addresses cannot be empty")
        object.__setattr__(self, "addresses", addresses)

    def as_canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "sample_sha256": self.sample_sha256,
            "capability_id": self.capability_id,
            "addresses": sorted(self.addresses),
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class ReportClaimProvenance:
    claim_id: str | UUID
    source_document: str | UUID
    kind: ClassVar[str] = "report_claim"

    def __post_init__(self) -> None:
        _require_text(str(self.claim_id), "claim_id")
        _require_text(str(self.source_document), "source_document")
        object.__setattr__(self, "claim_id", str(self.claim_id))
        object.__setattr__(self, "source_document", str(self.source_document))

    def as_canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "claim_id": self.claim_id,
            "source_document": self.source_document,
        }


@dataclass(frozen=True, slots=True, kw_only=True)
class AnalystManualProvenance:
    actor_id: str
    occurred_at: datetime
    motif: str
    kind: ClassVar[str] = "analyst_manual"

    def __post_init__(self) -> None:
        _require_text(self.actor_id, "actor_id")
        _require_text(self.motif, "motif")
        _require_aware(self.occurred_at, "occurred_at")

    def as_canonical_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "actor_id": self.actor_id,
            "occurred_at": _canonical_datetime(self.occurred_at),
            "motif": self.motif,
        }


type InvariantProvenance = (
    SampleFeatureProvenance
    | CodeFeatureProvenance
    | ToolOutputProvenance
    | CapabilityProvenance
    | ReportClaimProvenance
    | AnalystManualProvenance
)


def canonical_pattern(pattern: str) -> str:
    if not isinstance(pattern, str):
        raise ValueError("pattern must be a string")
    return pattern.strip()


SIMILARITY_HASH_SUBTYPES = (
    "imphash",
    "ssdeep",
    "tlsh",
    "rich_header_hash",
    "vhash",
    "main_icon_dhash",
)


def parse_similarity_hash_pattern(pattern: str) -> tuple[str, str] | None:
    value = canonical_pattern(pattern)
    if not value:
        return None
    if value.startswith("{"):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return None
        if not isinstance(decoded, dict) or set(decoded) != {"subtype", "value"}:
            return None
        subtype, content = decoded["subtype"], decoded["value"]
    elif ":" in value:
        subtype, content = value.split(":", 1)
    else:
        return None
    if (
        not isinstance(subtype, str)
        or subtype not in SIMILARITY_HASH_SUBTYPES
        or not isinstance(content, str)
        or not content.strip()
    ):
        return None
    return subtype, content.strip().lower()


def canonical_provenance(provenance: InvariantProvenance) -> dict[str, Any]:
    if not isinstance(
        provenance,
        (
            SampleFeatureProvenance,
            CodeFeatureProvenance,
            ToolOutputProvenance,
            CapabilityProvenance,
            ReportClaimProvenance,
            AnalystManualProvenance,
        ),
    ):
        raise ValueError("invalid invariant provenance")
    try:
        return provenance.as_canonical_dict()
    except AttributeError as exc:
        raise ValueError("invalid invariant provenance") from exc


def canonical_provenances(
    provenances: Sequence[InvariantProvenance],
) -> tuple[InvariantProvenance, ...]:
    items = [(canonical_provenance(item), item) for item in provenances]
    items.sort(
        key=lambda item: json.dumps(
            item[0], ensure_ascii=False, separators=(",", ":"), sort_keys=True
        )
    )
    return tuple(item for _, item in items)


def make_proposal_key(
    *,
    investigation_id: UUID,
    invariant_type: InvariantType,
    pattern: str,
    provenances: Sequence[InvariantProvenance] | None = None,
    provenance: InvariantProvenance | None = None,
) -> str:
    if provenances is None:
        if provenance is None:
            raise ValueError("provenances cannot be empty")
        provenances = (provenance,)
    elif provenance is not None:
        raise ValueError("provide provenances or provenance, not both")
    canonical_set = sorted(
        (canonical_provenance(item) for item in provenances),
        key=lambda item: json.dumps(
            item, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ),
    )
    payload = {
        "investigation_id": str(investigation_id),
        "type": InvariantType(invariant_type).value,
        "pattern": canonical_pattern(pattern),
        "provenances": canonical_set,
    }
    encoded = json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True, kw_only=True)
class ResolvedFeature:
    source_id: str
    sample_sha256: str | None
    feature_kind: str | None
    normalized_value: str | None
    code_ngram: CodeNgram | None = None
    packing: PackingSignals | None = None

    def __post_init__(self) -> None:
        _require_text(self.source_id, "source_id")
        if self.sample_sha256 is not None:
            _require_sha256(self.sample_sha256)
        if self.feature_kind is not None:
            _require_text(self.feature_kind, "feature_kind")
        if self.normalized_value is not None:
            _require_text(self.normalized_value, "normalized_value")


@dataclass(frozen=True, slots=True, kw_only=True)
class FeatureMeasurements:
    reference_members: tuple[tuple[UUID, str], ...] = ()
    eligible_samples_by_family: Mapping[str, int] = field(default_factory=dict)
    benign_prevalence: int | None = None
    positive_support: int | None = None

    def __post_init__(self) -> None:
        if self.benign_prevalence is not None and self.benign_prevalence < 0:
            raise ValueError("benign_prevalence must be non-negative")
        if self.positive_support is not None and self.positive_support < 0:
            raise ValueError("positive_support must be non-negative")


@dataclass(frozen=True, slots=True, kw_only=True)
class CandidateInvariant:
    investigation_id: UUID
    type: InvariantType
    category: InvariantCategory
    pattern: str
    proposal_key: str
    provenances: tuple[InvariantProvenance, ...]
    status: InvariantStatus = InvariantStatus.PROPOSED
    banality: Banality = Banality.UNKNOWN
    banality_occurrence_count: int | None = None
    goodware_baseline_id: UUID | None = None
    corpus_verdict: ReferenceCorpusVerdict = ReferenceCorpusVerdict.UNKNOWN
    corpus_malware_sample_count: int | None = None
    family_labels: tuple[str, ...] = ()
    benign_prevalence: int | None = None
    positive_support: int | None = None
    positive_sample_confirmed: bool = False
    masked_pattern: str | None = None
    byte_count: int | None = None
    fixed_byte_count: int | None = None
    masked_byte_count: int | None = None
    longest_fixed_run: int | None = None
    likely_packed: bool | None = None
    id: UUID = field(default_factory=uuid4)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "type", InvariantType(self.type))
        object.__setattr__(self, "category", InvariantCategory(self.category))
        object.__setattr__(self, "status", InvariantStatus(self.status))
        object.__setattr__(self, "banality", Banality(self.banality))
        object.__setattr__(self, "corpus_verdict", ReferenceCorpusVerdict(self.corpus_verdict))
        pattern = canonical_pattern(self.pattern)
        if not pattern:
            raise ValueError("pattern cannot be empty")
        object.__setattr__(self, "pattern", pattern)
        if len(self.proposal_key) != 64 or any(
            char not in "0123456789abcdef" for char in self.proposal_key
        ):
            raise ValueError("proposal_key must be lowercase SHA-256")
        if any(value < 0 for value in (
            self.banality_occurrence_count,
            self.corpus_malware_sample_count,
            self.benign_prevalence,
            self.positive_support,
            self.byte_count,
            self.fixed_byte_count,
            self.masked_byte_count,
            self.longest_fixed_run,
        ) if value is not None):
            raise ValueError("invariant measurements cannot be negative")
        if not self.provenances:
            raise ValueError("at least one provenance is required")
        canonical_items = [
            (canonical_provenance(provenance), provenance) for provenance in self.provenances
        ]
        canonical_items.sort(
            key=lambda item: json.dumps(
                item[0], ensure_ascii=False, separators=(",", ":"), sort_keys=True
            )
        )
        canonical_json = [
            json.dumps(item[0], ensure_ascii=False, separators=(",", ":"), sort_keys=True)
            for item in canonical_items
        ]
        if len(canonical_json) != len(set(canonical_json)):
            raise ValueError("duplicate invariant provenance")
        object.__setattr__(self, "provenances", tuple(item[1] for item in canonical_items))
        if self.created_at.tzinfo is None or self.created_at.utcoffset() is None:
            raise ValueError("created_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class InvariantRejection:
    investigation_id: UUID
    proposal_key: str
    cause: InvariantRejectionCause
    reason: str
    type: str
    category: str
    pattern: str
    cycle_number: int | None = None
    id: UUID = field(default_factory=uuid4)
    occurred_at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def __post_init__(self) -> None:
        object.__setattr__(self, "cause", InvariantRejectionCause(self.cause))
        _require_text(self.reason, "reason")
        if len(self.reason) > 500:
            object.__setattr__(self, "reason", self.reason[:500])
        _require_text(self.type, "type")
        _require_text(self.category, "category")
        object.__setattr__(self, "pattern", canonical_pattern(self.pattern))
        if len(self.proposal_key) != 64 or any(
            char not in "0123456789abcdef" for char in self.proposal_key
        ):
            raise ValueError("proposal_key must be lowercase SHA-256")
        if self.cycle_number is not None and self.cycle_number < 1:
            raise ValueError("cycle_number must be positive")
        if self.occurred_at.tzinfo is None or self.occurred_at.utcoffset() is None:
            raise ValueError("occurred_at must be timezone-aware")


@dataclass(frozen=True, slots=True, kw_only=True)
class InvariantTransition:
    invariant_id: UUID
    from_status: InvariantStatus
    to_status: InvariantStatus
    actor_id: str
    occurred_at: datetime
    reason: str
    id: UUID = field(default_factory=uuid4)

    def __post_init__(self) -> None:
        object.__setattr__(self, "from_status", InvariantStatus(self.from_status))
        object.__setattr__(self, "to_status", InvariantStatus(self.to_status))
        if self.from_status is self.to_status:
            raise ValueError("a status transition must change status")
        _require_text(self.actor_id, "actor_id")
        _require_text(self.reason, "reason")
        object.__setattr__(self, "reason", self.reason[:500])
        _require_aware(self.occurred_at, "occurred_at")


def m2_feature_kind(invariant_type: InvariantType, pattern: str) -> tuple[str, str] | None:
    mapping = {
        InvariantType.LITERAL_STRING: "string",
        InvariantType.HEX_PATTERN: "opcode_fragment16",
        InvariantType.OPCODE_SEQUENCE: "opcode_fragment16",
        InvariantType.CODE_NGRAM: "code_ngram",
        InvariantType.IMPORT_NAME: "import",
        InvariantType.EXPORT_NAME: "export",
        InvariantType.SECTION_NAME: "section",
        InvariantType.CAPABILITY: "capability",
    }
    if invariant_type is InvariantType.SIMILARITY_HASH:
        parsed = parse_similarity_hash_pattern(pattern)
        return parsed if parsed else None
    feature_kind = mapping.get(invariant_type)
    return (feature_kind, canonical_pattern(pattern).lower()) if feature_kind else None


def likely_packed(
    packing: PackingSignals | None,
    *,
    operator: Literal["ALL", "ANY"],
    max_executable_section_entropy_gte: float,
    executable_bytes_per_function_gte: int,
    known_packer_marker_hit: bool,
) -> bool | None:
    if packing is None:
        return None
    if (
        packing.max_executable_section_entropy is None
        or packing.executable_bytes_per_function is None
    ):
        return None
    signals = (
        packing.max_executable_section_entropy >= max_executable_section_entropy_gte,
        packing.executable_bytes_per_function >= executable_bytes_per_function_gte,
        bool(packing.known_packer_marker_hits) == known_packer_marker_hit,
    )
    if operator == "ALL":
        return all(signals)
    if operator == "ANY":
        return any(signals)
    raise ValueError("likely_packed operator must be ALL or ANY")
