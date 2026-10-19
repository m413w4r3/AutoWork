"""ToolRegistry adapters for deterministic, local network protocol analysis.

Composition call contract:
register_protocol_tools(
    registry,
    service=NetworkProtocolWorkbench(network_workbench),
    recorder=evidence_recorder,
    model_store=ProductionProtocolModelStore(production_artifact_store),
)
This module registers definitions only. It does not modify investigation composition.
"""

from __future__ import annotations

import hashlib
import math
import re
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol
from uuid import UUID

from cti_app.application.evidence_recorder import EvidenceRecorder, sanitize_evidence_text
from cti_app.application.investigation_tools import (
    ToolConfidentiality,
    ToolDefinition,
    ToolEffect,
    ToolExecutionContext,
    ToolRegistry,
    ToolResult,
)
from cti_app.application.network_protocol_model import (
    HypothesisNotReplayableError,
    NetworkProtocolWorkbench,
    ProtocolTraceError,
    ProtocolTraceRequestV1,
    render_protocol_model,
)
from cti_app.application.network_workbench import NetworkCaptureRejected
from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    ProductionArtifactStore,
)
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    ToolInvocationStatus,
    canonical_json_bytes,
)
from cti_app.domain.network_protocol import (
    NetworkCaptureFormat,
    NetworkCaptureRefV1,
    NetworkExtractionLimitsV1,
    NetworkTransport,
    StreamDirection,
)
from cti_app.domain.protocol_model import (
    AssuranceLevel,
    BinaryFunctionProvenanceV1,
    FieldDefinitionV1,
    FieldStability,
    MessageDefinitionV1,
    ProtocolCharacteristicKindV1,
    ProtocolCharacteristicV1,
    ProtocolEvidenceLevelV1,
    ProtocolEvidenceRefV1,
    ProtocolHypothesisProposalV1,
    ProtocolModelV1,
    ProtocolStateV1,
    ProtocolTransitionV1,
    ProtocolValidationStatus,
)

NETWORK_PROTOCOL_COMPARE = "network.protocol.compare"
NETWORK_PROTOCOL_HYPOTHESIZE = "network.protocol.hypothesize"
NETWORK_PROTOCOL_VERIFY = "network.protocol.verify"
NETWORK_PROTOCOL_RENDER = "network.protocol.render"
NETWORK_PROTOCOL_DOCUMENT = "network.protocol.document"
PROTOCOL_TOOL_NAMES = frozenset(
    {
        NETWORK_PROTOCOL_COMPARE,
        NETWORK_PROTOCOL_HYPOTHESIZE,
        NETWORK_PROTOCOL_VERIFY,
        NETWORK_PROTOCOL_RENDER,
        NETWORK_PROTOCOL_DOCUMENT,
    }
)
_MAX_TOOL_SECONDS = 120
_MODEL_BUCKET = "network-protocol-models"
_FLOW_PATTERN = "^[0-9a-f]{24}$"
_SHA_PATTERN = "^[0-9a-f]{64}$"
_UUID_SCHEMA: dict[str, Any] = {"type": "string", "format": "uuid"}
_DOC_ID_PATTERN = "^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$"
_DOCUMENT_MAX_BYTES = 128 * 1024
_DOCUMENT_REF_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["observation", "finding", "claim"]},
        "ref": {"type": "string", "pattern": _DOC_ID_PATTERN},
    },
    "required": ["kind", "ref"],
    "additionalProperties": False,
}
_DOCUMENT_REF_LIST_SCHEMA: dict[str, Any] = {
    "type": "array",
    "minItems": 1,
    "maxItems": 16,
    "uniqueItems": True,
    "items": _DOCUMENT_REF_SCHEMA,
}
_CAPTURE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "blob_id": _UUID_SCHEMA,
        "sha256": {"type": "string", "pattern": _SHA_PATTERN},
        "size_bytes": {"type": "integer", "minimum": 1, "maximum": 67_108_864},
        "format": {"type": "string", "enum": ["pcap", "pcapng"]},
    },
    "required": ["blob_id", "sha256", "size_bytes", "format"],
    "additionalProperties": False,
}
_TRACE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "capture_ref": _CAPTURE_SCHEMA,
        "flow_id": {"type": "string", "pattern": _FLOW_PATTERN},
        "sample_sha256": {"type": ["string", "null"], "pattern": _SHA_PATTERN},
        "report_id": {"type": ["string", "null"], "format": "uuid"},
        "holdout": {"type": "boolean"},
    },
    "required": ["capture_ref", "flow_id", "sample_sha256", "report_id", "holdout"],
    "additionalProperties": False,
}
_TEST_STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {
            "type": "string",
            "enum": ["message_length", "field_value", "state_transition", "direction"],
        },
        "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
        "field_id": {"type": ["string", "null"], "maxLength": 128},
        "transition_id": {"type": ["string", "null"], "maxLength": 128},
    },
    "required": ["kind", "message_id", "field_id", "transition_id"],
    "additionalProperties": False,
}
_PROPOSAL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "schema_version": {"type": "string", "enum": ["ProtocolHypothesisProposalV1"]},
        "messages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "ordinal": {"type": "integer", "minimum": 1, "maximum": 10_000},
                    "direction": {
                        "type": "string",
                        "enum": [
                            "originator_to_responder",
                            "responder_to_originator",
                            "unresolved",
                        ],
                    },
                    "min_length": {"type": "integer", "minimum": 0, "maximum": 8_388_608},
                    "max_length": {"type": "integer", "minimum": 0, "maximum": 8_388_608},
                    "encrypted_opaque": {"type": "boolean"},
                },
                "required": [
                    "message_id",
                    "name",
                    "ordinal",
                    "direction",
                    "min_length",
                    "max_length",
                    "encrypted_opaque",
                ],
                "additionalProperties": False,
            },
        },
        "fields": {
            "type": "array",
            "maxItems": 256,
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "offset": {"type": "integer", "minimum": 0, "maximum": 8_388_608},
                    "length": {"type": "integer", "minimum": 1, "maximum": 65_536},
                    "endianness": {
                        "type": ["string", "null"],
                        "enum": ["big", "little", "none", "unknown", None],
                    },
                    "encoding": {"type": ["string", "null"], "maxLength": 64},
                    "condition": {"type": ["string", "null"], "maxLength": 256},
                    "justification": {"type": ["string", "null"], "maxLength": 1024},
                    "expected_hex": {
                        "type": ["string", "null"],
                        "pattern": "^(?:[0-9a-f]{2}){1,16}$",
                    },
                    "encrypted_opaque": {"type": "boolean"},
                },
                "required": [
                    "field_id",
                    "message_id",
                    "name",
                    "offset",
                    "length",
                    "endianness",
                    "encoding",
                    "condition",
                    "expected_hex",
                    "encrypted_opaque",
                ],
                "additionalProperties": False,
            },
        },
        "transitions": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "properties": {
                    "transition_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "from_state": {"type": "string", "minLength": 1, "maxLength": 128},
                    "to_state": {"type": "string", "minLength": 1, "maxLength": 128},
                    "message_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "trigger": {"type": "string", "minLength": 1, "maxLength": 128},
                },
                "required": ["transition_id", "from_state", "to_state", "message_id", "trigger"],
                "additionalProperties": False,
            },
        },
        "test_plan": {
            "type": "array",
            "maxItems": 256,
            "items": _TEST_STEP_SCHEMA,
        },
        "characteristics": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [item.value for item in ProtocolCharacteristicKindV1],
                    },
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "statement": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "justification": {"type": "string", "minLength": 1, "maxLength": 1024},
                },
                "required": ["kind", "name", "statement", "justification"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["schema_version", "messages", "fields", "transitions", "test_plan"],
    "additionalProperties": False,
}

_DOCUMENTED_MODEL_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "protocol": {"type": ["string", "null"], "maxLength": 128},
        "transport": {"type": "string", "enum": ["tcp", "udp", "other", "unknown"]},
        "client_server_orientation": {
            "type": "string",
            "enum": ["client_to_server", "server_to_client", "unresolved"],
        },
        "evidence_refs": _DOCUMENT_REF_LIST_SCHEMA,
        "messages": {
            "type": "array",
            "minItems": 1,
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "message_id": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "ordinal": {"type": "integer", "minimum": 1, "maximum": 10000},
                    "direction": {
                        "type": "string",
                        "enum": [
                            "originator_to_responder",
                            "responder_to_originator",
                            "unresolved",
                        ],
                    },
                    "evidence_refs": _DOCUMENT_REF_LIST_SCHEMA,
                },
                "required": ["message_id", "name", "ordinal", "direction", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "states": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "state_id": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "evidence_refs": _DOCUMENT_REF_LIST_SCHEMA,
                },
                "required": ["state_id", "name", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "fields": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "properties": {
                    "field_id": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "message_id": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "evidence_refs": _DOCUMENT_REF_LIST_SCHEMA,
                },
                "required": ["field_id", "message_id", "name", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "transitions": {
            "type": "array",
            "maxItems": 128,
            "items": {
                "type": "object",
                "properties": {
                    "transition_id": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "from_state": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "to_state": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "message_id": {"type": "string", "pattern": _DOC_ID_PATTERN},
                    "trigger": {"type": "string", "minLength": 1, "maxLength": 128},
                    "evidence_refs": _DOCUMENT_REF_LIST_SCHEMA,
                },
                "required": [
                    "transition_id",
                    "from_state",
                    "to_state",
                    "message_id",
                    "trigger",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
        },
        "characteristics": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "kind": {
                        "type": "string",
                        "enum": [item.value for item in ProtocolCharacteristicKindV1],
                    },
                    "name": {"type": "string", "minLength": 1, "maxLength": 128},
                    "statement": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "justification": {"type": "string", "minLength": 1, "maxLength": 1024},
                    "evidence_refs": _DOCUMENT_REF_LIST_SCHEMA,
                },
                "required": ["kind", "name", "statement", "justification", "evidence_refs"],
                "additionalProperties": False,
            },
        },
        "static_function_provenance": {
            "type": "array",
            "maxItems": 64,
            "items": {
                "type": "object",
                "properties": {
                    "sample_sha256": {"type": "string", "pattern": _SHA_PATTERN},
                    "binary_sha256": {"type": "string", "pattern": _SHA_PATTERN},
                    "provider": {"type": "string", "minLength": 1, "maxLength": 128},
                    "provider_version": {"type": "string", "minLength": 1, "maxLength": 64},
                    "function_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    "evidence_refs": _DOCUMENT_REF_LIST_SCHEMA,
                },
                "required": [
                    "sample_sha256",
                    "binary_sha256",
                    "provider",
                    "provider_version",
                    "function_id",
                    "evidence_refs",
                ],
                "additionalProperties": False,
            },
        },
    },
    "required": [
        "protocol",
        "transport",
        "client_server_orientation",
        "evidence_refs",
        "messages",
        "states",
        "fields",
        "transitions",
    ],
    "additionalProperties": False,
}


class ProtocolModelBlobStore(Protocol):
    async def put_model(self, model: ProtocolModelV1) -> tuple[UUID, str]: ...

    async def get_model(self, blob_id: UUID, *, investigation_id: UUID) -> ProtocolModelV1: ...


class ProtocolModelNotInInvestigation(PermissionError):
    """The requested immutable protocol model belongs to another investigation."""


class ProtocolDocumentReferenceError(ValueError):
    """A documentary protocol source is missing or belongs to another investigation."""


@dataclass(frozen=True, slots=True)
class ProtocolDocumentSourceBundle:
    subject_id: str
    source_sha256s: Mapping[tuple[str, str], str]


class ProtocolDocumentEvidenceResolver(Protocol):
    async def resolve(
        self,
        investigation_id: UUID,
        references: tuple[tuple[str, str], ...],
    ) -> ProtocolDocumentSourceBundle: ...


class RepositoryProtocolDocumentEvidenceResolver:
    """Resolve only evidence already stored on this investigation or its article."""

    def __init__(
        self,
        *,
        investigations: Any,
        observations: Any,
        findings: Any,
        article_snapshots: Any,
    ) -> None:
        self._investigations = investigations
        self._observations = observations
        self._findings = findings
        self._article_snapshots = article_snapshots

    async def resolve(
        self,
        investigation_id: UUID,
        references: tuple[tuple[str, str], ...],
    ) -> ProtocolDocumentSourceBundle:
        investigation = await self._investigations.get(investigation_id)
        if investigation is None:
            raise ProtocolDocumentReferenceError("investigation_not_found")
        observations = {
            str(item.id): item
            for item in await self._observations.list_for_investigation(investigation_id)
            if item.investigation_id == investigation_id
        }
        findings = {
            str(item.id): item
            for item in await self._findings.list_for_investigation(investigation_id)
            if item.investigation_id == investigation_id
        }
        snapshot = await self._article_snapshots.resolve(investigation.synthesis_artifact_id)
        claims = {
            str(item.id): item
            for item in snapshot.article.claims
            if isinstance(getattr(item, "id", None), str)
        }

        source_hashes: dict[tuple[str, str], str] = {}
        for kind, reference_id in references:
            key = (kind, reference_id)
            if key in source_hashes:
                continue
            if kind == "observation":
                source = observations.get(reference_id)
                if source is None:
                    raise ProtocolDocumentReferenceError("observation_not_found")
                source_hash = source.sha256
            elif kind == "finding":
                source = findings.get(reference_id)
                if source is None:
                    raise ProtocolDocumentReferenceError("finding_not_found")
                source_hash = _finding_source_sha256(source)
            elif kind == "claim":
                source = claims.get(reference_id)
                if source is None:
                    raise ProtocolDocumentReferenceError("claim_not_found")
                dump = getattr(source, "model_dump", None)
                payload = (
                    dump(mode="json", by_alias=True)
                    if callable(dump)
                    else {
                        "id": source.id,
                        "statement": getattr(source, "statement", ""),
                        "source_refs": list(getattr(source, "source_refs", ())),
                    }
                )
                source_hash = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
            else:
                raise ProtocolDocumentReferenceError("source_kind_invalid")
            if not isinstance(source_hash, str) or re.fullmatch(_SHA_PATTERN, source_hash) is None:
                raise ProtocolDocumentReferenceError("source_hash_invalid")
            source_hashes[key] = source_hash
        return ProtocolDocumentSourceBundle(str(investigation.subject_id), source_hashes)


def _finding_source_sha256(finding: Any) -> str:
    payload = {
        "id": str(finding.id),
        "version": finding.version,
        "kind": finding.kind.value,
        "assessment": finding.assessment.value,
        "statement": finding.statement,
        "observation_refs": [str(item) for item in finding.observation_refs],
        "claim_refs": list(finding.claim_refs),
        "novelty": finding.novelty.value if finding.novelty is not None else None,
        "scope": finding.scope,
        "limitations": list(finding.limitations),
    }
    return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()


class ProductionProtocolModelStore:
    """Store semantic models as immutable canonical JSON blobs."""

    def __init__(self, artifacts: ProductionArtifactStore) -> None:
        self._artifacts = artifacts

    async def put_model(self, model: ProtocolModelV1) -> tuple[UUID, str]:
        encoded = model.canonical_json_bytes()
        if len(encoded) > MAX_ARTIFACT_BYTES:
            raise ValueError(f"protocol model exceeds {MAX_ARTIFACT_BYTES} bytes")
        blob_id = await self._artifacts.put_bytes(
            encoded,
            bucket=_MODEL_BUCKET,
            mime_type="application/json",
        )
        return blob_id, model.canonical_sha256

    async def get_model(self, blob_id: UUID, *, investigation_id: UUID) -> ProtocolModelV1:
        model = ProtocolModelV1.from_dict(await self._artifacts.read_json(blob_id))
        if model.investigation_id != investigation_id:
            raise ProtocolModelNotInInvestigation
        return model


def protocol_tool_capabilities() -> frozenset[str]:
    """Return L6 capability keys; no optional binary is needed for local replay."""
    return PROTOCOL_TOOL_NAMES


def register_protocol_tools(
    registry: ToolRegistry,
    *,
    service: NetworkProtocolWorkbench | None,
    recorder: EvidenceRecorder,
    model_store: ProtocolModelBlobStore,
    document_evidence: ProtocolDocumentEvidenceResolver | None = None,
    tool_seconds: int = 65,
) -> None:
    if not 1 <= tool_seconds <= _MAX_TOOL_SECONDS:
        raise ValueError(f"tool_seconds must be between 1 and {_MAX_TOOL_SECONDS}")
    definitions: list[tuple[str, Mapping[str, Any]]] = []
    if service is not None:
        definitions.extend(
            (
                (
                    NETWORK_PROTOCOL_COMPARE,
                    _operation_schema(
                        "Compare observed messages and fields across captures.",
                        properties={
                            "subject_id": {"type": "string", "minLength": 1, "maxLength": 256},
                            "traces": _trace_array_schema(),
                        },
                        required=["subject_id", "traces"],
                    ),
                ),
                (
                    NETWORK_PROTOCOL_HYPOTHESIZE,
                    _operation_schema(
                        "Replay a strict schema and state-machine hypothesis on training traces.",
                        properties={
                            "subject_id": {"type": "string", "minLength": 1, "maxLength": 256},
                            "proposal": _PROPOSAL_SCHEMA,
                            "traces": _trace_array_schema(),
                        },
                        required=["subject_id", "proposal", "traces"],
                    ),
                ),
                (
                    NETWORK_PROTOCOL_VERIFY,
                    _operation_schema(
                        "Check a stored model against holdout captures.",
                        properties={
                            "model_blob_id": _UUID_SCHEMA,
                            "traces": _trace_array_schema(),
                        },
                        required=["model_blob_id", "traces"],
                    ),
                ),
            ),
        )
    definitions.append(
        (
            NETWORK_PROTOCOL_RENDER,
            _operation_schema(
                "Produce sequence, state-machine, and message-format specs from "
                "a stored protocol model.",
                properties={"model_blob_id": _UUID_SCHEMA},
                required=["model_blob_id"],
            ),
        )
    )
    if document_evidence is not None:
        definitions.append(
            (
                NETWORK_PROTOCOL_DOCUMENT,
                _operation_schema(
                    "Build a documented, not observed protocol model "
                    "from cited investigation facts.",
                    properties={"model": _DOCUMENTED_MODEL_SCHEMA},
                    required=["model"],
                ),
            )
        )
    limitations: tuple[str, ...] = (
        "Les hypothèses sont des structures typées validées contre des flux locaux.",
        "Les octets bruts restent dans les blobs de capture immuables et ne sont jamais retournés.",
        "encrypted_opaque reste opaque; les champs et transitions citent leurs frames et offsets.",
    )
    for name, schema in definitions:
        if name == NETWORK_PROTOCOL_DOCUMENT:
            limitations = (
                "Modèle documentaire construit uniquement à partir de faits cités "
                "de cette investigation.",
                "Documenté, non observé; aucun frame, offset, octet ou échange capturé n'est créé.",
            )
        registry.register(
            ToolDefinition(
                name=name,
                schema_version="1",
                input_schema=schema,
                effect=ToolEffect.LOCAL_COMPUTE,
                cost_model={BudgetDimension.TOOL_SECONDS: tool_seconds},
                confidentiality=ToolConfidentiality.RESTRICTED,
                limitations=limitations,
                capability_key=name,
            ),
            _ProtocolToolAdapter(
                name,
                service=service,
                recorder=recorder,
                model_store=model_store,
                document_evidence=document_evidence,
                reserved_seconds=tool_seconds,
            ),
        )


async def _document_protocol_model(
    value: object,
    *,
    investigation_id: UUID,
    evidence_resolver: ProtocolDocumentEvidenceResolver,
) -> ProtocolModelV1:
    if not isinstance(value, Mapping):
        raise ValueError("model must be an object")
    try:
        encoded = canonical_json_bytes(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("model must contain JSON-safe values") from exc
    if len(encoded) > _DOCUMENT_MAX_BYTES:
        raise ValueError("documentary protocol model exceeds its input byte limit")

    root = _require_document_object(
        value,
        {
            "protocol",
            "transport",
            "client_server_orientation",
            "evidence_refs",
            "messages",
            "states",
            "fields",
            "transitions",
        },
        optional={"characteristics", "static_function_provenance"},
    )
    messages_raw = _document_list(root["messages"], "messages", minimum=1, maximum=64)
    states_raw = _document_list(root["states"], "states", minimum=0, maximum=64)
    fields_raw = _document_list(root["fields"], "fields", minimum=0, maximum=128)
    transitions_raw = _document_list(root["transitions"], "transitions", minimum=0, maximum=128)
    characteristics_raw = _document_list(
        root.get("characteristics", []), "characteristics", minimum=0, maximum=64
    )
    static_function_rows = _document_list(
        root.get("static_function_provenance", []),
        "static_function_provenance",
        minimum=0,
        maximum=64,
    )

    reference_lists: dict[str, tuple[tuple[str, str], ...]] = {}
    all_references: list[tuple[str, str]] = []
    reference_lists["root"] = _document_references(root["evidence_refs"])
    all_references.extend(reference_lists["root"])
    characteristic_rows: list[dict[str, Any]] = []
    for row_value in characteristics_raw:
        row = _require_document_object(
            row_value,
            {"kind", "name", "statement", "justification", "evidence_refs"},
        )
        row_refs = _document_references(row["evidence_refs"])
        all_references.extend(row_refs)
        characteristic_rows.append({**row, "_references": row_refs})
    static_function_provenance_rows: list[dict[str, Any]] = []
    for row_value in static_function_rows:
        row = _require_document_object(
            row_value,
            {
                "sample_sha256",
                "binary_sha256",
                "provider",
                "provider_version",
                "function_id",
                "evidence_refs",
            },
        )
        row_refs = _document_references(row["evidence_refs"])
        all_references.extend(row_refs)
        static_function_provenance_rows.append({**row, "_references": row_refs})
    typed_rows: dict[str, list[dict[str, Any]]] = {
        "messages": [],
        "states": [],
        "fields": [],
        "transitions": [],
    }
    field_ids_by_message: dict[str, list[str]] = {}
    for collection, rows, required in (
        (
            "messages",
            messages_raw,
            {"message_id", "name", "ordinal", "direction", "evidence_refs"},
        ),
        ("states", states_raw, {"state_id", "name", "evidence_refs"}),
        (
            "fields",
            fields_raw,
            {"field_id", "message_id", "name", "evidence_refs"},
        ),
        (
            "transitions",
            transitions_raw,
            {
                "transition_id",
                "from_state",
                "to_state",
                "message_id",
                "trigger",
                "evidence_refs",
            },
        ),
    ):
        for row_value in rows:
            row = _require_document_object(row_value, required)
            row_refs = _document_references(row["evidence_refs"])
            all_references.extend(row_refs)
            typed_rows[collection].append({**row, "_references": row_refs})
            if collection == "fields":
                field_id = _document_id(row["field_id"], "field_id")
                message_id = _document_id(row["message_id"], "message_id")
                field_ids_by_message.setdefault(message_id, []).append(field_id)

    unique_references = tuple(dict.fromkeys(all_references))
    if len(all_references) > 512 or len(unique_references) > 256:
        raise ValueError("documentary protocol citations exceed the reference budget")
    bundle = await evidence_resolver.resolve(investigation_id, unique_references)

    def evidence(refs: tuple[tuple[str, str], ...]) -> tuple[ProtocolEvidenceRefV1, ...]:
        result: list[ProtocolEvidenceRefV1] = []
        for kind, reference_id in refs:
            source_sha256 = bundle.source_sha256s.get((kind, reference_id))
            if source_sha256 is None:
                raise ProtocolDocumentReferenceError("source_hash_missing")
            result.append(
                ProtocolEvidenceRefV1(
                    source=f"documented:{kind}:{reference_id}",
                    documented_ref_kind=kind,
                    documented_ref_id=reference_id,
                    documented_source_sha256=source_sha256,
                )
            )
        return tuple(result)

    model_messages = tuple(
        MessageDefinitionV1(
            message_id=_document_id(row["message_id"], "message_id"),
            name=_document_text(row["name"], "message name", 128),
            ordinal=_document_int(row["ordinal"], "message ordinal", 1, 10_000),
            directions=(StreamDirection(row["direction"]),),
            min_length=None,
            max_length=None,
            field_ids=tuple(
                field_ids_by_message.get(_document_id(row["message_id"], "message_id"), ())
            ),
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            evidence=evidence(row["_references"]),
        )
        for row in typed_rows["messages"]
    )
    model_states = tuple(
        ProtocolStateV1(
            state_id=_document_id(row["state_id"], "state_id"),
            name=_document_text(row["name"], "state name", 128),
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            evidence=evidence(row["_references"]),
        )
        for row in typed_rows["states"]
    )
    model_fields = tuple(
        FieldDefinitionV1(
            field_id=_document_id(row["field_id"], "field_id"),
            message_id=_document_id(row["message_id"], "message_id"),
            name=_document_text(row["name"], "field name", 128),
            offset=None,
            length=None,
            endianness="unknown",
            encoding=None,
            condition=None,
            source="documented_fact",
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            stability=FieldStability.UNKNOWN,
            evidence=evidence(row["_references"]),
        )
        for row in typed_rows["fields"]
    )
    model_transitions = tuple(
        ProtocolTransitionV1(
            transition_id=_document_id(row["transition_id"], "transition_id"),
            from_state=_document_id(row["from_state"], "from_state"),
            to_state=_document_id(row["to_state"], "to_state"),
            message_id=_document_id(row["message_id"], "message_id"),
            trigger=_document_text(row["trigger"], "transition trigger", 128),
            hypotheses=(),
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            evidence=evidence(row["_references"]),
        )
        for row in typed_rows["transitions"]
    )
    model_characteristics = tuple(
        ProtocolCharacteristicV1(
            kind=ProtocolCharacteristicKindV1(
                _document_enum(row["kind"], {item.value for item in ProtocolCharacteristicKindV1})
            ),
            name=_document_text(row["name"], "characteristic name", 128),
            statement=_document_text(row["statement"], "characteristic statement", 1024),
            evidence_level=ProtocolEvidenceLevelV1.ARTICLE_DOCUMENTED,
            validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
            evidence=evidence(row["_references"]),
            justification=_document_text(row["justification"], "characteristic rationale", 1024),
        )
        for row in characteristic_rows
    )
    static_function_provenance = tuple(
        BinaryFunctionProvenanceV1(
            sample_sha256=_document_text(row["sample_sha256"], "sample sha256", 64),
            binary_sha256=_document_text(row["binary_sha256"], "binary sha256", 64),
            provider=_document_text(row["provider"], "static provider", 128),
            provider_version=_document_text(row["provider_version"], "provider version", 64),
            function_id=_document_text(row["function_id"], "static function id", 128),
            evidence=evidence(row["_references"]),
            execution_mode="static_non_executed",
            evidence_level=ProtocolEvidenceLevelV1.STATIC_INFERENCE,
        )
        for row in static_function_provenance_rows
    )
    transport = _document_enum(root["transport"], {item.value for item in NetworkTransport})
    orientation = _document_enum(
        root["client_server_orientation"],
        {"client_to_server", "server_to_client", "unresolved"},
    )
    return ProtocolModelV1(
        schema_version="ProtocolModelV1",
        subject_id=bundle.subject_id,
        investigation_id=investigation_id,
        family=None,
        cluster=None,
        family_assurance=AssuranceLevel.UNCONFIRMED,
        sample_sha256s=(),
        capture_sha256s=(),
        report_ids=(),
        model_version="documented-protocol-model.v1",
        protocol=(
            None
            if root["protocol"] is None
            else _document_text(root["protocol"], "protocol name", 128)
        ),
        transport=NetworkTransport(transport),
        endpoints=(),
        client_server_orientation=orientation,
        infection_state=None,
        conversations=(),
        messages=model_messages,
        fields=model_fields,
        transitions=model_transitions,
        function_provenance=static_function_provenance,
        validation_status=ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED,
        verification_findings=(),
        test_plan=(),
        limits=NetworkExtractionLimitsV1(),
        limitations=(
            "Documenté, non observé: construit à partir de faits cités de cette investigation.",
            "Aucune capture, frame, coordonnée d'octet ou valeur d'octet n'a été inventée.",
        ),
        extractor_versions=(("protocol-document", "1"),),
        states=model_states,
        documentary_evidence=evidence(reference_lists["root"]),
        characteristics=model_characteristics,
    )


def _require_document_object(
    value: object, required: set[str], *, optional: set[str] | None = None
) -> Mapping[str, Any]:
    allowed = required | (optional or set())
    if not isinstance(value, Mapping) or not required.issubset(value) or set(value) - allowed:
        raise ValueError("documentary protocol input has missing or unknown fields")
    return value


def _document_list(value: object, name: str, *, minimum: int, maximum: int) -> list[Any]:
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{name} count is outside its bound")
    return value


def _document_references(value: object) -> tuple[tuple[str, str], ...]:
    if not isinstance(value, list) or not 1 <= len(value) <= 16:
        raise ValueError("each documentary element needs between 1 and 16 citations")
    references: list[tuple[str, str]] = []
    for item in value:
        row = _require_document_object(item, {"kind", "ref"})
        kind = _document_enum(row["kind"], {"observation", "finding", "claim"})
        reference_id = _document_id(row["ref"], "citation reference")
        if (kind, reference_id) in references:
            raise ValueError("duplicate citation reference")
        if kind in {"observation", "finding"}:
            UUID(reference_id)
        references.append((kind, reference_id))
    return tuple(references)


def _document_id(value: object, label: str) -> str:
    if not isinstance(value, str) or re.fullmatch(_DOC_ID_PATTERN, value) is None:
        raise ValueError(f"{label} has an invalid identifier")
    return value


def _document_text(value: object, label: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise ValueError(f"{label} is empty or outside its character bound")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"{label} contains a control character")
    cleaned = sanitize_evidence_text(value, max_chars=maximum).strip()
    if not cleaned:
        raise ValueError(f"{label} is empty after sanitization")
    return cleaned


def _document_int(value: object, label: str, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise ValueError(f"{label} is outside its integer bound")
    return value


def _document_enum(value: object, allowed: set[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise ValueError("documentary protocol enum value is invalid")
    return value


class _ProtocolToolAdapter:
    def __init__(
        self,
        name: str,
        *,
        service: NetworkProtocolWorkbench | None,
        recorder: EvidenceRecorder,
        model_store: ProtocolModelBlobStore,
        document_evidence: ProtocolDocumentEvidenceResolver | None,
        reserved_seconds: int,
    ) -> None:
        self.name = name
        self.service = service
        self.recorder = recorder
        self.model_store = model_store
        self.document_evidence = document_evidence
        self.reserved_seconds = reserved_seconds

    async def execute(
        self,
        arguments: Mapping[str, Any],
        context: ToolExecutionContext,
    ) -> ToolResult:
        started = time.monotonic()
        output: dict[str, Any]
        try:
            if self.name == NETWORK_PROTOCOL_DOCUMENT:
                if self.document_evidence is None:
                    raise ProtocolDocumentReferenceError("document_evidence_unavailable")
                model = await _document_protocol_model(
                    arguments.get("model"),
                    investigation_id=context.investigation_id,
                    evidence_resolver=self.document_evidence,
                )
                blob_id, stored_sha = await self.model_store.put_model(model)
                output = {
                    "schema": "ProtocolModelV1",
                    "protocol_model_blob_id": str(blob_id),
                    "model_blob_id": str(blob_id),
                    "model_sha256": stored_sha,
                    "validation_status": model.validation_status.value,
                    "message_count": len(model.messages),
                    "state_count": len(model.states),
                    "field_count": len(model.fields),
                    "transition_count": len(model.transitions),
                    "characteristic_count": len(model.characteristics),
                    "static_function_provenance_count": len(model.function_provenance),
                }
                observation_id = await self._record(context, model, blob_id, stored_sha, output)
                output["observation_ref"] = str(observation_id)
                return self._result(
                    context,
                    started,
                    ToolInvocationStatus.SUCCEEDED,
                    output,
                    (observation_id,),
                )

            if self.name == NETWORK_PROTOCOL_COMPARE:
                assert self.service is not None
                model_result = await self.service.compare(
                    subject_id=_required_str(arguments, "subject_id"),
                    investigation_id=context.investigation_id,
                    traces=_trace_requests(arguments.get("traces")),
                )
                model = model_result.model
                blob_id, stored_sha = await self.model_store.put_model(model)
                output = {
                    "schema": "ProtocolModelV1",
                    "model_blob_id": str(blob_id),
                    "model_sha256": stored_sha,
                    "compatible": model_result.compatible,
                    "stable_field_ids": list(model_result.stable_field_ids),
                    "variant_field_ids": list(model_result.variant_field_ids),
                    "family_assurance": model.family_assurance.value,
                    "message_count": len(model.messages),
                }
                observation_id = await self._record(context, model, blob_id, stored_sha, output)
                output["observation_ref"] = str(observation_id)
                return self._result(
                    context, started, ToolInvocationStatus.SUCCEEDED, output, (observation_id,)
                )

            if self.name == NETWORK_PROTOCOL_HYPOTHESIZE:
                assert self.service is not None
                proposal = ProtocolHypothesisProposalV1.from_dict(arguments.get("proposal"))
                model = await self.service.hypothesize(
                    subject_id=_required_str(arguments, "subject_id"),
                    investigation_id=context.investigation_id,
                    proposal=proposal,
                    traces=_trace_requests(arguments.get("traces")),
                )
                blob_id, stored_sha = await self.model_store.put_model(model)
                output = {
                    "schema": "ProtocolModelV1",
                    "model_blob_id": str(blob_id),
                    "model_sha256": stored_sha,
                    "validation_status": model.validation_status.value,
                    "message_count": len(model.messages),
                    "field_count": len(model.fields),
                    "transition_count": len(model.transitions),
                    "test_plan": [step.to_dict() for step in model.test_plan],
                    "characteristic_count": len(model.characteristics),
                    "validation_results": [run.to_dict() for run in model.validation_runs],
                }
                observation_id = await self._record(context, model, blob_id, stored_sha, output)
                output["observation_ref"] = str(observation_id)
                return self._result(
                    context, started, ToolInvocationStatus.SUCCEEDED, output, (observation_id,)
                )

            if self.name == NETWORK_PROTOCOL_VERIFY:
                assert self.service is not None
                model = await self._load_model_for_investigation(
                    _uuid(arguments.get("model_blob_id")), context.investigation_id
                )
                verified = await self.service.verify(
                    model=model,
                    traces=_trace_requests(arguments.get("traces")),
                    investigation_id=context.investigation_id,
                )
                blob_id, stored_sha = await self.model_store.put_model(verified)
                output = {
                    "schema": "ProtocolModelV1",
                    "model_blob_id": str(blob_id),
                    "model_sha256": stored_sha,
                    "validation_status": verified.validation_status.value,
                    "validation_results": [run.to_dict() for run in verified.validation_runs],
                    "finding_count": len(verified.verification_findings),
                    "contradiction_count": sum(
                        item.validation_status.value == "contradicted"
                        for item in verified.verification_findings
                    ),
                    "unresolved_count": sum(
                        item.validation_status.value == "unresolved"
                        for item in verified.verification_findings
                    ),
                }
                observation_id = await self._record(context, verified, blob_id, stored_sha, output)
                output["observation_ref"] = str(observation_id)
                status = (
                    ToolInvocationStatus.PARTIAL
                    if verified.validation_runs
                    and verified.validation_runs[-1].result.value
                    in {"rejected", "partial", "not_testable"}
                    else ToolInvocationStatus.SUCCEEDED
                )
                return self._result(context, started, status, output, (observation_id,))

            blob_value = arguments.get("protocol_model_blob_id") or arguments.get("model_blob_id")
            model = await self._load_model_for_investigation(
                _uuid(blob_value), context.investigation_id
            )
            rendered = (
                self.service.render(model)
                if self.service is not None
                else render_protocol_model(model)
            )
            render_observation = await self.recorder.record_json(
                investigation_id=context.investigation_id,
                kind=EvidenceKind.OTHER,
                payload=rendered.to_dict(),
                source=NETWORK_PROTOCOL_RENDER,
                source_version="1",
                object_type="protocol_diagram_specs",
                object_id=rendered.canonical_sha256,
                limitations=(
                    "Specs sémantiques; le lot L7 projette ces données vers DiagramSpecV1/D2.",
                    "Aucun D2 libre fourni par le modèle n'est compilé.",
                ),
                idempotency_key=context.idempotency_key,
            )
            output = {
                **rendered.to_dict(),
                "protocol_model_blob_id": str(blob_value),
                "observation_ref": str(render_observation.id),
            }
            return self._result(
                context,
                started,
                ToolInvocationStatus.SUCCEEDED,
                output,
                (render_observation.id,),
            )
        except ProtocolModelNotInInvestigation:
            return self._result(
                context,
                started,
                ToolInvocationStatus.POLICY_DENIED,
                {"detail_code": "protocol_model_not_in_investigation"},
            )
        except HypothesisNotReplayableError as exc:
            return self._result(
                context,
                started,
                ToolInvocationStatus.POLICY_DENIED,
                {"detail_code": "hypothesis_not_replayable", "detail": str(exc)[:256]},
            )
        except NetworkCaptureRejected as exc:
            status = (
                ToolInvocationStatus.UNAVAILABLE
                if exc.reason.value == "blob_unavailable"
                else ToolInvocationStatus.POLICY_DENIED
            )
            return self._result(
                context,
                started,
                status,
                {"detail_code": exc.reason.value},
            )
        except ProtocolDocumentReferenceError as exc:
            return self._result(
                context,
                started,
                ToolInvocationStatus.POLICY_DENIED,
                {"detail_code": str(exc)[:128]},
            )
        except (ProtocolTraceError, ValueError, TypeError, KeyError) as exc:
            return self._result(
                context,
                started,
                ToolInvocationStatus.POLICY_DENIED,
                {"detail_code": "protocol_input_invalid", "detail": str(exc)[:256]},
            )

    async def _record(
        self,
        context: ToolExecutionContext,
        model: ProtocolModelV1,
        blob_id: UUID,
        stored_sha: str,
        summary: Mapping[str, Any],
    ) -> UUID:
        observation = await self.recorder.record_json(
            investigation_id=context.investigation_id,
            kind=EvidenceKind.OTHER,
            payload={
                "schema": "ProtocolModelV1",
                "model_blob_id": str(blob_id),
                "model_sha256": stored_sha,
                "model_canonical_sha256": model.canonical_sha256,
                "summary": dict(summary),
            },
            source=self.name,
            source_version="1",
            object_type="protocol_model",
            object_id=stored_sha,
            limitations=(
                "Le modèle complet est un blob JSON canonique immuable.",
                (
                    "Modèle documenté, non observé; aucune capture réseau n'est citée."
                    if model.validation_status is ProtocolValidationStatus.DOCUMENTED_NOT_OBSERVED
                    else "Les identifiants de capture pointent vers les blobs PCAP immuables."
                ),
            ),
            idempotency_key=context.idempotency_key,
        )
        return observation.id

    async def _load_model_for_investigation(
        self, blob_id: UUID, investigation_id: UUID
    ) -> ProtocolModelV1:
        model = await self.model_store.get_model(blob_id, investigation_id=investigation_id)
        if model.investigation_id != investigation_id:
            raise ProtocolModelNotInInvestigation
        return model

    def _result(
        self,
        context: ToolExecutionContext,
        started: float,
        status: ToolInvocationStatus,
        output: Mapping[str, Any],
        observation_refs: tuple[UUID, ...] = (),
    ) -> ToolResult:
        consumed = min(
            self.reserved_seconds,
            max(0, math.ceil(time.monotonic() - started)),
        )
        return ToolResult(
            status=status,
            observation_refs=observation_refs,
            budget_consumed={BudgetDimension.TOOL_SECONDS: consumed},
            output={**output, "status": status.value},
        )


def _operation_schema(
    description: str,
    *,
    properties: Mapping[str, Any],
    required: list[str],
) -> dict[str, Any]:
    return {
        "type": "object",
        "description": description,
        "properties": dict(properties),
        "required": required,
        "additionalProperties": False,
    }


def _trace_array_schema() -> dict[str, Any]:
    return {"type": "array", "minItems": 1, "maxItems": 32, "items": _TRACE_SCHEMA}


def _required_str(arguments: Mapping[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _uuid(value: object) -> UUID:
    if not isinstance(value, str):
        raise ValueError("UUID value must be a string")
    return UUID(value)


def _capture_ref(value: object) -> NetworkCaptureRefV1:
    if not isinstance(value, Mapping):
        raise ValueError("capture_ref must be an object")
    allowed = {"blob_id", "sha256", "size_bytes", "format"}
    if set(value) != allowed:
        raise ValueError("capture_ref has missing or unknown properties")
    size = value["size_bytes"]
    if isinstance(size, bool) or not isinstance(size, int):
        raise ValueError("capture size_bytes must be an integer")
    return NetworkCaptureRefV1(
        blob_id=_uuid(value["blob_id"]),
        sha256=_required_nested_str(value, "sha256"),
        size_bytes=size,
        format=NetworkCaptureFormat(_required_nested_str(value, "format")),
    )


def _trace_requests(value: object) -> tuple[ProtocolTraceRequestV1, ...]:
    if not isinstance(value, list) or not value:
        raise ValueError("traces must be a non-empty array")
    result: list[ProtocolTraceRequestV1] = []
    for item in value:
        if not isinstance(item, Mapping):
            raise ValueError("trace must be an object")
        allowed = {"capture_ref", "flow_id", "sample_sha256", "report_id", "holdout"}
        if set(item) != allowed:
            raise ValueError("trace has missing or unknown properties")
        sample_sha = item["sample_sha256"]
        if sample_sha is not None and not isinstance(sample_sha, str):
            raise ValueError("sample_sha256 must be a string or null")
        report_id = None if item["report_id"] is None else _uuid(item["report_id"])
        holdout = item["holdout"]
        if not isinstance(holdout, bool):
            raise ValueError("holdout must be a boolean")
        result.append(
            ProtocolTraceRequestV1(
                capture=_capture_ref(item["capture_ref"]),
                flow_id=_required_nested_str(item, "flow_id"),
                sample_sha256=sample_sha,
                report_id=report_id,
                holdout=holdout,
            )
        )
    return tuple(result)


def _required_nested_str(value: Mapping[str, Any], key: str) -> str:
    field = value.get(key)
    if not isinstance(field, str) or not field.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return field
