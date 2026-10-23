from __future__ import annotations

import base64
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from cti_app.application.advisor_contracts import (
    ADVISOR_RESPONSE_SCHEMA_V3,
    extract_json_object,
    parse_typed_advisor_response,
)
from cti_app.application.bridge_investigator import (
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.content_pivot_candidates import verify_content_pattern_candidates
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_RESPONSE_SCHEMA,
    MAX_ADVISOR_INPUT_BYTES,
    MAX_ADVISOR_TOOL_SECONDS,
    InvestigationAdvisorTool,
    _advisor_dossier_opinion_source_refs,
    _marked_secret_field_survives,
    _parse_advisor_output,
    _redact_advisor_payload,
    _safe_text,
    _truncate_context_text,
    register_investigation_advisor_tool,
)
from cti_app.application.investigation_tools import ToolExecutionContext, ToolResult
from cti_app.domain.analyst_opinion import OpinionBlockType
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationFinding,
    ToolInvocationStatus,
)


class Transport:
    def __init__(self, submission):
        self.submission = submission
        self.calls = []

    async def submit(self, **kwargs):
        self.calls.append(kwargs)
        return self.submission(kwargs) if callable(self.submission) else self.submission


class ObservationRepository:
    def __init__(self, observations):
        self.observations = {item.id: item for item in observations}

    async def get(self, observation_id):
        return self.observations.get(observation_id)

    async def list_for_investigation(self, investigation_id):
        return tuple(
            item for item in self.observations.values() if item.investigation_id == investigation_id
        )

    async def append_if_absent(self, observation):
        self.observations.setdefault(observation.id, observation)
        return self.observations[observation.id]


class FindingRepository:
    def __init__(self, findings: tuple[InvestigationFinding, ...] = ()) -> None:
        self.findings = {item.id: item for item in findings}

    async def get(self, finding_id: UUID) -> InvestigationFinding | None:
        return self.findings.get(finding_id)

    async def list_for_investigation(
        self, investigation_id: UUID
    ) -> tuple[InvestigationFinding, ...]:
        return tuple(
            item for item in self.findings.values() if item.investigation_id == investigation_id
        )


class ArtifactStore:
    def __init__(self, values):
        self.values = values

    async def read_bytes(self, blob_id, *, max_bytes):
        value = self.values[blob_id]
        assert len(value) <= max_bytes
        return value

    async def put_canonical_json(self, payload, *, bucket):
        raw = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode()
        blob_id = uuid4()
        self.values[blob_id] = raw
        return blob_id, hashlib.sha256(raw).hexdigest()

    async def put_text(self, value, *, bucket):
        blob_id = uuid4()
        self.values[blob_id] = value.encode("utf-8")
        return blob_id

    async def put_bytes(self, value, *, bucket, mime_type):
        blob_id = uuid4()
        self.values[blob_id] = bytes(value)
        return blob_id


class BlobRepository:
    def __init__(self) -> None:
        self.descriptors = {}

    def add(self, blob_id, *, sha256, size, logical_bucket="samples-quarantine"):
        self.descriptors[blob_id] = SimpleNamespace(
            descriptor=SimpleNamespace(
                sha256=sha256,
                size=size,
                mime_type="application/octet-stream",
                logical_bucket=logical_bucket,
            )
        )

    async def get(self, blob_id):
        return self.descriptors.get(blob_id)


class Registry:
    def __init__(self, definitions=()):
        self.definitions = tuple(definitions)

    def list_registered(self):
        return self.definitions

    def list_available(self, context):
        return tuple(
            item
            for item in self.definitions
            if item.capability_key in context.available_capabilities
        )


def make_observation(investigation_id, payload):
    raw = json.dumps(payload, separators=(",", ":")).encode()
    return (
        EvidenceObservation(
            investigation_id=investigation_id,
            kind=EvidenceKind.BINARY_STRING,
            blob_id=uuid4(),
            sha256=hashlib.sha256(raw).hexdigest(),
            source="sample.extract_obfuscated_strings",
            source_version="floss-3.1",
            observed_at=datetime.now(UTC),
            object_type="sample",
            object_id=payload.get("sample_sha256", "sample"),
        ),
        raw,
    )


def make_tool(*, investigation_id, transport, observation=None, raw=None, allowed=True):
    class _Hypothesis:
        def to_payload(self):
            return {
                "id": "H_SYNTHETIC",
                "statement": "Synthetic hypothesis for offline adviser tests.",
                "status": "open",
                "evidence_refs": [],
                "finding_refs": [],
            }

    working_state = SimpleNamespace(
        investigation_id=investigation_id,
        version=1,
        sha256="f" * 64,
        hypotheses=(_Hypothesis(),),
    )
    subject_id = uuid4()
    sample_blob_id = uuid4()
    snapshot = SimpleNamespace(
        external_llm_allowed=allowed,
        do_not_submit=not allowed,
        tlp="clear",
        synthesis_sha256="a" * 64,
        article=SimpleNamespace(short_summary="A bounded summary.", open_questions=("Q1",)),
        article_passages=(
            SimpleNamespace(
                claim_id="S1P1",
                heading="Résumé",
                text="S1P1. The article describes Blackwood behavior and evidence markers.",
            ),
        ),
    )
    blobs = BlobRepository()
    if observation is not None and raw is not None:
        blobs.add(
            observation.blob_id,
            sha256=observation.sha256,
            size=len(raw),
            logical_bucket="evidence",
        )
    tool = InvestigationAdvisorTool(
        transport=transport,
        settings=SimpleNamespace(
            openai_bridge_min_submission_interval_seconds=0,
            openai_bridge_submission_jitter_fraction=0,
            operator_advisor_checkpoint_tool_threshold=12,
        ),
        investigations=SimpleNamespace(get=lambda _id: None),
        observations=ObservationRepository((observation,) if observation else ()),
        findings=FindingRepository(),
        investigation_states=SimpleNamespace(get=async_get(working_state)),
        opinions=SimpleNamespace(),
        sample_associations=SimpleNamespace(),
        samples=SimpleNamespace(),
        blobs=blobs,
        article_snapshots=SimpleNamespace(resolve=lambda _id: snapshot),
        artifact_store=ArtifactStore({observation.blob_id: raw} if observation else {}),
        registry=Registry(),
    )
    tool._investigations.get = async_get(
        SimpleNamespace(subject_id=subject_id, synthesis_artifact_id=uuid4())
    )
    tool._article_snapshots.resolve = async_get(snapshot)
    return tool, sample_blob_id


def async_get(value):
    async def get(_key):
        return value

    return get


def make_context(investigation_id, key="operator:advisor:test", capabilities=()):
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key=key,
        available_capabilities=frozenset(capabilities),
    )


def advisor_response(summary, basis_refs, *, recommendations=()):
    return json.dumps(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA,
            "summary": summary,
            "alternative_hypotheses": [
                {"statement": "Une autre explication reste possible.", "basis_refs": basis_refs}
            ],
            "discriminating_experiment": {
                "question": "Quel résultat distingue les hypothèses ?",
                "tool": None,
                "schema_version": None,
                "expected_signal": "Un signal mesuré distinct.",
                "basis_refs": basis_refs,
            },
            "discarded_lead": {
                "lead": "La piste moins discriminante.",
                "reason": "Elle ne sépare pas les hypothèses.",
                "basis_refs": basis_refs,
            },
            "recommended_next_tools": list(recommendations),
            "risks": [],
            "questions": [],
            "untrusted": True,
        },
        ensure_ascii=False,
    )


PERSISTED_RESPONSE_SHAPES = json.loads(
    (
        Path(__file__).parent / "fixtures" / "advisor_bridge_persisted_response_shapes.json"
    ).read_text()
)


def projected_persisted_response(invocation_id: str, allowed_refs: tuple[UUID, ...]) -> str:
    """Rebuild a redacted field-shape fixture captured from a persisted response."""
    shape = PERSISTED_RESPONSE_SHAPES[invocation_id]
    if "raw_text" in shape:
        raw_text = shape["raw_text"]
        if not isinstance(raw_text, str):
            raise ValueError("Persisted response fixture raw_text must be a string")
        return raw_text
    refs: dict[str, str] = {}

    def resolve(value: str) -> str:
        if value.startswith("@o"):
            return str(allowed_refs[int(value[2:]) - 1])
        return refs.setdefault(value, str(uuid4()))

    base_ref = str(allowed_refs[0]) if allowed_refs else str(uuid4())
    payload = json.loads(advisor_response("Redacted persisted response fixture.", [base_ref]))
    payload["alternative_hypotheses"] = [
        {
            "statement": f"Redacted persisted alternative {index + 1}.",
            "basis_refs": [resolve(ref) for ref in basis_refs],
        }
        for index, basis_refs in enumerate(shape["alternative_refs"])
    ]
    payload["discriminating_experiment"]["basis_refs"] = [
        resolve(ref) for ref in shape["experiment_refs"]
    ]
    payload["discarded_lead"]["basis_refs"] = [resolve(ref) for ref in shape["discarded_refs"]]
    payload["recommended_next_tools"] = [
        {
            "tool": "sample.analyze",
            "schema_version": "1",
            "why": "Redacted persisted recommendation.",
            "args_hint": "redacted non-object args_hint" if hint_type == "string" else {},
            "expected_signal": "Redacted measured signal.",
            "basis_refs": [resolve(ref) for ref in basis_refs],
        }
        for basis_refs, hint_type in zip(
            shape["recommendation_refs"], shape["recommendation_args_hint"], strict=True
        )
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def parse_projected_persisted_response(invocation_id: str) -> tuple[dict, set[str]]:
    shape = PERSISTED_RESPONSE_SHAPES[invocation_id]
    reference_lists = [
        *shape.get("alternative_refs", ()),
        shape.get("experiment_refs", ()),
        shape.get("discarded_refs", ()),
        *shape.get("recommendation_refs", ()),
    ]
    allowed_count = max(
        (int(ref[2:]) for refs in reference_lists for ref in refs if ref.startswith("@o")),
        default=0,
    )
    allowed_refs = tuple(uuid4() for _ in range(allowed_count))
    rejection: dict = {}
    parsed = _parse_advisor_output(
        projected_persisted_response(invocation_id, allowed_refs),
        prompt="bounded prompt",
        available_tools={"sample.analyze": "1"},
        available_tool_schemas={"sample.analyze": {"type": "object", "properties": {}}},
        evidence_refs=allowed_refs,
        source_refs=(),
        consultation_type="review_text"
        if invocation_id
        in {
            "ca78b757-988d-4bb4-a622-ed5a2cb6deaf",
            "f125ff59-bff6-4ec4-8879-43aad4a2a83a",
        }
        else "pivot_strategy",
        validation_rejection=rejection,
    )
    assert parsed is not None
    return parsed, {str(ref) for ref in allowed_refs}


def test_advisor_response_json_extraction_is_single_block_and_never_repairs() -> None:
    assert extract_json_object('Bridge output:\n```json\n{"ok":true}\n```\nDone') == {"ok": True}
    assert extract_json_object('```\n{"ok":true}\n```') == {"ok": True}
    assert extract_json_object('{"x":1,"x":2}') is None
    assert extract_json_object('```json\n{"ok":true}\n```\n```json\n{"ok":false}\n```') is None
    assert extract_json_object('Here is {"ok":true}') == {"ok": True}
    assert extract_json_object('{"value":NaN}') is None


def test_advisor_timeout_budgets_allow_slow_bridge_responses() -> None:
    assert MAX_ADVISOR_TOOL_SECONDS >= 200


def test_advisor_schema_has_no_arbitrary_editorial_or_measurement_ceilings() -> None:
    investigation_id = uuid4()
    tool, _ = make_tool(investigation_id=investigation_id, transport=Transport(None))

    class CaptureRegistry:
        def __init__(self):
            self.definitions = []

        def register(self, definition, adapter):
            self.definitions.append(definition)

    registry = CaptureRegistry()
    register_investigation_advisor_tool(registry, adapter=tool)
    schema = registry.definitions[0].input_schema["properties"]

    assert "maxLength" not in schema["question"]
    assert "maxItems" not in schema["units"]
    assert "maxItems" not in schema["measurements"]
    assert "maxItems" not in schema["evidence_refs"]


def test_verified_advisor_measurements_accept_more_than_sixty_four_items() -> None:
    tool, _ = make_tool(investigation_id=uuid4(), transport=Transport(None))
    blob_id = str(uuid4())
    verified = [
        {
            "blob_id": blob_id,
            "kind": "string",
            "offset": index,
            "value": f"distinct-observation-{index}",
        }
        for index in range(65)
    ]

    selected = tool._select_verified_measurements(
        verified,
        [dict(item) for item in verified],
    )

    assert len(selected) == 65


@pytest.mark.parametrize(
    ("invocation_id", "expected_alternatives", "expected_recommendations", "usable"),
    [
        ("ca78b757-988d-4bb4-a622-ed5a2cb6deaf", 2, 2, True),
        ("d595141f-a80b-44e0-b265-53b90f97eebc", 2, 5, True),
        ("f125ff59-bff6-4ec4-8879-43aad4a2a83a", 0, 4, True),
        ("ab1851f2-15fd-4e11-a474-8721bf001aa6", 1, 3, True),
    ],
)
def test_redacted_persisted_bridge_response_fixtures_are_salvaged_explicitly(
    invocation_id, expected_alternatives, expected_recommendations, usable
) -> None:
    parsed, allowed_refs = parse_projected_persisted_response(invocation_id)

    assert parsed["status"] == "partial"
    assert parsed["normalization_status"] == "partial"
    assert parsed["warnings"]
    assert parsed["usable"] is usable
    assert len(parsed["alternative_hypotheses"]) == expected_alternatives
    assert len(parsed["recommended_next_tools"]) == expected_recommendations
    refs = [
        *(ref for item in parsed["alternative_hypotheses"] for ref in item["basis_refs"]),
        *parsed["discriminating_experiment"]["basis_refs"],
        *parsed["discarded_lead"]["basis_refs"],
        *(ref for item in parsed["recommended_next_tools"] for ref in item["basis_refs"]),
    ]
    assert set(refs) <= allowed_refs
    if invocation_id == "f125ff59-bff6-4ec4-8879-43aad4a2a83a":
        assert all(item["args_hint"] == {} for item in parsed["recommended_next_tools"])
        assert all(item["args_hint_incomplete"] for item in parsed["recommended_next_tools"])


def typed_prompt_payload(prompt):
    import re

    result = {
        "text": prompt,
        "units": [],
        "assertions": [],
        "evidence": [],
        "findings": [],
        "measurements": [],
        "measured_facts": [],
        "wire_ref_catalog": [],
        "wire_measurement_catalog": [],
        "wire_fact_catalog": [],
    }
    in_target = False
    current_unit = None
    lines = prompt.splitlines()
    for line in lines:
        if line == "TEXTE CIBLE:":
            in_target = True
            continue
        if line == "DOSSIER DE FAITS":
            in_target = False
        if in_target:
            match = re.match(r"^(U[1-9][0-9]*)(?:\s+—\s+(.*))?:$", line)
            if match:
                current_unit = {
                    "id": match.group(1),
                    "unit_id": match.group(1),
                    "block_type": match.group(2) or "",
                    "text": "",
                    "assertions": [],
                }
                result["units"].append(current_unit)
                continue
            if current_unit is not None and line.startswith("| "):
                current_unit["text"] += ("\n" if current_unit["text"] else "") + line[2:]
            assertion = re.match(r"^CIBLE \[(A[1-9][0-9]*)\]:\s*(.*)$", line)
            if assertion:
                row = {
                    "id": assertion.group(1),
                    "unit_id": current_unit["id"] if current_unit else "",
                    "text": assertion.group(2),
                }
                result["assertions"].append(row)
                if current_unit is not None:
                    current_unit["assertions"].append(row)

    for index, line in enumerate(lines):
        match = re.match(r"^\[(E[1-9][0-9]*)\]\s+(.+)$", line)
        if not match:
            continue
        alias, label = match.groups()
        body_lines = []
        for candidate in lines[index + 1 :]:
            if re.match(r"^\[E[1-9][0-9]*\]", candidate):
                break
            if candidate and not candidate.startswith("| ") and candidate != "|":
                break
            body_lines.append(candidate[2:] if candidate.startswith("| ") else "")
        body = "\n".join(body_lines).strip()
        if label.casefold().startswith("mesure"):
            value_match = re.search(r"(?:offset\s+[0-9]+\s*:\s*|:\s*)(.*)$", body, re.I)
            kind_match = re.search(r"Mesure —\s*([^,]+)", label, re.I)
            measurement = {
                "alias": alias,
                "measurement_id": alias,
                "value": value_match.group(1) if value_match else body,
                "kind": kind_match.group(1) if kind_match else label,
                "evidence_id": alias,
            }
            offset = re.search(r"offset\s+([0-9]+)", label + " " + body, re.I)
            if offset:
                measurement["offset"] = int(offset.group(1))
            result["measurements"].append(measurement)
            result["wire_measurement_catalog"].append(measurement)
            kind = "measurement"
        elif label.casefold().startswith("fait réseau"):
            result["wire_fact_catalog"].append({"fact": alias})
            kind = "fact"
        elif label.casefold().startswith("constat"):
            result["findings"].append({"finding_id": alias, "line": body})
            kind = "finding"
        elif label.casefold().startswith("article"):
            kind = "article"
        elif label.casefold().startswith("avis"):
            kind = "opinion"
        else:
            result["evidence"].append({"evidence_id": alias, "source": label, "content": body})
            kind = "observation"
        result["wire_ref_catalog"].append({"ref": alias, "kind": kind})
    result["review_text"] = {
        "evidence_refs": [item["evidence_id"] for item in result["evidence"]],
        "opinion_id": next(
            (item["ref"] for item in result["wire_ref_catalog"] if item["kind"] == "opinion"),
            "",
        ),
    }
    for unit in result["units"]:
        unit["evidence_refs"] = [item["evidence_id"] for item in result["evidence"]]
        unit["finding_refs"] = [item["finding_id"] for item in result["findings"]]
        unit["assertions"] = [
            item for item in result["assertions"] if item["unit_id"] == unit["id"]
        ]
    for line in lines:
        match = re.search(r"Mesure observée:\s*([^,]+)(?:, offset\s+([0-9]+))?:\s*(.*)", line, re.I)
        if match:
            row = {"kind": match.group(1), "value": match.group(3)}
            if match.group(2):
                row["offset"] = int(match.group(2))
            result["measured_facts"].append(row)
    return result


def typed_yara_response(prompt, *, rule_source):
    measurements = typed_prompt_payload(prompt)["measurements"]
    aliases = ",".join(item["alias"] for item in measurements)
    return (
        "advisor-wire.v1\nTYPE: yara_draft\nSTATUS: succeeded\n"
        f"```yara\n{rule_source}\n```\nMOTIF: $marker | measurements={aliases}\n"
        "INVARIANT: Le motif provient d'une mesure vérifiée.\nLIMITATION: none"
    )


@pytest.mark.parametrize(
    ("hint_kind", "expected_hint", "normalizations", "expected_incomplete"),
    [
        pytest.param(
            "string",
            {},
            ["$.recommended_next_tools[0].args_hint:non_object_to_empty_object"],
            True,
            id="string-normalized-incomplete",
        ),
        pytest.param(
            "null",
            {},
            ["$.recommended_next_tools[0].args_hint:null_to_empty_object"],
            True,
            id="null-normalized",
        ),
        pytest.param(
            "absent",
            {},
            ["$.recommended_next_tools[0].args_hint:absent_to_empty_object"],
            True,
            id="absent-normalized",
        ),
        pytest.param(
            "object",
            {"sample_sha256": "c" * 64},
            [
                "$.recommended_next_tools[0].args_hint:unknown_property_removed:nested",
                "$.recommended_next_tools[0].args_hint:unknown_property_removed:list",
            ],
            False,
            id="object-kept-bounded",
        ),
    ],
)
@pytest.mark.asyncio
async def test_args_hint_contract_is_bounded_and_normalization_is_recorded(
    hint_kind, expected_hint, normalizations, expected_incomplete
):
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "c" * 64,
            "strings": [{"value": "MeasuredSampleMarker", "offset": 128}],
        },
    )
    recommendation = {
        "tool": "sample.analyze",
        "schema_version": "1",
        "why": "Compare the measured sample feature.",
        "expected_signal": "The second sample has a matching feature.",
        "basis_refs": [str(observation.id)],
    }
    if hint_kind == "string":
        recommendation["args_hint"] = "--sample-sha256 " + "c" * 64
    elif hint_kind == "null":
        recommendation["args_hint"] = None
    elif hint_kind == "object":
        recommendation["args_hint"] = {
            "sample_sha256": "c" * 64,
            "nested": {"unexpected": "dropped"},
            "list": ["dropped"],
        }

    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "Compare the measured evidence.",
                [str(observation.id)],
                recommendations=(recommendation,),
            ),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
                input_schema={
                    "type": "object",
                    "properties": {"sample_sha256": {"type": "string", "minLength": 64}},
                    "required": ["sample_sha256"],
                    "additionalProperties": False,
                },
            ),
        )
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, capabilities=("local.sample_analysis",)),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert "advisor-wire.v1" in prompt
    assert "FAITS TRANSMIS" in prompt
    assert "required_arguments" not in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult" and item.id in result.observation_refs
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    recommendation = result.output["recommended_next_tools"][0]
    stored_recommendation = persisted["output"]["recommended_next_tools"][0]
    assert recommendation["args_hint"] == expected_hint
    assert stored_recommendation["args_hint"] == expected_hint
    if expected_incomplete:
        assert recommendation["args_hint_incomplete"] is True
        assert stored_recommendation["args_hint_incomplete"] is True
    else:
        assert "args_hint_incomplete" not in recommendation
        assert "args_hint_incomplete" not in stored_recommendation
    assert list(result.output["normalizations"]) == normalizations
    assert persisted["output"]["normalizations"] == normalizations


@pytest.mark.parametrize(
    ("args_hint", "expected_hint", "expected_incomplete"),
    [
        pytest.param(
            {"observation_uuid": "00000000-0000-0000-0000-000000000001"},
            {},
            True,
            id="missing-required-fields",
        ),
        pytest.param(
            {
                "object_type": "sample",
                "object_id": "sample-1",
                "observation_uuid": "00000000-0000-0000-0000-000000000001",
            },
            {"object_type": "sample", "object_id": "sample-1"},
            False,
            id="unknown-field-removed-valid-hint",
        ),
    ],
)
@pytest.mark.asyncio
async def test_args_hint_is_normalized_against_recommended_tool_schema(
    args_hint, expected_hint, expected_incomplete
):
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "c" * 64,
            "strings": [{"value": "MeasuredSampleMarker", "offset": 128}],
        },
    )
    recommendation = {
        "tool": "graph.neighbors",
        "schema_version": "1",
        "why": "Inspect the measured relationship.",
        "args_hint": args_hint,
        "expected_signal": "A related object is returned.",
        "basis_refs": [str(observation.id)],
    }
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "Check the measured relationship.",
                [str(observation.id)],
                recommendations=(recommendation,),
            ),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="graph.neighbors",
                schema_version="1",
                capability_key="graph.read",
                input_schema={
                    "type": "object",
                    "properties": {
                        "object_type": {"type": "string", "enum": ["sample", "observation"]},
                        "object_id": {"type": "string", "minLength": 1, "maxLength": 128},
                    },
                    "required": ["object_type", "object_id"],
                    "additionalProperties": False,
                },
            ),
        )
    )

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, capabilities=("graph.read",)),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    prompt = transport.calls[0]["prompt"]
    assert "required_arguments" not in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult" and item.id in result.observation_refs
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    recommendation = result.output["recommended_next_tools"][0]
    stored_recommendation = persisted["output"]["recommended_next_tools"][0]
    assert recommendation["args_hint"] == expected_hint
    assert stored_recommendation["args_hint"] == expected_hint
    if expected_incomplete:
        assert recommendation["args_hint_incomplete"] is True
        assert stored_recommendation["args_hint_incomplete"] is True
    else:
        assert "args_hint_incomplete" not in recommendation
        assert "args_hint_incomplete" not in stored_recommendation
    assert list(result.output["normalizations"]) == [
        "$.recommended_next_tools[0].args_hint:unknown_property_removed:observation_uuid"
    ]
    assert persisted["output"]["normalizations"] == list(result.output["normalizations"])


@pytest.mark.asyncio
async def test_yara_draft_sends_only_verified_offsets_and_returns_untrusted_text() -> None:
    investigation_id = uuid4()
    sample_sha256 = "b" * 64
    measured = {
        "schema": "sample-obfuscated-strings.v1",
        "sample_sha256": sample_sha256,
        "strings": [
            {"value": "CreateMutexW", "offset": 0x80, "function": "entry"},
            {"value": "ignored-without-offset", "offset": None},
        ],
        "hex": [{"value": "41424344", "offset": 0x90}],
        "wide": [{"value": "WideMarker", "offset": 0x98}],
        "base64": [{"value": "QmFzZTY0TWVhc3VyZQ==", "offset": 0xA0}],
    }
    observation, raw = make_observation(investigation_id, measured)
    rule_source = 'rule Candidate { strings: $mutex = "CreateMutexW" condition: $mutex }'
    transport = Transport(
        lambda call: TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=typed_yara_response(call["prompt"], rule_source=rule_source),
            bridge_invocation_id=str(uuid4()),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    unrelated, unrelated_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "9" * 64,
            "strings": [{"value": "UnrelatedTelemetryMarker", "offset": 512}],
        },
    )
    tool._observations.observations[unrelated.id] = unrelated
    tool._artifact_store.values[unrelated.blob_id] = unrelated_raw
    tool._blobs.add(
        unrelated.blob_id,
        sha256=unrelated.sha256,
        size=len(unrelated_raw),
        logical_bucket="evidence",
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
            ),
        )
    )
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256),)
    )
    tool._samples.list_for_subject = async_get(
        (
            SimpleNamespace(
                expected_hash=sample_sha256,
                source_object_id=None,
                state="quarantined",
                do_not_submit=True,
                external_llm_allowed=False,
                blob_id=sample_blob_id,
            ),
        )
    )
    tool._blobs.add(sample_blob_id, sha256=sample_sha256, size=4096)

    invalid = await tool.execute(
        {
            "consultation_type": "yara_draft",
            "evidence_refs": [str(observation.id)],
            "measurements": [
                {
                    "blob_id": str(sample_blob_id),
                    "kind": "hex",
                    "offset": 0x90,
                    "value": "4142434",
                }
            ],
        },
        make_context(investigation_id, key="operator:advisor:invalid-measurement"),
    )
    assert invalid.status is ToolInvocationStatus.FAILED
    assert invalid.error_code == "advisor_input_invalid"
    assert invalid.output["advisor_status"] == "invalid_input"
    assert invalid.output["input_error_code"] == "measurements_invalid"
    assert [dict(item) for item in invalid.output["measurement_errors"]] == [
        {"index": 0, "reason": "measurement_not_verified"}
    ]
    assert invalid.output["failure_stage"] == "pre_transport_validation"
    assert invalid.output["confirmed_non_execution"] is True
    assert invalid.output["provider_usage"] == {"status": "not_submitted"}
    assert transport.calls == []

    result = await tool.execute(
        {
            "consultation_type": "yara_draft",
            "evidence_refs": [str(observation.id)],
            "measurements": [
                {
                    "blob_id": str(sample_blob_id),
                    "kind": "string",
                    "offset": 0x80,
                    "value": "CreateMutexW",
                },
                {
                    "blob_id": str(sample_blob_id),
                    "kind": "hex",
                    "offset": 0x90,
                    "value": "41424344",
                },
                {
                    "blob_id": str(sample_blob_id),
                    "kind": "wide",
                    "offset": 0x98,
                    "value": "WideMarker",
                },
                {
                    "blob_id": str(sample_blob_id),
                    "kind": "base64",
                    "offset": 0xA0,
                    "value": "QmFzZTY0TWVhc3VyZQ==",
                },
            ],
        },
        make_context(investigation_id),
    )

    prompt = transport.calls[0]["prompt"]
    assert result.status is ToolInvocationStatus.SUCCEEDED, dict(result.output)
    assert str(sample_blob_id) not in prompt
    prompt_payload = typed_prompt_payload(prompt)
    assert "blob_id" not in prompt_payload["measurements"][0]
    assert prompt_payload["measurements"][0]["measurement_id"] == "E1"
    assert "offset 128:" in prompt
    assert {item["kind"] for item in typed_prompt_payload(prompt)["measurements"]} == {
        "string",
        "hex",
        "wide",
        "base64",
    }
    assert "ignored-without-offset" not in prompt
    assert "UNTRUSTED ADVISORY" in result.output["advisory_text"]
    assert result.output["untrusted"] is True
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    advisor_observation = [
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    ][-1]
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    assert persisted["schema"] == "operator-advisor-call.v1"
    assert persisted["input_observation_refs"] == [str(observation.id)]
    assert persisted["output"].get("schema") == ADVISOR_RESPONSE_SCHEMA_V3, persisted
    assert persisted["output"]["rule_source"].rstrip("\n") == rule_source
    assert {
        item["measurement_id"] for item in persisted["output"]["motifs"][0]["measurement_refs"]
    } == {"m001", "m002", "m003", "m004"}
    assert persisted["bridge_invocation_id_kind"] == "conversation_turn"
    assert persisted["bridge_invocation_id"]
    assert len(persisted["input_sha256"]) == 64
    assert persisted["input_refs"]
    assert persisted["output_sha256"] == result.output["output_sha256"]
    assert persisted["provider_usage"] == {"status": "usage_unavailable"}
    assert persisted["cost"]["bridge_turns"] == 1
    assert persisted["cost"]["context_bytes"] > 0
    assert persisted["cost"]["context_units_estimated_from_bytes"] > 0
    assert persisted["context_bytes"] == persisted["context_estimate"]["context_bytes"]
    assert persisted["context_estimate"]["status"] == "estimated"
    assert result.output["bridge_provenance"]["provider_usage"] == {"status": "usage_unavailable"}


@pytest.mark.asyncio
async def test_plan_investigation_selects_relevant_content_from_six_refs() -> None:
    investigation_id = uuid4()
    observations = []
    raw_by_blob = {}
    for index in range(6):
        observation, raw = make_observation(
            investigation_id,
            {
                "schema": "sample-obfuscated-strings.v1",
                "sample_sha256": f"{index + 1:064x}",
                "strings": [
                    {"value": f"CandidateLiteral{row}", "offset": 0x100 + row} for row in range(8)
                ],
            },
        )
        observations.append(observation)
        raw_by_blob[observation.blob_id] = raw
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("Bounded next steps.", [str(observations[0].id)]),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observations[0],
        raw=raw_by_blob[observations[0].blob_id],
    )
    tool._observations = ObservationRepository(observations)
    tool._artifact_store = ArtifactStore(raw_by_blob)
    for item in observations:
        item_raw = raw_by_blob[item.blob_id]
        tool._blobs.add(
            item.blob_id,
            sha256=item.sha256,
            size=len(item_raw),
            logical_bucket="evidence",
        )
    hashes = tuple(f"{index + 1:064x}" for index in range(6))
    sample_blobs = {sample_hash: uuid4() for sample_hash in hashes}
    tool._sample_associations.list_for_investigation = async_get(
        tuple(
            SimpleNamespace(
                id=uuid4(),
                sample_sha256=sample_hash,
                qualification=SimpleNamespace(value="candidate"),
            )
            for sample_hash in hashes
        )
    )
    tool._samples.list_for_subject = async_get(
        tuple(
            SimpleNamespace(
                expected_hash=sample_hash,
                source_object_id=None,
                state="quarantined",
                blob_id=blob_id,
            )
            for sample_hash, blob_id in sample_blobs.items()
        )
    )

    for sample_hash, blob_id in sample_blobs.items():
        tool._blobs.add(blob_id, sha256=sample_hash, size=4096)
    tool._findings = SimpleNamespace(
        list_for_investigation=async_get(
            tuple(
                SimpleNamespace(
                    id=uuid4(),
                    version=1,
                    kind=SimpleNamespace(value="network_behaviour"),
                    assessment=SimpleNamespace(value="inconclusive"),
                    statement=(f"Measured finding {index}: " + "x" * 900),
                    observation_refs=(observations[index].id,),
                    claim_refs=(),
                    limitations=("bounded",) * 8,
                )
                for index in range(6)
            )
        )
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(item.id) for item in observations],
        },
        make_context(investigation_id, key="operator:advisor:six-refs"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    prompt = transport.calls[0]["prompt"]
    assert all(str(item.id) not in prompt for item in observations)
    reference_catalog = typed_prompt_payload(prompt)["wire_ref_catalog"]
    assert all(
        {"ref": f"E{index}", "kind": "observation"} in reference_catalog
        for index in range(1, len(observations) + 1)
    )
    assert "CandidateLiteral0" in prompt
    assert "Measured finding" not in prompt
    assert "x" * 500 not in prompt


@pytest.mark.asyncio
async def test_minimal_plan_assembles_evidence_and_parses_structured_output() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "c" * 64,
            "strings": [{"value": "CreateMutexW", "offset": 128, "encoding": "ascii"}],
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "Comparer la constante mesurée au comportement observé.",
                [str(observation.id)],
                recommendations=(
                    {
                        "tool": "sample.analyze",
                        "schema_version": "1",
                        "why": "Vérifier les moteurs statiques manquants.",
                        "args_hint": {"sample_sha256": "c" * 64},
                        "expected_signal": "La même capacité apparaît dans une deuxième mesure.",
                        "basis_refs": [str(observation.id)],
                    },
                ),
            ),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    unrelated, unrelated_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "9" * 64,
            "strings": [{"value": "UnrelatedFindingObservationMarker", "offset": 512}],
        },
    )
    tool._observations.observations[unrelated.id] = unrelated
    tool._artifact_store.values[unrelated.blob_id] = unrelated_raw
    tool._blobs.add(
        unrelated.blob_id,
        sha256=unrelated.sha256,
        size=len(unrelated_raw),
        logical_bucket="evidence",
    )
    unrelated_finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.INCONCLUSIVE,
        statement="Une activité sans rapport reste à qualifier.",
        observation_refs=(unrelated.id,),
    )
    tool._findings = FindingRepository((unrelated_finding,))
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
            ),
        )
    )
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256="c" * 64, id=uuid4()),)
    )
    tool._samples.list_for_subject = async_get(
        (
            SimpleNamespace(
                id=uuid4(),
                expected_hash="c" * 64,
                source_object_id=None,
                state="quarantined",
                blob_id=sample_blob_id,
                size_bytes=4096,
            ),
        )
    )
    tool._blobs.add(sample_blob_id, sha256="c" * 64, size=4096)

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Plan autour de CreateMutexW.",
        },
        make_context(
            investigation_id,
            key="operator:advisor:minimal-plan",
            capabilities=("local.sample_analysis",),
        ),
    )

    prompt = transport.calls[0]["prompt"]
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert str(observation.id) not in prompt
    assert str(sample_blob_id) not in prompt
    assert {"ref": "E1", "kind": "observation"} in typed_prompt_payload(prompt)["wire_ref_catalog"]
    assert "UnrelatedTelemetryMarker" not in prompt
    assert "evidence_recorded" not in prompt
    assert "Plan autour de CreateMutexW." in prompt
    assert str(unrelated.id) not in prompt
    assert result.output["summary"].startswith("Comparer")
    assert result.output["recommended_next_tools"][0]["tool"] == "sample.analyze"
    assert result.output["recommended_next_tools"][0]["schema_version"] == "1"
    assert result.output["recommended_next_tools"][0]["basis_observation_refs"] == (
        str(observation.id),
    )
    assert result.output["untrusted"] is True
    advisor_observation = [
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    ][-1]
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    unrelated_status = next(
        item
        for item in persisted["completeness_manifest"]
        if item["kind"] == "observation" and item["id"] == str(unrelated.id)
    )
    assert "REQUIRED" not in {row["status"] for row in unrelated_status["statuses"]}
    assert "INTENTIONALLY_OMITTED" in {row["status"] for row in unrelated_status["statuses"]}


@pytest.mark.asyncio
async def test_pivot_strategy_persists_only_observation_validated_recommendations() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "e" * 64,
            "strings": [{"value": "BeaconPath", "offset": 256}],
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "La fonction mesurée fournit un pivot vérifiable.",
                [str(observation.id)],
                recommendations=(
                    {
                        "tool": "sample.analyze",
                        "schema_version": "1",
                        "why": "Comparer les fonctions à une autre mesure.",
                        "args_hint": {"sample_sha256": "e" * 64},
                        "expected_signal": (
                            "La même fonction est mesurée dans le second échantillon."
                        ),
                        "basis_refs": [str(observation.id)],
                    },
                ),
            ),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
            ),
        )
    )

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, capabilities=("local.sample_analysis",)),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    suggestion = result.output["recommended_next_tools"][0]
    assert suggestion["tool"] == "sample.analyze"
    assert suggestion["schema_version"] == "1"
    assert suggestion["basis_observation_refs"] == (str(observation.id),)
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["input_observation_refs"] == [str(observation.id)]
    persisted_suggestion = persisted["output"]["recommended_next_tools"][0]
    assert persisted_suggestion["tool"] == "sample.analyze"
    assert persisted_suggestion["basis_observation_refs"] == [str(observation.id)]
    assert persisted["output"]["alternative_hypotheses"][0]["basis_refs"] == [str(observation.id)]
    assert persisted["output"]["discarded_lead"]["basis_refs"] == [str(observation.id)]


@pytest.mark.asyncio
async def test_pivot_strategy_rejects_a_basis_reference_absent_from_observations() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "f" * 64,
            "strings": [{"value": "SyntheticPivotEvidence", "offset": 64}],
        },
    )
    unknown_ref = str(uuid4())
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("Unsupported pivot.", [unknown_ref]),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert result.output["usable"] is False
    assert result.output["alternative_hypotheses"] == ()
    assert result.output["recommended_next_tools"] == ()
    assert result.output["normalization_status"] == "partial"
    assert any(item.endswith(unknown_ref) for item in result.output["normalizations"])
    assert result.output["warnings"]
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_unknown_basis_refs_are_removed_and_traced_when_valid_refs_remain() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "a" * 64,
            "strings": [{"value": "MeasuredSampleMarker", "offset": 128}],
        },
    )
    valid_ref = str(observation.id)
    unknown_refs = [str(uuid4()) for _ in range(4)]
    payload = json.loads(
        advisor_response("Keep the measured signal in view.", [unknown_refs[0], valid_ref])
    )
    payload["discriminating_experiment"]["basis_refs"] = [valid_ref, unknown_refs[1]]
    payload["discarded_lead"]["basis_refs"] = [unknown_refs[2], valid_ref]
    payload["recommended_next_tools"] = [
        {
            "tool": "sample.analyze",
            "schema_version": "1",
            "why": "Check the cited observation independently.",
            "args_hint": {},
            "expected_signal": "A second measured result matches the observation.",
            "basis_refs": [unknown_refs[3], valid_ref],
        }
    ]
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=json.dumps(payload, ensure_ascii=False),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
            ),
        )
    )

    result = await tool.execute(
        {"consultation_type": "pivot_strategy", "evidence_refs": [valid_ref]},
        make_context(investigation_id, capabilities=("local.sample_analysis",)),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert result.output["normalization_status"] == "partial"
    assert result.output["alternative_hypotheses"][0]["basis_refs"] == (valid_ref,)
    assert result.output["discriminating_experiment"]["basis_refs"] == (valid_ref,)
    assert result.output["discarded_lead"]["basis_refs"] == (valid_ref,)
    recommendation = result.output["recommended_next_tools"][0]
    assert recommendation["basis_refs"] == (valid_ref,)
    assert recommendation["basis_observation_refs"] == (valid_ref,)
    expected_ignored = [
        ("$.alternative_hypotheses[0].basis_refs[0]", unknown_refs[0]),
        ("$.discriminating_experiment.basis_refs[1]", unknown_refs[1]),
        ("$.discarded_lead.basis_refs[0]", unknown_refs[2]),
        ("$.recommended_next_tools[0].basis_refs[0]", unknown_refs[3]),
    ]
    normalizations = result.output["normalizations"]
    assert len(normalizations) == 4
    for path, unknown_ref in expected_ignored:
        assert any(
            event.startswith(f"{path}:") and event.endswith(unknown_ref) for event in normalizations
        )

    prompt = transport.calls[0]["prompt"]
    assert "[E1]" in prompt
    assert valid_ref not in prompt
    assert all(unknown_ref not in prompt for unknown_ref in unknown_refs)

    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["output"]["normalization_status"] == "partial"
    assert persisted["output"]["normalizations"] == list(normalizations)


@pytest.mark.asyncio
async def test_recommendation_without_any_valid_observation_ref_is_dropped_and_traced() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "b" * 64,
            "strings": [{"value": "SyntheticPivotEvidence", "offset": 64}],
        },
    )
    unknown_ref = str(uuid4())
    payload = json.loads(
        advisor_response("Keep the measured signal in view.", [str(observation.id)])
    )
    payload["recommended_next_tools"] = [
        {
            "tool": "sample.analyze",
            "schema_version": "1",
            "why": "Check the cited observation independently.",
            "args_hint": {},
            "expected_signal": "A second measured result matches the observation.",
            "basis_refs": [unknown_ref],
        }
    ]
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=json.dumps(payload, ensure_ascii=False),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
            ),
        )
    )

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, capabilities=("local.sample_analysis",)),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["recommended_next_tools"] == ()
    assert result.output["advisor_status"] == "partial"
    assert result.output["normalization_status"] == "partial"
    assert any(item.endswith(unknown_ref) for item in result.output["normalizations"])
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["output"]["advisor_status"] == "partial"
    assert persisted["output"]["recommended_next_tools"] == []
    assert persisted["output"]["normalizations"] == list(result.output["normalizations"])


@pytest.mark.asyncio
async def test_article_source_ref_supports_non_tool_basis_without_transmitted_observations() -> (
    None
):
    investigation_id = uuid4()

    class ArticleBasisTransport:
        def __init__(self):
            self.calls = []

        async def submit(self, **kwargs):
            self.calls.append(kwargs)
            prompt = kwargs["prompt"]
            article_ref = re.search(r"^\[(E[1-9][0-9]*)\]\s+Article", prompt, re.M).group(1)
            return TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=advisor_response(
                    "L'article fournit un contexte de départ.", [article_ref]
                ),
            )

    transport = ArticleBasisTransport()
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)

    result = await tool.execute(
        {"consultation_type": "plan_investigation", "question": "S1P1"},
        make_context(investigation_id),
    )

    prompt = transport.calls[0]["prompt"]
    article_alias = re.search(r"^\[(E[1-9][0-9]*)\]\s+Article", prompt, re.M).group(1)
    assert result.status is ToolInvocationStatus.SUCCEEDED, dict(result.output)
    article_ref = result.output["alternative_hypotheses"][0]["basis_refs"][0]
    assert article_ref.startswith("article:")
    assert result.output["alternative_hypotheses"][0]["basis_refs"] == (article_ref,)
    assert result.output["discriminating_experiment"]["basis_refs"] == (article_ref,)
    assert result.output["discarded_lead"]["basis_refs"] == (article_ref,)
    assert result.output["recommended_next_tools"] == ()
    assert article_alias == "E1"
    assert "article:" not in prompt
    assert "article:" not in prompt


@pytest.mark.asyncio
async def test_article_source_ref_cannot_support_a_tool_recommendation() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "e" * 64,
            "strings": [{"value": "SyntheticPivotEvidence", "offset": 64}],
        },
    )

    class ArticleRecommendationTransport:
        def __init__(self):
            self.calls = []

        async def submit(self, **kwargs):
            self.calls.append(kwargs)
            prompt = kwargs["prompt"]
            article_ref = re.search(r"^\[(E[1-9][0-9]*)\]\s+Article", prompt, re.M).group(1)
            payload = json.loads(
                advisor_response("Comparer les signaux mesurés.", [str(observation.id)])
            )
            payload["recommended_next_tools"] = [
                {
                    "tool": "sample.analyze",
                    "schema_version": "1",
                    "why": "Examiner le signal mesuré.",
                    "args_hint": {},
                    "expected_signal": "Une nouvelle mesure discrimine les pistes.",
                    "basis_refs": [article_ref],
                }
            ]
            return TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=json.dumps(payload, ensure_ascii=False),
            )

    transport = ArticleRecommendationTransport()
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
            ),
        )
    )

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
            "question": "S1P1",
        },
        make_context(investigation_id, capabilities=("local.sample_analysis",)),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert result.output["recommended_next_tools"] == ()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("basis_refs", "expected_code"),
    [
        ("not-a-list", "invalid_reference_list"),
        (["duplicate-placeholder", "duplicate-placeholder"], "invalid_reference_list"),
        ([None], "invalid_reference_list"),
    ],
)
async def test_malformed_basis_ref_lists_remain_rejected(basis_refs, expected_code) -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "c" * 64,
            "strings": [{"value": "SyntheticPivotEvidence", "offset": 64}],
        },
    )
    payload = json.loads(
        advisor_response("Keep the measured signal in view.", [str(observation.id)])
    )
    if basis_refs == ["duplicate-placeholder", "duplicate-placeholder"]:
        payload["alternative_hypotheses"][0]["basis_refs"] = [
            str(observation.id),
            str(observation.id),
        ]
    elif basis_refs == ["too-many-placeholder"] * 9:
        payload["alternative_hypotheses"][0]["basis_refs"] = [
            str(observation.id),
            *(str(uuid4()) for _ in range(8)),
        ]
    else:
        payload["alternative_hypotheses"][0]["basis_refs"] = basis_refs
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=json.dumps(payload, ensure_ascii=False),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.output["validation_rejection"]["code"] == expected_code
    assert result.output["validation_rejection"]["path"] == "$.alternative_hypotheses[0].basis_refs"
    assert "normalization_status" not in result.output["validation_rejection"]


@pytest.mark.asyncio
async def test_observation_excerpts_require_matching_blob_descriptor_and_digest() -> None:
    investigation_id = uuid4()
    payload = {
        "schema": "dotnet-analysis-observation.v1",
        "assembly": "Blackwood.dll",
        "methods": [{"name": "MapImage", "body": "void MapImage() { MapSections(); }"}],
    }
    observation, raw = make_observation(investigation_id, payload)
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused")),
        observation=observation,
        raw=raw,
    )

    assert await tool._measurement_payload(observation) == payload
    assert (await tool._observation_content_excerpts(observation))[0]["kind"] == "method"

    tool._artifact_store.values[observation.blob_id] = raw[:-1] + b" "
    assert await tool._measurement_payload(observation) is None
    assert await tool._observation_content_excerpts(observation) == []

    tool._artifact_store.values[observation.blob_id] = raw
    tool._blobs.descriptors[observation.blob_id].descriptor.sha256 = "0" * 64
    assert await tool._measurement_payload(observation) is None


@pytest.mark.asyncio
async def test_short_verified_xxd_signature_remains_a_selectable_measurement() -> None:
    investigation_id = uuid4()
    sample_sha256 = "e" * 64
    output = b"00000080: 4372 6561 7465 4d75 7465 7841  CreateMutexA"
    payload = {
        "schema": "local-command-execution.v1",
        "sample_sha256": sample_sha256,
        "command_id": "xxd",
        "command": ["xxd", "-s", "128", "-l", "12", "Setup.exe"],
        "output_encoding": "base64-utf8",
        "output_chunks": [base64.b64encode(output).decode("ascii")],
        "output_byte_length": len(output),
        "output_sha256": hashlib.sha256(output).hexdigest(),
        "output_truncated": False,
        "output_truncated_by_evidence_limit": False,
    }
    observation, raw = make_observation(investigation_id, payload)
    observation = replace(
        observation,
        source="AutoWork local analysis",
        kind=EvidenceKind.BINARY_ANALYSIS,
        source_version="xxd-test-v1",
        object_type="command",
        object_id=sample_sha256,
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused")),
        observation=observation,
        raw=raw,
    )

    measurements = await tool._measurement_rows(observation)
    excerpts = await tool._observation_content_excerpts(observation)

    assert any(
        item["kind"] == "hex"
        and item["value"] == "4372656174654d7574657841"
        and item["offset"] == 128
        and item["length"] == 12
        for item in measurements
    )
    assert excerpts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("consultation_type", ["plan_investigation", "draft_text", "yara_draft"])
@pytest.mark.parametrize("measurement_key", ["hex", "hex_bytes", "base64"])
async def test_oversized_legacy_hex_measurement_is_rejected_before_prompt(
    consultation_type: str, measurement_key: str
) -> None:
    investigation_id = uuid4()
    sample_sha256 = "a" * 64
    oversized_hex = "41" * 262_144
    measurement_value = (
        base64.b64encode(b"A" * 513).decode() if measurement_key == "base64" else oversized_hex
    )
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_sha256,
            measurement_key: [{"value": measurement_value, "offset": 128}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256),)
    )
    tool._samples.list_for_subject = async_get(
        (
            SimpleNamespace(
                expected_hash=sample_sha256,
                source_object_id=None,
                state="quarantined",
                do_not_submit=True,
                external_llm_allowed=False,
                blob_id=sample_blob,
            ),
        )
    )
    tool._blobs.add(sample_blob, sha256=sample_sha256, size=4096)

    arguments: dict[str, Any] = {
        "consultation_type": consultation_type,
        "question": "Use the oversized raw hex marker from this observation.",
        "evidence_refs": [str(observation.id)],
    }
    if consultation_type == "draft_text":
        arguments["units"] = [
            {
                "id": "U1",
                "block_type": "key_findings",
                "form": "body",
                "anchor": None,
                "assertions": [
                    {
                        "id": "a1",
                        "text": "The oversized marker is relevant.",
                        "nature": "analyse",
                        "refs": [{"kind": "observation", "id": str(observation.id)}],
                        "block_type": "key_findings",
                        "form": "body",
                        "anchor": None,
                    }
                ],
            }
        ]
    result = await tool.execute(
        arguments,
        make_context(
            investigation_id,
            key=f"operator:advisor:oversized-legacy-hex:{consultation_type}",
        ),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["input_error_code"] == "binary_measurement_exceeds_limit", repr(
        result.output
    )
    assert [dict(item) for item in result.output["measurement_errors"]] == [
        {"index": 0, "reason": "binary_measurement_exceeds_512_byte_limit"}
    ]
    assert measurement_value not in repr(result.output)
    assert "dossier_text" not in result.output
    assert transport.calls == []


@pytest.mark.asyncio
async def test_oversized_generic_content_is_rejected_before_prompt() -> None:
    investigation_id = uuid4()
    oversized_content = "41!" * 6_000
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", "content": oversized_content},
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the content in this observation.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:oversized-generic-content"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["input_error_code"] == "content_excerpt_budget_exceeds_16384_bytes"
    assert oversized_content not in repr(result.output)
    assert "dossier_text" not in result.output
    assert transport.calls == []


@pytest.mark.asyncio
async def test_aggregate_generic_content_list_is_rejected_before_prompt() -> None:
    investigation_id = uuid4()
    first_content = "A!" * 7_953 + "A"
    second_content = "B!" * 7_953 + "B"
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "generic-observation.v1",
            "content": [first_content, second_content],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the listed content in this observation.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:oversized-content-list"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["input_error_code"] == "content_excerpt_budget_exceeds_16384_bytes"
    assert [dict(item) for item in result.output["omitted_evidence"]] == [
        {
            "kind": "observation",
            "id": str(observation.id),
            "status": "INTENTIONALLY_OMITTED",
            "reason": "content_excerpt_budget_exceeds_16384_bytes",
        }
    ]
    assert first_content not in repr(result.output)
    assert second_content not in repr(result.output)
    assert "dossier_text" not in result.output
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("nested", [False, True], ids=["neutral-list", "nested-neutral-list"])
async def test_aggregate_neutral_content_list_is_rejected_before_prompt(nested: bool) -> None:
    investigation_id = uuid4()
    first_content = "A!" * 7_953 + "A"
    second_content = "B!" * 7_953 + "B"
    content = (
        {"outer": {"items": [[first_content], {"leaf": [second_content]}]}}
        if nested
        else {"neutral": [first_content, second_content]}
    )
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", **content},
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review both synthetic markers from this observation.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key=f"operator:advisor:aggregate-neutral-list:{nested}"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["input_error_code"] == "content_excerpt_budget_exceeds_16384_bytes"
    assert [dict(item) for item in result.output["omitted_evidence"]] == [
        {
            "kind": "observation",
            "id": str(observation.id),
            "status": "INTENTIONALLY_OMITTED",
            "reason": "content_excerpt_budget_exceeds_16384_bytes",
        }
    ]
    assert first_content not in repr(result.output)
    assert second_content not in repr(result.output)
    assert "dossier_text" not in result.output
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "raw_size", "strip_padding"),
    [
        ("content", 600, False),
        ("content", 514, True),
        ("strings", 600, False),
        ("strings", 514, True),
    ],
    ids=["content-padded", "content-unpadded", "strings-padded", "strings-unpadded"],
)
async def test_oversized_generic_base64_is_rejected_before_prompt(
    field: str, raw_size: int, strip_padding: bool
) -> None:
    investigation_id = uuid4()
    encoded_content = base64.b64encode(b"A" * raw_size).decode("ascii")
    if strip_padding:
        encoded_content = encoded_content.rstrip("=")
    field_value: Any = (
        encoded_content
        if field == "content"
        else [{"value": encoded_content, "encoding": "base64"}]
    )
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", field: field_value},
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review this encoded synthetic content.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key=f"operator:advisor:generic-base64:{field}"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["input_error_code"] == "base64_content_exceeds_512_byte_decoded_limit"
    assert [dict(item) for item in result.output["omitted_evidence"]] == [
        {
            "kind": "observation",
            "id": str(observation.id),
            "status": "INTENTIONALLY_OMITTED",
            "reason": "base64_content_exceeds_512_byte_decoded_limit",
        }
    ]
    assert encoded_content not in repr(result.output)
    assert "dossier_text" not in result.output
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "representation"),
    [
        ("content", "plain"),
        ("content", "percent_encoded"),
        ("content", "newline_wrapped"),
        ("content", "space_wrapped"),
        ("content", "tab_wrapped"),
        ("strings", "plain"),
        ("strings", "percent_encoded"),
        ("strings", "newline_wrapped"),
        ("strings", "space_wrapped"),
        ("strings", "tab_wrapped"),
    ],
)
async def test_oversized_data_url_base64_is_rejected_before_prompt(
    field: str, representation: str
) -> None:
    investigation_id = uuid4()
    encoded_content = base64.b64encode(b"A" * 600).decode("ascii")
    if representation == "percent_encoded":
        encoded_content = "".join(f"%{ord(character):02X}" for character in encoded_content)
    elif representation == "newline_wrapped":
        encoded_content = f"{encoded_content[:400]}\n{encoded_content[400:]}"
    elif representation == "space_wrapped":
        encoded_content = f"{encoded_content[:400]} {encoded_content[400:]}"
    elif representation == "tab_wrapped":
        encoded_content = f"{encoded_content[:400]}\t{encoded_content[400:]}"
    data_url = f"data:application/octet-stream;base64,{encoded_content}"
    if field == "content":
        generic_content: dict[str, Any] = {"outer": {"content": data_url}}
    else:
        generic_content = {"outer": {"strings": [{"metadata": {"value": data_url}}]}}
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", **generic_content},
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review this nested synthetic data URL.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key=f"operator:advisor:generic-data-url:{field}"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["input_error_code"] == "data_url_base64_exceeds_512_byte_decoded_limit"
    assert [dict(item) for item in result.output["omitted_evidence"]] == [
        {
            "kind": "observation",
            "id": str(observation.id),
            "status": "INTENTIONALLY_OMITTED",
            "reason": "data_url_base64_exceeds_512_byte_decoded_limit",
        }
    ]
    assert data_url not in repr(result.output)
    assert "dossier_text" not in result.output
    assert transport.calls == []


@pytest.mark.asyncio
async def test_oversized_method_title_is_rejected_before_prompt() -> None:
    investigation_id = uuid4()
    oversized_name = "X" * 20_000
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "generic-observation.v1",
            "methods": [{"name": oversized_name, "body": "short synthetic method body"}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review this synthetic method.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:oversized-method-title"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["input_error_code"] == "content_excerpt_budget_exceeds_16384_bytes"
    assert [dict(item) for item in result.output["omitted_evidence"]] == [
        {
            "kind": "observation",
            "id": str(observation.id),
            "status": "INTENTIONALLY_OMITTED",
            "reason": "content_excerpt_budget_exceeds_16384_bytes",
        }
    ]
    assert oversized_name not in repr(result.output)
    assert "dossier_text" not in result.output
    assert transport.calls == []


@pytest.mark.asyncio
async def test_unavailable_observation_blob_is_not_marked_retrievable() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "f" * 64,
            "strings": [{"value": "SyntheticUnavailableBlob", "offset": 64}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    original_read_bytes = tool._artifact_store.read_bytes

    async def fail_observation_read(blob_id, *, max_bytes):
        if blob_id == observation.blob_id:
            raise OSError("synthetic storage read failure")
        return await original_read_bytes(blob_id, max_bytes=max_bytes)

    tool._artifact_store.read_bytes = fail_observation_read
    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use SyntheticUnavailableBlob as the required observation.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["preflight_status"] == "TECHNICAL_BLOCKED"
    assert transport.calls == []
    item = next(
        row
        for row in result.output["completeness_manifest"]
        if row["kind"] == "observation" and row["id"] == str(observation.id)
    )
    statuses = {row["status"] for row in item["statuses"]}
    assert "NOT_VERIFIED" in statuses
    assert "RETRIEVABLE" not in statuses
    assert any("unreadable" in row["reason"].casefold() for row in result.output["missing_refs"])


@pytest.mark.asyncio
async def test_minimal_yara_draft_selects_measured_blob_offsets_server_side() -> None:
    investigation_id = uuid4()
    sample_sha256 = "d" * 64
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_sha256,
            "strings": [
                {"value": "CreateMutexW", "offset": 128},
                {"value": "ReflectiveLoader", "offset": 384},
            ],
        },
    )
    rule_source = 'rule Sample { strings: $a = "CreateMutexW" condition: $a }'
    transport = Transport(
        lambda call: TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=typed_yara_response(call["prompt"], rule_source=rule_source),
            bridge_invocation_id=str(uuid4()),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    unmeasured, unmeasured_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "8" * 64,
            "strings": [],
        },
    )
    tool._observations.observations[unmeasured.id] = unmeasured
    tool._artifact_store.values[unmeasured.blob_id] = unmeasured_raw
    tool._blobs.add(
        unmeasured.blob_id,
        sha256=unmeasured.sha256,
        size=len(unmeasured_raw),
        logical_bucket="evidence",
    )
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256, id=uuid4()),)
    )
    tool._samples.list_for_subject = async_get(
        (
            SimpleNamespace(
                expected_hash=sample_sha256,
                source_object_id=None,
                state="quarantined",
                blob_id=sample_blob_id,
            ),
        )
    )
    tool._blobs.add(sample_blob_id, sha256=sample_sha256, size=4096)

    result = await tool.execute(
        {"consultation_type": "yara_draft"},
        make_context(investigation_id, key="operator:advisor:minimal-yara"),
    )

    prompt = transport.calls[0]["prompt"]
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert str(sample_blob_id) not in prompt
    assert "blob_id" not in typed_prompt_payload(prompt)["measurements"][0]
    assert "offset 128:" in prompt
    assert "offset 384:" in prompt
    assert result.output["rule_source"].rstrip("\n") == rule_source
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult" and item.object_id == "yara_draft"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    omitted = next(
        item
        for item in persisted["completeness_manifest"]
        if item["kind"] == "observation" and item["id"] == str(unmeasured.id)
    )
    assert "REQUIRED" not in {row["status"] for row in omitted["statuses"]}
    assert "INTENTIONALLY_OMITTED" in {row["status"] for row in omitted["statuses"]}


@pytest.mark.parametrize(
    "consultation_type",
    ["pivot_strategy", "draft_text", "suricata_draft", "figure_plan"],
)
@pytest.mark.asyncio
async def test_verified_measurement_candidates_reach_pivot_and_v3_prompts(
    consultation_type,
) -> None:
    investigation_id = uuid4()
    sample_sha256 = "e" * 64
    payload: dict[str, object] = {
        "schema": "sample-obfuscated-strings.v1",
        "sample_sha256": sample_sha256,
        "strings": [
            {"value": "CreateMutexW", "offset": 128},
            {"value": "ReflectiveLoader", "offset": 384},
        ],
    }
    if consultation_type == "suricata_draft":
        payload = {
            "schema": "investigation-tool-observation.v1",
            "result": {**payload, "http": {"host": "example.invalid"}},
        }
    observation, raw = make_observation(investigation_id, payload)
    if consultation_type == "suricata_draft":
        observation = replace(observation, object_type="network_capture")
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="JSON"))
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256, id=uuid4()),)
    )
    tool._samples.list_for_subject = async_get(
        (
            SimpleNamespace(
                expected_hash=sample_sha256,
                source_object_id=None,
                state="quarantined",
                blob_id=sample_blob_id,
            ),
        )
    )
    tool._blobs.add(sample_blob_id, sha256=sample_sha256, size=4096)
    if consultation_type == "figure_plan":
        finding = InvestigationFinding(
            investigation_id=investigation_id,
            kind=FindingKind.CAPABILITY,
            assessment=FindingAssessment.INCONCLUSIVE,
            statement="Synthetic figure finding for the offline fixture.",
            observation_refs=(),
        )
        tool._findings = FindingRepository((finding,))
    candidate = {
        "blob_id": str(sample_blob_id),
        "kind": "string",
        "offset": 128,
        "value": "CreateMutexW",
    }
    arguments = {
        "consultation_type": consultation_type,
        "evidence_refs": [str(observation.id)],
        "measurements": [candidate],
    }
    if consultation_type == "figure_plan":
        arguments["finding_refs"] = [str(finding.id)]
    if consultation_type == "draft_text":
        arguments["assertions"] = [
            {
                "id": "observed_marker",
                "text": "Une mesure existe.",
                "nature": "analyse",
                "refs": [{"kind": "observation", "id": str(observation.id)}],
                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                "form": "body",
                "anchor": None,
            }
        ]

    result = await tool.execute(arguments, make_context(investigation_id))

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert result.output["usable"] is False
    assert len(transport.calls) == 1
    prompt_data = typed_prompt_payload(transport.calls[0]["prompt"])
    assert prompt_data["measurements"][0]["value"] == "CreateMutexW"
    assert prompt_data["measurements"][0]["offset"] == 128
    evidence_alias = prompt_data["measurements"][0]["evidence_id"]
    assert re.fullmatch(r"E[1-9][0-9]*", evidence_alias)
    assert any(
        item == {"ref": evidence_alias, "kind": "measurement"}
        for item in prompt_data["wire_ref_catalog"]
    )
    assert prompt_data["measurements"][0]["measurement_id"] == evidence_alias
    assert "blob_id" not in prompt_data["measurements"][0]
    assert str(observation.id) not in transport.calls[0]["prompt"]
    assert str(sample_blob_id) not in transport.calls[0]["prompt"]


@pytest.mark.parametrize(
    ("source", "payload", "consultation_type", "expected_kind", "expected_value", "offset"),
    [
        (
            "autowork.floss",
            {
                "schema": "sample-obfuscated-strings.v1",
                "sample_sha256": "e" * 64,
                "strings": [{"value": "CreateMutexW", "offset": 128, "encoding": "ascii"}],
            },
            "pivot_strategy",
            "string",
            "CreateMutexW",
            128,
        ),
        (
            "autowork.floss",
            {
                "schema": "sample-obfuscated-strings.v1",
                "sample_sha256": "e" * 64,
                "strings": [{"value": "CreateMutexW", "offset": 128, "encoding": "ascii"}],
            },
            "draft_text",
            "string",
            "CreateMutexW",
            128,
        ),
        (
            "autowork.floss",
            {
                "schema": "sample-obfuscated-strings.v1",
                "sample_sha256": "e" * 64,
                "strings": [{"value": "CreateMutexW", "offset": 128, "encoding": "ascii"}],
            },
            "figure_plan",
            "string",
            "CreateMutexW",
            128,
        ),
        (
            "autowork.floss",
            {
                "schema": "investigation-tool-observation.v1",
                "result": {
                    "schema": "sample-obfuscated-strings.v1",
                    "sample_sha256": "e" * 64,
                    "strings": [{"value": "CreateMutexW", "offset": 128, "encoding": "ascii"}],
                    "http": {"host": "example.invalid"},
                },
            },
            "suricata_draft",
            "string",
            "CreateMutexW",
            128,
        ),
        (
            "dotnet.analysis",
            {
                "schema": "dotnet-analysis-observation.v1",
                "sample_sha256": "e" * 64,
                "result": {
                    "strings": [
                        {
                            "value": "CreateMutexW",
                            "offset": 128,
                            "offset_kind": "file",
                            "encoding": "ascii",
                            "source_file": "Setup.exe",
                        }
                    ]
                },
            },
            "yara_draft",
            "string",
            "CreateMutexW",
            128,
        ),
        (
            "AutoWork local analysis",
            {
                "schema": "local-command-execution.v1",
                "sample_sha256": "e" * 64,
                "command_id": "xxd",
                "command": ["xxd", "-s", "128", "-l", "12", "Setup.exe"],
                "output_encoding": "base64-utf8",
                "output_chunks": [
                    "MDAwMDAwODA6IDQzNzIgNjU2MSA3NDY1IDRkNzUgNzQ2NSA3ODQxICBDcmVhdGVNdXRleEE="
                ],
                "output_byte_length": 53,
                "output_sha256": hashlib.sha256(
                    b"00000080: 4372 6561 7465 4d75 7465 7841  CreateMutexA"
                ).hexdigest(),
            },
            "pivot_strategy",
            "hex",
            "4372656174654d7574657841",
            128,
        ),
    ],
)
@pytest.mark.asyncio
async def test_typed_floss_dotnet_and_verified_xxd_content_patterns_are_advisor_measurements(
    source, payload, consultation_type, expected_kind, expected_value, offset
) -> None:
    investigation_id = uuid4()
    sample_sha256 = "e" * 64
    observation, raw = make_observation(investigation_id, payload)
    is_local = source == "AutoWork local analysis"
    is_dotnet = source.startswith("dotnet.")
    observation = replace(
        observation,
        source=source,
        kind=(
            EvidenceKind.BINARY_ANALYSIS if is_local or is_dotnet else EvidenceKind.BINARY_STRING
        ),
        object_id=(f"{sample_sha256}:ilspycmd:request" if is_dotnet else sample_sha256),
        object_type=(
            "network_capture"
            if consultation_type == "suricata_draft"
            else "dotnet_analysis"
            if is_dotnet
            else "file"
            if is_local
            else "sample"
        ),
    )
    rule_source = 'rule Candidate { strings: $marker = "CreateMutexW" condition: $marker }'
    transport = Transport(
        lambda call: TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=(
                typed_yara_response(call["prompt"], rule_source=rule_source)
                if consultation_type == "yara_draft"
                else advisor_response("Pivot mesuré.", [str(observation.id)])
            ),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256, id=uuid4()),)
    )
    tool._samples.list_for_subject = async_get(
        (
            SimpleNamespace(
                expected_hash=sample_sha256,
                source_object_id=None,
                state="quarantined",
                blob_id=sample_blob_id,
            ),
        )
    )
    tool._blobs.add(sample_blob_id, sha256=sample_sha256, size=4096)
    if consultation_type == "figure_plan":
        finding = InvestigationFinding(
            investigation_id=investigation_id,
            kind=FindingKind.CAPABILITY,
            assessment=FindingAssessment.INCONCLUSIVE,
            statement="Synthetic figure finding for the offline fixture.",
            observation_refs=(),
        )
        tool._findings = FindingRepository((finding,))
    arguments: dict[str, object] = {
        "consultation_type": consultation_type,
        "evidence_refs": [str(observation.id)],
    }
    if consultation_type == "figure_plan":
        arguments["finding_refs"] = [str(finding.id)]
    if consultation_type == "suricata_draft":
        arguments["measurements"] = [
            {
                "blob_id": str(sample_blob_id),
                "kind": expected_kind,
                "value": expected_value,
                "offset": offset,
            }
        ]
    else:
        candidates = verify_content_pattern_candidates(
            (observation,), {str(observation.id): payload}
        )
        expected_hex = (
            expected_value if expected_kind == "hex" else expected_value.encode("ascii").hex()
        )
        candidate = next(item for item in candidates if item.hex_value == expected_hex)
        arguments["measurements"] = [{"content_pattern_id": candidate.id}]
    if consultation_type == "draft_text":
        arguments["assertions"] = [
            {
                "id": "measured_assertion",
                "text": "Le marqueur a été mesuré.",
                "nature": "analyse",
                "refs": [{"kind": "observation", "id": str(observation.id)}],
                "block_type": OpinionBlockType.KEY_FINDINGS.value,
                "form": "body",
                "anchor": None,
            }
        ]
    result = await tool.execute(arguments, make_context(investigation_id))

    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    measurements = typed_prompt_payload(prompt)["measurements"]
    assert measurements[0]["kind"] == expected_kind
    assert measurements[0]["value"] == expected_value
    assert measurements[0]["offset"] == offset
    assert "blob_id" not in measurements[0]
    evidence_alias = measurements[0]["evidence_id"]
    assert measurements[0]["measurement_id"] == evidence_alias
    assert re.fullmatch(r"E[1-9][0-9]*", evidence_alias)
    assert any(
        item == {"ref": evidence_alias, "kind": "measurement"}
        for item in typed_prompt_payload(prompt)["wire_ref_catalog"]
    )
    assert str(sample_blob_id) not in prompt
    if consultation_type == "yara_draft":
        assert result.output["schema"] == ADVISOR_RESPONSE_SCHEMA_V3


@pytest.mark.asyncio
async def test_yara_advisor_verifies_pivot_candidate_outside_selected_evidence_refs() -> None:
    investigation_id = uuid4()
    sample_sha256 = "e" * 64
    selected, selected_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "d" * 64,
            "strings": [{"value": "SyntheticSelectedEvidence", "offset": 32}],
        },
    )
    measured = bytes.fromhex("ed c7 c7 cb cf c3 f0 cc 94 96 c2 93 91 92 c1 88")
    ascii_column = "".join(chr(byte) if 32 <= byte < 127 else "." for byte in measured)
    dump = f"00000100: {' '.join(f'{byte:02x}' for byte in measured):<47}  {ascii_column}".encode()
    source_payload = {
        "schema": "local-command-execution.v1",
        "command_id": "xxd",
        "command": ["xxd", "-g", "1", "-s", "256", "-l", "16", "Setup.exe"],
        "sample_sha256": sample_sha256,
        "output_encoding": "base64-utf8",
        "output_byte_length": len(dump),
        "output_sha256": hashlib.sha256(dump).hexdigest(),
        "output_chunks": [base64.b64encode(dump).decode("ascii")],
        "output_truncated": False,
        "output_truncated_by_evidence_limit": False,
    }
    source, source_raw = make_observation(investigation_id, source_payload)
    source = replace(
        source,
        source="AutoWork local analysis",
        kind=EvidenceKind.BINARY_ANALYSIS,
        source_version="xxd-test-v1",
        object_type="command",
        object_id=sample_sha256,
    )
    transport = Transport(
        lambda call: TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=typed_yara_response(
                call["prompt"],
                rule_source=(
                    "rule Candidate { strings: $marker = { ed c7 c7 cb cf c3 f0 cc 94 "
                    "96 c2 93 91 92 c1 88 } condition: $marker }"
                ),
            ),
        )
    )
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=selected,
        raw=selected_raw,
    )
    tool._observations = ObservationRepository((selected, source))
    tool._artifact_store.values[source.blob_id] = source_raw
    tool._blobs.add(
        source.blob_id,
        sha256=source.sha256,
        size=len(source_raw),
        logical_bucket="evidence",
    )
    pattern_id = verify_content_pattern_candidates((source,), {str(source.id): source_payload})[
        0
    ].id

    result = await tool.execute(
        {
            "consultation_type": "yara_draft",
            "evidence_refs": [str(selected.id)],
            "measurements": [{"content_pattern_id": pattern_id}],
        },
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(transport.calls) == 1
    measurements = typed_prompt_payload(transport.calls[0]["prompt"])["measurements"]
    assert len(measurements) == 1
    assert measurements[0]["kind"] == "hex"
    assert measurements[0]["value"] == measured.hex()
    assert measurements[0]["offset"] == 256
    assert measurements[0]["evidence_id"] != str(selected.id)


@pytest.mark.asyncio
async def test_xxd_measurement_rejects_output_with_invalid_chunk_sha256() -> None:
    investigation_id = uuid4()
    payload = {
        "schema": "local-command-execution.v1",
        "sample_sha256": "e" * 64,
        "command_id": "xxd",
        "command": ["xxd", "-s", "128", "-l", "12", "Setup.exe"],
        "output_encoding": "base64-utf8",
        "output_chunks": [
            "MDAwMDAwODA6IDQzNzIgNjU2MSA3NDY1IDRkNzUgNzQ2NSA3ODQxICBDcmVhdGVNdXRleEE="
        ],
        "output_byte_length": 53,
        "output_sha256": "0" * 64,
    }
    observation, raw = make_observation(investigation_id, payload)
    observation = replace(observation, source="AutoWork local analysis")
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="{}")),
        observation=observation,
        raw=raw,
    )

    assert await tool._measurement_rows(observation) == []


@pytest.mark.asyncio
async def test_measurement_rejections_report_indexes_without_echoing_candidate_secrets() -> None:
    investigation_id = uuid4()
    sample_sha256 = "f" * 64
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_sha256,
            "strings": [{"value": "CreateMutexW", "offset": 128}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="{}"))
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256, id=uuid4()),)
    )
    tool._samples.list_for_subject = async_get(
        (
            SimpleNamespace(
                expected_hash=sample_sha256,
                source_object_id=None,
                state="quarantined",
                blob_id=sample_blob_id,
            ),
        )
    )
    tool._blobs.add(sample_blob_id, sha256=sample_sha256, size=4096)

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
            "measurements": [
                {
                    "blob_id": str(sample_blob_id),
                    "kind": "string",
                    "offset": 128,
                    "value": "PRIVATE-MEASUREMENT",
                },
                {"unknown": "PRIVATE-SHAPE"},
            ],
        },
        make_context(investigation_id, key="operator:advisor:measurement-diagnostics"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert [dict(item) for item in result.output["measurement_errors"]] == [
        {"index": 0, "reason": "measurement_not_verified"},
        {"index": 1, "reason": "measurement_shape_invalid"},
    ]
    assert "PRIVATE-MEASUREMENT" not in result.message
    assert "PRIVATE-MEASUREMENT" not in repr(result.output)
    assert "PRIVATE-SHAPE" not in repr(result.output)
    assert transport.calls == []


@pytest.mark.asyncio
async def test_literal_json_advisor_reply_is_partial_and_not_retried() -> None:
    investigation_id = uuid4()
    raw_output = PERSISTED_RESPONSE_SHAPES["cd3ae038-77b6-4e2d-aab8-83b291bc2e23"]["raw_text"]
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "strings": [{"value": "SyntheticMarker", "offset": 8}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=raw_output))
    tool, _ = make_tool(
        investigation_id=investigation_id, transport=transport, observation=observation, raw=raw
    )

    result = await tool.execute(
        {"consultation_type": "plan_investigation", "evidence_refs": [str(observation.id)]},
        make_context(investigation_id, key="operator:advisor:invalid-output"),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.error_code is None
    assert result.output["advisor_status"] == "partial"
    assert result.output["normalization_status"] == "partial"
    assert result.output["usable"] is False
    assert result.output["output_sha256"] == hashlib.sha256(raw_output.encode()).hexdigest()
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["output"]["normalizations"] == list(result.output["normalizations"])
    assert persisted["output"]["output_sha256"] == result.output["output_sha256"]
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_invalid_json_schema_excerpt_omits_response_values() -> None:
    investigation_id = uuid4()
    raw_output = json.dumps(
        {
            "schema": "wrong-schema",
            "summary": "PRIVATE-RESPONSE-MARKER",
            "untrusted": True,
        }
    )
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "strings": [{"value": "SyntheticMarker", "offset": 8}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=raw_output))
    tool, _ = make_tool(
        investigation_id=investigation_id, transport=transport, observation=observation, raw=raw
    )

    result = await tool.execute(
        {"consultation_type": "plan_investigation", "evidence_refs": [str(observation.id)]},
        make_context(investigation_id, key="operator:advisor:schema-diagnostic"),
    )

    assert result.output.get("validation_rejection") == {
        "code": "schema_mismatch",
        "path": "$.schema",
    }
    assert "PRIVATE-RESPONSE-MARKER" not in result.output["response_excerpt"]
    assert result.output["response_sha256"] == hashlib.sha256(raw_output.encode()).hexdigest()


@pytest.mark.asyncio
async def test_large_invalid_advisor_output_is_archived_without_content_limit() -> None:
    investigation_id = uuid4()
    raw_output = "private-response-marker " * 5_000
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "strings": [{"value": "SyntheticMarker", "offset": 8}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=raw_output))
    tool, _ = make_tool(
        investigation_id=investigation_id, transport=transport, observation=observation, raw=raw
    )
    tool._registry = Registry(
        (SimpleNamespace(name="advisor.consult", cost_model={BudgetDimension.BRIDGE_TURNS: 1}),)
    )

    result = await tool.execute(
        {"consultation_type": "plan_investigation", "evidence_refs": [str(observation.id)]},
        make_context(investigation_id, key="operator:advisor:oversized-output"),
    )

    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.output["advisor_status"] == "partial"
    assert result.output["response_bytes"] == len(raw_output.encode())
    assert result.output["output_sha256"] == hashlib.sha256(raw_output.encode()).hexdigest()
    output_blob_id = UUID(result.output["output_blob_id"])
    assert tool._artifact_store.values[output_blob_id] == raw_output.encode()
    prompt_blob_id = UUID(result.output["prompt_blob_id"])
    prompt = transport.calls[0]["prompt"]
    assert tool._artifact_store.values[prompt_blob_id] == prompt.encode()
    observation = tool._observations.observations[result.observation_refs[-1]]
    manifest = json.loads(tool._artifact_store.values[observation.blob_id])
    assert manifest["invocation_stage"] == "run_confirmed"
    assert manifest["prompt_blob_id"] == str(prompt_blob_id)
    assert manifest["prompt_bytes"] == len(prompt.encode())
    assert manifest["response_bytes"] == len(raw_output.encode())
    assert manifest["provider_usage"] == {"status": "usage_unavailable"}
    assert manifest["durations_ms"]["submission"] >= 0
    assert manifest["budget_reservation"]["bridge_turns"] == 1
    assert manifest["budget_consumption"]["bridge_turns"] == 1


@pytest.mark.asyncio
async def test_review_text_passes_cited_refs_without_internal_provenance() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "7" * 64,
            "strings": [{"value": "MeasuredSampleMarker", "offset": 128}],
        },
    )
    opinion_id = uuid4()
    finding_id = uuid4()
    opinion_sha256 = "b" * 64
    source_ref = f"opinion:{opinion_id}:v14:{opinion_sha256}:claim_verification"
    recommendation = {
        "tool": "sample.analyze",
        "schema_version": "1",
        "why": "Vérifier le signal déjà cité.",
        "args_hint": {"sample_sha256": "7" * 64},
        "expected_signal": "Une mesure indépendante confirme le constat.",
        "basis_refs": [str(observation.id)],
    }
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "Le paragraphe distingue les éléments observés des hypothèses.",
                [str(observation.id)],
                recommendations=(recommendation,),
            ),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._registry = Registry(
        (
            SimpleNamespace(
                name="sample.analyze",
                schema_version="1",
                capability_key="local.sample_analysis",
            ),
        )
    )
    block = SimpleNamespace(
        block_type=OpinionBlockType.CLAIM_VERIFICATION,
        text="Le paragraphe cite une observation existante.",
        finding_refs=(finding_id,),
        evidence_refs=(observation.id,),
    )
    opinion = SimpleNamespace(
        id=opinion_id,
        investigation_id=investigation_id,
        version=14,
        content_sha256=opinion_sha256,
        blocks=(block,),
    )
    tool._findings = FindingRepository(
        (
            InvestigationFinding(
                investigation_id=investigation_id,
                id=finding_id,
                kind=FindingKind.CAPABILITY,
                assessment=FindingAssessment.INCONCLUSIVE,
                statement="Le constat demeure à vérifier.",
                observation_refs=(),
            ),
        )
    )

    async def get_opinion(_opinion_id, *, version):
        assert version == 14
        return opinion

    tool._opinions.get = get_opinion

    result = await tool.execute(
        {
            "consultation_type": "review_text",
            "opinion_id": str(opinion_id),
            "opinion_version": 14,
            "block_type": "claim_verification",
        },
        make_context(
            investigation_id,
            key="operator:advisor:review-text-citations",
            capabilities=("local.sample_analysis",),
        ),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    prompt = transport.calls[0]["prompt"]
    assert "Le paragraphe cite une observation existante." in prompt
    prompt_data = typed_prompt_payload(prompt)
    evidence_alias = prompt_data["review_text"]["evidence_refs"][0]
    assert re.fullmatch(r"E[1-9][0-9]*", evidence_alias)
    assert {"ref": evidence_alias, "kind": "observation"} in prompt_data["wire_ref_catalog"]
    assert not any(item["kind"] == "opinion" for item in prompt_data["wire_ref_catalog"])
    assert str(observation.id) not in prompt
    assert str(opinion_id) not in prompt
    assert '"opinion_version"' not in prompt
    assert opinion_sha256 not in prompt
    assert source_ref not in prompt
    assert '"schema":"sample-obfuscated-strings.v1"' not in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    assert persisted["input_observation_refs"] == [str(observation.id)]
    assert persisted["input_source_refs"] == [source_ref]
    assert {"kind": "opinion", "id": source_ref, "sha256": opinion_sha256} in persisted[
        "input_refs"
    ]


def test_dossier_opinion_alias_is_attached_to_the_exact_opinion_section() -> None:
    opinion_id = str(uuid4())
    digest = "c" * 64
    source_ref = f"opinion:{opinion_id}:v59:{digest}:implant_materials"
    prompt_data = {
        "consultation_type": "review_text",
        "dossier_manifest": [
            {
                "alias": "E1",
                "kind": "opinion",
                "id": opinion_id,
                "sha256": digest,
                "opinion_version": 59,
                "block_type": "implant_materials",
                "source_ref": source_ref,
            }
        ],
    }

    assert _advisor_dossier_opinion_source_refs(prompt_data, "[E1] Avis — Matériel d'implant") == (
        source_ref,
    )


@pytest.mark.asyncio
async def test_review_text_includes_unit_fact_dossier_and_omits_advisor_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    investigation_id = uuid4()
    observations: list[EvidenceObservation] = []
    for index in range(17):
        sample_sha256 = f"{index + 1:x}" * 64
        observation, _raw = make_observation(
            investigation_id,
            {
                "schema": "sample-obfuscated-strings.v1",
                "sample_sha256": sample_sha256,
                "strings": [],
            },
        )
        observations.append(observation)
    findings = tuple(
        InvestigationFinding(
            investigation_id=investigation_id,
            kind=FindingKind.CAPABILITY,
            assessment=FindingAssessment.INCONCLUSIVE,
            statement=f"Le constat {index + 1} reste borné par les observations.",
            observation_refs=(),
            limitations=("La valeur n'a pas été décodée intégralement.",),
        )
        for index in range(4)
    )
    review_text = (
        "U6 distingue les faits des hypothèses. " + ("Phrase de revue bornée. " * 130)
    ).rstrip()
    block = SimpleNamespace(
        block_type=OpinionBlockType.UNCERTAINTIES_AND_NEXT_LEADS,
        text=review_text,
        finding_refs=tuple(item.id for item in findings),
        evidence_refs=tuple(item.id for item in observations),
    )
    opinion = SimpleNamespace(
        id=uuid4(),
        investigation_id=investigation_id,
        version=57,
        content_sha256="c" * 64,
        blocks=(block,),
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("Revue bornée.", [str(observations[0].id)]),
        )
    )
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observations[0],
        raw=b"{}",
    )
    tool._observations = ObservationRepository(observations)
    tool._findings = FindingRepository(findings)

    async def measurement_rows(item: EvidenceObservation) -> list[dict[str, object]]:
        index = observations.index(item)
        if index == 0:
            value = "RC4_KEY_EXAMPLE=0x1234; attacker_token=ghp_" + "a" * 36
        else:
            value = f"decoded implant value {index}"
        return [
            {
                "kind": "string",
                "value": value,
                "offset": index * 16,
                "sample_sha256": item.object_id,
            }
        ]

    monkeypatch.setattr(tool, "_measurement_rows", measurement_rows)
    requested_versions = []

    async def get_current_opinion(_opinion_id, *, version):
        requested_versions.append(version)
        return opinion if version is None else None

    tool._opinions.get = get_current_opinion
    definitions = tuple(
        SimpleNamespace(
            name=f"unused.tool.{index}",
            schema_version="1",
            capability_key=f"unused.capability.{index}",
            input_schema={"required": []},
        )
        for index in range(24)
    )
    tool._registry = Registry(definitions)
    context = make_context(
        investigation_id,
        key="operator:advisor:review-u6-fact-dossier",
        capabilities=tuple(item.capability_key for item in definitions),
    )

    result = await tool.execute(
        {
            "consultation_type": "review_text",
            "opinion_id": str(opinion.id),
            "units": [
                {
                    "id": "U6",
                    "block_type": "uncertainties_and_next_leads",
                    "form": "body",
                    "anchor": None,
                }
            ],
        },
        context,
    )

    assert requested_versions == [None]
    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    prompt = transport.calls[0]["prompt"]
    prompt_data = typed_prompt_payload(prompt)
    assert len(prompt_data["evidence"]) == 17
    assert len(prompt_data["findings"]) == 4
    assert len(prompt_data["measured_facts"]) == 17
    assert not any(item["kind"] == "opinion" for item in prompt_data["wire_ref_catalog"])
    assert all(
        re.fullmatch(r"E[1-9][0-9]*", item["ref"]) for item in prompt_data["wire_ref_catalog"]
    )
    assert "sha256_short" not in prompt
    assert str(opinion.id) not in prompt
    assert "opinion_version" not in prompt
    assert "tools" not in prompt_data
    assert "gaps" not in prompt_data
    assert "budget_remaining" not in prompt_data
    assert len(prompt.encode("utf-8")) <= MAX_ADVISOR_INPUT_BYTES
    assert "RC4_KEY_EXAMPLE" in prompt
    assert "RC4_KEY_EXAMPLE=0x1234" in prompt
    assert "ghp_" + "a" * 36 in prompt

    duplicate_unit = {
        "evidence_refs": [str(item.id) for item in observations],
        "finding_refs": [str(item.id) for item in findings],
    }
    (
        _,
        deduped_evidence,
        deduped_measurements,
        deduped_findings,
    ) = await tool._editorial_reference_facts(
        investigation_id=investigation_id,
        evidence=(),
        prompt_data={"units": [duplicate_unit, duplicate_unit]},
    )
    assert len(deduped_evidence) == 17
    assert len(deduped_measurements) == 17
    assert len(deduped_findings) == 4


@pytest.mark.asyncio
async def test_draft_text_resolves_facts_from_assertion_refs() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "d" * 64,
            "strings": [{"value": "MeasuredSampleMarker", "offset": 128}],
        },
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.INCONCLUSIVE,
        statement="Le déchiffrement du contenu reste incomplet.",
        observation_refs=(),
    )
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="response is not used")
    )
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._findings = FindingRepository((finding,))

    await tool.execute(
        {
            "consultation_type": "draft_text",
            "units": [
                {
                    "id": "U1",
                    "block_type": "uncertainties_and_next_leads",
                    "form": "body",
                    "anchor": None,
                    "assertions": [
                        {
                            "id": "a1",
                            "text": "Les éléments de déchiffrement sont incomplets.",
                            "nature": "analyse",
                            "refs": [
                                {"kind": "observation", "id": str(observation.id)},
                                {"kind": "finding", "id": str(finding.id)},
                            ],
                            "block_type": "uncertainties_and_next_leads",
                            "form": "body",
                            "anchor": None,
                        }
                    ],
                }
            ],
        },
        make_context(investigation_id, key="operator:advisor:draft-facts"),
    )

    prompt_data = typed_prompt_payload(transport.calls[0]["prompt"])
    assert len(prompt_data["evidence"]) == 1
    assert len(prompt_data["findings"]) == 1
    assert prompt_data["units"][0]["evidence_refs"]
    assert prompt_data["units"][0]["finding_refs"]
    assert "tools" not in prompt_data
    assert "gaps" not in prompt_data
    assert "budget_remaining" not in prompt_data


def test_context_truncation_stops_at_sentence_or_word_and_marks_ellipsis() -> None:
    sentence = _truncate_context_text(
        "Première phrase complète. Deuxième phrase qui continue sans fin.", 30
    )
    word = _truncate_context_text("clé active matière implantale", 16)

    assert sentence == "Première phrase complète.…"
    assert word.endswith("…")
    assert word == "clé active…"


@pytest.mark.asyncio
async def test_external_disclosure_denial_never_calls_bridge() -> None:
    investigation_id = uuid4()
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="should not run")
    )
    tool, _subject = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        allowed=False,
    )
    result = await tool.execute(
        {"consultation_type": "plan_investigation", "evidence_refs": []},
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.error_code == "POLICY_DENIED"
    assert transport.calls == []


def test_advisor_payload_redacts_credentials_embedded_in_urls() -> None:
    cleaned, count = _redact_advisor_payload(
        {"request": "fetch https://analyst:secret@example.test/path"}
    )
    assert cleaned["request"] == "fetch [clé masquée]"
    assert count == 1


def test_advisor_secret_filter_preserves_sample_material_and_redacts_autowork_secrets() -> None:
    operator_secret = "autowork_fixture_" + "Z" * 48
    settings = SimpleNamespace(operator_api_token=SecretStr(operator_secret))
    rc4_key = bytes(range(16)).hex()
    attacker_token = "ghp_" + "a" * 36
    payload = {
        "findings": [
            {
                "finding_id": str(uuid4()),
                "kind": "crypto_behavior",
                "assessment": "observed",
                "line": f"L'échantillon conserve une clé RC4: {rc4_key}.",
                "limitations": [],
            }
        ],
        "canonical_finding": {
            "id": str(uuid4()),
            "kind": "crypto_behavior",
            "assessment": "observed",
            "statement": f"La méthode lit le matériau de clé RC4: {rc4_key}.",
            "observation_refs": [],
            "claim_refs": [],
            "limitations": [],
        },
        "extracted_key": {"algorithm": "RC4", "key": rc4_key},
        "notes": [
            {
                "anchor": "section:2:paragraph:0003",
                "text": f"La note contient une clé AES = {rc4_key}.",
            },
            {
                "anchor": "section:2:paragraph:0004",
                "text": f"Le dépôt expose un PAT: {attacker_token}.",
            },
        ],
        "operator_api_token": operator_secret,
        "operator_token_assignment": f"OPERATOR_API_TOKEN={operator_secret}",
    }

    cleaned, count = _redact_advisor_payload(payload, settings=settings)

    encoded = json.dumps(cleaned, ensure_ascii=False)
    assert count == 2
    assert encoded.count("[clé masquée]") == 2
    assert rc4_key in encoded
    assert attacker_token in encoded
    assert operator_secret not in encoded
    assert not _marked_secret_field_survives(cleaned, settings=settings)
    assert _marked_secret_field_survives(
        {"operator_api_token": "unredacted test sentinel"}, settings=settings
    )


@pytest.mark.asyncio
async def test_bridge_receives_cti_material_in_clear_and_autowork_tokens_masked(
    monkeypatch,
) -> None:
    investigation_id = uuid4()
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=(
                "advisor-wire.v1\nTYPE: plan_investigation\nSTATUS: succeeded\n"
                "SUMMARY: Vérifier les valeurs citées.\nLIMITATION: none"
            ),
        )
    )
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    operator_secret = "autowork_fixture_" + "Z" * 48
    rc4_key = bytes(range(16)).hex()
    attacker_token = "github_pat_" + "b" * 82
    tool._settings.operator_api_token = SecretStr(operator_secret)

    async def consultation_data(**_kwargs):
        return {
            "question": "Vérifier le canal C2 de l'implant.",
            "evidence": [
                {
                    "evidence_id": str(uuid4()),
                    "source": "sample.analyze",
                    "object_type": "sample",
                    "sha256": "f" * 64,
                    "content_excerpts": [
                        {
                            "title": "Canal C2 de l'implant — paramètres de chiffrement",
                            "text": f"Clé RC4: {rc4_key}; jeton GitHub extrait: {attacker_token}.",
                        }
                    ],
                }
            ],
            "operator_api_token": operator_secret,
        }

    monkeypatch.setattr(tool, "_consultation_data", consultation_data)
    result = await tool.execute(
        {"consultation_type": "plan_investigation"}, make_context(investigation_id)
    )

    assert transport.calls
    prompt = str(transport.calls[0]["prompt"])
    assert rc4_key in prompt
    assert attacker_token in prompt
    assert "[clé masquée]" not in prompt
    assert operator_secret not in prompt
    assert result.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}


@pytest.mark.asyncio
async def test_advisor_refuses_and_persists_when_marked_secret_survives(monkeypatch) -> None:
    import cti_app.application.investigation_advisor_tools as advisor_tools_module

    investigation_id = uuid4()
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    monkeypatch.setattr(
        advisor_tools_module,
        "_redact_advisor_payload",
        lambda _payload, settings=None: ({"operator_api_token": "test sentinel"}, 0),
    )

    result = await tool.execute(
        {"consultation_type": "plan_investigation"}, make_context(investigation_id)
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_secret_field_remaining"
    assert result.output["refusal_code"] == "secret_field_remaining"
    assert transport.calls == []
    call = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(await tool._artifact_store.read_bytes(call.blob_id, max_bytes=16 * 1024))
    assert persisted["advisor_status"] == "refused"
    assert persisted["bridge_turns_consumed"] == 0


@pytest.mark.asyncio
async def test_figure_plan_only_transmits_operator_selected_findings() -> None:
    investigation_id = uuid4()
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused")),
    )
    finding = SimpleNamespace(
        id=uuid4(),
        investigation_id=investigation_id,
        version=3,
        kind=SimpleNamespace(value="behavior"),
        assessment=SimpleNamespace(value="observed"),
        statement="Le finding sélectionnable décrit une capacité observée.",
        observation_refs=(),
        claim_refs=(),
        limitations=(),
        supersedes=None,
    )
    tool._findings = SimpleNamespace(list_for_investigation=async_get((finding,)))
    investigation = SimpleNamespace(subject_id=uuid4())
    snapshot = SimpleNamespace(
        synthesis_sha256="a" * 64,
        article=SimpleNamespace(short_summary="Résumé", open_questions=()),
    )

    implicit = await tool._consultation_data(
        consultation_type="figure_plan",
        investigation_id=investigation_id,
        investigation=investigation,
        snapshot=snapshot,
        evidence=(),
        arguments={},
        context=make_context(investigation_id),
        question="",
    )
    selected = await tool._consultation_data(
        consultation_type="figure_plan",
        investigation_id=investigation_id,
        investigation=investigation,
        snapshot=snapshot,
        evidence=(),
        arguments={"finding_refs": [str(finding.id)]},
        context=make_context(investigation_id),
        question="",
    )

    assert implicit["findings"] == []
    assert [item["finding_id"] for item in selected["findings"]] == [str(finding.id)]


@pytest.mark.asyncio
async def test_draft_text_batches_units_and_keeps_provenance_per_unit() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "7" * 64,
            "strings": [{"value": "SyntheticDraftEvidence", "offset": 32}],
        },
    )
    block_types = [
        item
        for item in OpinionBlockType
        if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    ]

    def response_for_call(call):
        payload = typed_prompt_payload(call["prompt"])
        lines = ["advisor-wire.v1", "TYPE: draft_text", "STATUS: succeeded"]
        for unit in payload["units"]:
            assertion = next(
                item for item in payload["assertions"] if item["unit_id"] == unit["id"]
            )
            lines.extend(
                [
                    f"UNIT: {unit['id']}",
                    f"S: La mesure étaye la proposition {unit['id']}. [{assertion['id']}]",
                ]
            )
        lines.append("LIMITATION: none")
        return TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text="\n".join(lines),
            bridge_invocation_id=str(uuid4()),
        )

    transport = Transport(response_for_call)
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    units = []
    for index in range(1, 20):
        block_type = block_types[(index - 1) % len(block_types)]
        unit_id = f"U{index}"
        units.append(
            {
                "id": unit_id,
                "block_type": block_type.value,
                "form": "analyst_note",
                "anchor": f"unit-{index}",
                "assertions": [
                    {
                        "id": f"assertion_{index}",
                        "text": "La mesure observée justifie cette proposition.",
                        "nature": "analyse",
                        "refs": [{"kind": "observation", "id": str(observation.id)}],
                        "block_type": block_type.value,
                        "form": "analyst_note",
                        "anchor": f"unit-{index}",
                    }
                ],
            }
        )

    result = await tool.execute(
        {
            "consultation_type": "draft_text",
            "evidence_refs": [str(observation.id)],
            "units": units,
        },
        make_context(investigation_id, key="operator:advisor:draft-text-batch"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(transport.calls) == 1
    assert all(
        len(call["prompt"].encode("utf-8")) <= MAX_ADVISOR_INPUT_BYTES for call in transport.calls
    )
    assert [len(typed_prompt_payload(call["prompt"])["units"]) for call in transport.calls] == [19]
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert len(result.output["blocks"]) == 19
    provenance = result.output["bridge_provenance"]
    assert set(provenance["unit_provenance"]) == {f"U{index}" for index in range(1, 20)}
    assert (
        provenance["unit_provenance"]["U1"]["bridge_invocation_id"]
        == provenance["unit_provenance"]["U19"]["bridge_invocation_id"]
    )
    assert all(
        re.fullmatch(r"[0-9a-f]{64}", item["text_sha256"])
        for item in provenance["unit_provenance"].values()
    )


@pytest.mark.asyncio
async def test_editorial_batch_retains_cursor_and_stops_after_ambiguous_result() -> None:
    investigation_id = uuid4()
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=Transport(None),
    )
    submitted: list[tuple[Mapping[str, Any], ToolExecutionContext]] = []

    async def needs_review(arguments, context, *, _editorial_batch_child=False):
        submitted.append((arguments, context))
        return ToolResult(
            ToolInvocationStatus.NEEDS_REVIEW,
            error_code="advisor_result_ambiguous",
            output={"advisor_status": "needs_review", "usable": False},
        )

    tool.execute = needs_review
    units = [{"id": f"U{index}"} for index in range(1, 4)]
    context = make_context(investigation_id, key="operator:advisor:cursor")

    result = await tool._execute_editorial_batches(
        {"consultation_type": "draft_text"},
        context,
        units,
        maximum_units=1,
    )

    assert len(submitted) == 1
    assert submitted[0][0]["units"] == [units[0]]
    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.output["unprocessed_unit_ids"] == ("U1", "U2", "U3")
    assert result.output["batch_cursor"]["next_batch_index"] == 1
    assert result.output["batch_cursor"]["idempotency_key"]


@pytest.mark.asyncio
async def test_six_unit_editorial_batch_includes_clear_implant_materials() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "6" * 64,
            "strings": [{"value": "SyntheticEditorialEvidence", "offset": 32}],
        },
    )
    ordinary_types = [
        item
        for item in OpinionBlockType
        if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    ][:5]
    rc4_key = "00112233445566778899aabbccddeeff"
    aes_passphrase = "implant-clear-passphrase"
    xor_scheme = "xor(bytes, 0x37)"
    attacker_token = "ghp_" + "a" * 36
    implant_assertion = (
        f"RC4 key {rc4_key}; AES passphrase {aes_passphrase}; XOR {xor_scheme}; "
        f"attacker GitHub C2 token {attacker_token}."
    )

    def response_for_call(call):
        payload = typed_prompt_payload(call["prompt"])
        lines = ["advisor-wire.v1", "TYPE: draft_text", "STATUS: succeeded"]
        for unit in payload["units"]:
            assertion = next(
                item for item in payload["assertions"] if item["unit_id"] == unit["id"]
            )
            lines.extend(
                [f"UNIT: {unit['id']}", f"S: Section {unit['id']} rédigée. [{assertion['id']}]"]
            )
        lines.append("LIMITATION: none")
        return TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text="\n".join(lines),
            bridge_invocation_id=str(uuid4()),
        )

    transport = Transport(response_for_call)
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    sample_inventory_calls: list[UUID] = []

    async def list_samples(case_id):
        sample_inventory_calls.append(case_id)
        return ()

    tool._sample_associations = SimpleNamespace(list_for_investigation=list_samples)
    units = []
    for index, block_type in enumerate(
        (*ordinary_types, OpinionBlockType.IMPLANT_MATERIALS), start=1
    ):
        unit_id = f"U{index}"
        assertion_text = (
            implant_assertion
            if block_type is OpinionBlockType.IMPLANT_MATERIALS
            else "La mesure observée justifie cette proposition."
        )
        units.append(
            {
                "id": unit_id,
                "block_type": block_type.value,
                "form": "body",
                "anchor": None,
                "assertions": [
                    {
                        "id": f"assertion_{index}",
                        "text": assertion_text,
                        "nature": "analyse",
                        "refs": [{"kind": "observation", "id": str(observation.id)}],
                        "block_type": block_type.value,
                        "form": "body",
                        "anchor": None,
                    }
                ],
            }
        )

    result = await tool.execute(
        {
            "consultation_type": "draft_text",
            "evidence_refs": [str(observation.id)],
            "units": units,
        },
        make_context(investigation_id, key="operator:advisor:draft-text-six-units"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(transport.calls) == 1
    assert sample_inventory_calls == []
    payload = typed_prompt_payload(transport.calls[0]["prompt"])
    assert attacker_token in transport.calls[0]["prompt"], transport.calls[0]["prompt"]
    assert len(payload["units"]) == 6
    implant_unit = payload["units"][-1]
    assert implant_unit["id"] == "U6"
    assert implant_unit["block_type"] == "Matériel d\u2019implant"
    sent_text = json.dumps(payload, ensure_ascii=False)
    for clear_value in (rc4_key, aes_passphrase, xor_scheme, attacker_token):
        assert clear_value in sent_text
    assert result.output["redacted_secret_count"] == 0
    assert result.output["bridge_provenance"]["unit_provenance"]["U6"]["block_type"] == (
        OpinionBlockType.IMPLANT_MATERIALS.value
    )


@pytest.mark.asyncio
async def test_editorial_batch_stops_on_oversized_shared_context_with_observation(
    monkeypatch,
) -> None:
    import cti_app.application.investigation_advisor_tools as advisor_tools_module

    investigation_id = uuid4()
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    monkeypatch.setattr(advisor_tools_module, "MAX_ADVISOR_INPUT_BYTES", 500)

    async def oversized_context(**_kwargs):
        return {
            "consultation_type": "draft_text",
            "question": "Rédiger les sections demandées.",
            "units": [
                {"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None},
                {
                    "id": "U2",
                    "block_type": "implant_materials",
                    "form": "body",
                    "anchor": None,
                },
            ],
            "assertions": [
                {"id": "a1", "unit_id": "U1", "text": "Assertion one."},
                {"id": "a2", "unit_id": "U2", "text": "Implant value ghp_clear_fixture."},
            ],
            "evidence": [{"summary": "x" * 1800}],
            "truncation": {},
        }

    monkeypatch.setattr(tool, "_consultation_data", oversized_context)
    units = [
        {
            "id": "U1",
            "block_type": OpinionBlockType.KEY_FINDINGS.value,
            "form": "body",
            "anchor": None,
            "assertions": [],
        },
        {
            "id": "U2",
            "block_type": OpinionBlockType.IMPLANT_MATERIALS.value,
            "form": "body",
            "anchor": None,
            "assertions": [],
        },
    ]

    result = await tool.execute(
        {"consultation_type": "draft_text", "evidence_refs": [], "units": units},
        make_context(investigation_id, key="operator:advisor:draft-text-oversized-shared"),
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert transport.calls == []
    assert result.output["failure_stage"] == "prompt_size_preflight"
    assert result.output["failure_reason"] == "shared_context_exceeds_input_limit"
    assert result.output["confirmed_non_execution"] is True
    assert result.output["unprocessed_unit_ids"] == ("U1", "U2")
    assert result.output["batch_cursor"]["next_batch_index"] == 1
    advisor_call = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult" and item.object_type == "advisor_call"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_call.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["failure_stage"] == "prompt_size_preflight"
    assert persisted["failure_reason"] == "shared_context_exceeds_input_limit"


@pytest.mark.asyncio
async def test_draft_text_uses_compact_context_for_multiple_units() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "8" * 64,
            "strings": [{"value": "SyntheticCompactEvidence", "offset": 32}],
        },
    )
    block_types = [
        item
        for item in OpinionBlockType
        if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    ][:2]

    def response_for_call(call):
        payload = typed_prompt_payload(call["prompt"])
        lines = ["advisor-wire.v1", "TYPE: draft_text", "STATUS: succeeded"]
        for unit in payload["units"]:
            assertion = next(
                item for item in payload["assertions"] if item["unit_id"] == unit["id"]
            )
            lines.extend(
                [f"UNIT: {unit['id']}", f"S: Le texte {unit['id']} est fondé. [{assertion['id']}]"]
            )
        lines.append("LIMITATION: none")
        return TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text="\n".join(lines),
            bridge_invocation_id=str(uuid4()),
        )

    transport = Transport(response_for_call)
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._settings.operator_advisor_max_input_bytes = 15_000
    units = []
    for unit_index, block_type in enumerate(block_types, start=1):
        assertions = [
            {
                "id": f"u{unit_index}_assertion_{assertion_index}",
                "text": f"Élément {assertion_index}: " + "x" * 450,
                "nature": "analyse",
                "refs": [{"kind": "observation", "id": str(observation.id)}],
                "block_type": block_type.value,
                "form": "body",
                "anchor": None,
            }
            for assertion_index in range(1, 11)
        ]
        units.append(
            {
                "id": f"U{unit_index}",
                "block_type": block_type.value,
                "form": "body",
                "anchor": None,
                "assertions": assertions,
            }
        )

    result = await tool.execute(
        {
            "consultation_type": "draft_text",
            "evidence_refs": [str(observation.id)],
            "units": units,
        },
        make_context(investigation_id, key="operator:advisor:draft-text-size-split"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(transport.calls) <= 2
    assert all(len(call["prompt"].encode("utf-8")) <= 15_000 for call in transport.calls)
    assert sum(len(typed_prompt_payload(call["prompt"])["units"]) for call in transport.calls) == 2


@pytest.mark.asyncio
async def test_draft_text_preserves_bridge_output_above_legacy_limit() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "9" * 64,
            "strings": [{"value": "SyntheticLargeOutputEvidence", "offset": 32}],
        },
    )
    block_types = [
        item
        for item in OpinionBlockType
        if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    ][:2]

    implant_detail = "implant_auth_token=" + "malware-config-" * 9_000

    def response_for_call(call):
        payload = typed_prompt_payload(call["prompt"])
        lines = ["advisor-wire.v1", "TYPE: draft_text", "STATUS: succeeded"]
        for unit in payload["units"]:
            assertion = next(
                item for item in payload["assertions"] if item["unit_id"] == unit["id"]
            )
            lines.extend(
                [
                    f"UNIT: {unit['id']}",
                    f"S: La mesure étaye {unit['id']}. [{assertion['id']}]",
                ]
            )
        lines.append("LIMITATION: " + implant_detail)
        return TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text="\n".join(lines),
            bridge_invocation_id=str(uuid4()),
        )

    transport = Transport(response_for_call)
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    units = [
        {
            "id": f"U{index}",
            "block_type": block_type.value,
            "form": "body",
            "anchor": None,
            "assertions": [
                {
                    "id": f"output_assertion_{index}",
                    "text": "La mesure observée justifie cette proposition.",
                    "nature": "analyse",
                    "refs": [{"kind": "observation", "id": str(observation.id)}],
                    "block_type": block_type.value,
                    "form": "body",
                    "anchor": None,
                }
            ],
        }
        for index, block_type in enumerate(block_types, start=1)
    ]

    result = await tool.execute(
        {
            "consultation_type": "draft_text",
            "evidence_refs": [str(observation.id)],
            "units": units,
        },
        make_context(investigation_id, key="operator:advisor:draft-text-large-output"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert result.output["limitations"] == (implant_detail,)
    assert len(result.output["limitations"][0]) > 96 * 1024
    assert set(result.output["bridge_provenance"]["unit_provenance"]) == {"U1", "U2"}


@pytest.mark.asyncio
async def test_review_text_batches_selected_opinion_units_with_individual_hashes() -> None:
    investigation_id = uuid4()
    opinion_id = uuid4()
    opinion_sha = "b" * 64
    block_types = [
        item
        for item in OpinionBlockType
        if item not in {OpinionBlockType.ANALYST_OBSERVATIONS, OpinionBlockType.IMPLANT_MATERIALS}
    ]
    blocks = tuple(
        SimpleNamespace(
            block_type=block_type,
            text=f"Le texte {index} est relu séparément.",
            finding_refs=(),
            evidence_refs=(),
        )
        for index, block_type in enumerate(block_types, start=1)
    )
    implant_text = "RC4 key 00112233445566778899aabbccddeeff; C2 ghp_" + "c" * 36
    implant_section = SimpleNamespace(
        block_type=OpinionBlockType.IMPLANT_MATERIALS,
        text=implant_text,
        finding_refs=(),
        evidence_refs=(),
    )
    opinion = SimpleNamespace(
        id=opinion_id,
        investigation_id=investigation_id,
        version=27,
        content_sha256=opinion_sha,
        blocks=blocks,
        implant_materials_section=implant_section,
        observations=(),
    )

    def response_for_call(call):
        payload = typed_prompt_payload(call["prompt"])
        lines = ["advisor-wire.v1", "TYPE: review_text", "STATUS: succeeded"]
        for unit in payload["units"]:
            lines.extend(
                [f"UNIT: {unit['unit_id']}", "REMARK: Garder la portée de ce bloc explicite."]
            )
        lines.append("LIMITATION: none")
        return TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text="\n".join(lines),
            bridge_invocation_id=str(uuid4()),
        )

    transport = Transport(response_for_call)
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)

    async def get_opinion(_opinion_id, *, version=None):
        return opinion if version in (None, 27) else None

    tool._opinions.get = get_opinion
    units = [
        {
            "id": f"U{index}",
            "block_type": block_type.value,
            "form": "body",
            "anchor": None,
        }
        for index, block_type in enumerate(block_types, start=1)
    ]
    units.append(
        {
            "id": "U8",
            "block_type": OpinionBlockType.IMPLANT_MATERIALS.value,
            "form": "body",
            "anchor": None,
        }
    )

    result = await tool.execute(
        {
            "consultation_type": "review_text",
            "opinion_id": str(opinion_id),
            "opinion_version": 27,
            "units": units,
        },
        make_context(investigation_id, key="operator:advisor:review-text-batch"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(transport.calls) == 1
    assert len(result.output["reviews"]) == 8
    provenance = result.output["bridge_provenance"]["unit_provenance"]
    assert set(provenance) == {f"U{index}" for index in range(1, 9)}
    assert {item["reviewed_text_sha256"] for item in provenance.values()} == {
        hashlib.sha256(block.text.encode()).hexdigest() for block in (*blocks, implant_section)
    }
    final_payload = typed_prompt_payload(transport.calls[0]["prompt"])
    implant_unit = next(item for item in final_payload["units"] if item["unit_id"] == "U8")
    assert implant_unit["block_type"] == "Matériel d\u2019implant"
    assert implant_unit["text"] == implant_text


@pytest.mark.asyncio
async def test_advisor_server_does_not_enforce_local_inter_call_pacing() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "strings": [{"value": "SyntheticPacingEvidence", "offset": 16}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    prior_call = SimpleNamespace(
        id=uuid4(),
        investigation_id=investigation_id,
        source="advisor.consult",
        object_type="advisor_call",
        observed_at=datetime.now(UTC),
    )
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._observations = ObservationRepository((prior_call, observation))

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.status is not ToolInvocationStatus.UNAVAILABLE
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_advisor_server_does_not_enforce_local_bridge_turn_ceiling() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "strings": [{"value": "SyntheticTurnEvidence", "offset": 16}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    prior_calls = tuple(
        SimpleNamespace(
            id=uuid4(),
            investigation_id=investigation_id,
            source="advisor.consult",
            object_type="advisor_call",
            observed_at=datetime.now(UTC),
        )
        for _ in range(4)
    )
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._observations = ObservationRepository((*prior_calls, observation))

    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.error_code != "advisor_cap_exhausted"
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_advisor_total_above_finite_g2_remaining_is_not_locally_capped() -> None:
    investigation_id = uuid4()
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused")),
    )
    prior_calls = tuple(
        SimpleNamespace(
            id=uuid4(),
            investigation_id=investigation_id,
            source="advisor.consult",
            object_type="advisor_call",
            observed_at=datetime.now(UTC),
        )
        for _ in range(69)
    )
    tool._observations = ObservationRepository(prior_calls)

    result = await tool._check_advisor_limits(
        investigation_id,
        g2_bridge_turns_remaining=68,
    )

    assert result is None


@pytest.mark.asyncio
async def test_advisor_server_refuses_when_g2_bridge_budget_is_exhausted() -> None:
    investigation_id = uuid4()
    tool, _sample_blob_id = make_tool(investigation_id=investigation_id, transport=Transport(None))
    tool._observations = SimpleNamespace()

    result = await tool._check_advisor_limits(
        investigation_id,
        g2_bridge_turns_remaining=0,
    )

    assert result is not None
    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.error_code == "g2_bridge_budget_exhausted"
    assert result.output["reason"] == "g2_bridge_budget_exhausted"
    assert result.output["advisor_turn_limit"] is None
    assert result.output["advisor_turns_used"] == 0
    assert result.output["g2_bridge_turns_remaining"] == 0


@pytest.mark.asyncio
async def test_confirmed_rate_limit_does_not_consume_advisor_bridge_turn_budget() -> None:
    investigation_id = uuid4()
    prior_payload = {
        "schema": "operator-advisor-call.v1",
        "consultation_type": "plan_investigation",
        "advisor_status": "rate_limited",
        "bridge_turns_consumed": 0,
        "output": {"advisor_status": "rate_limited", "retry": False},
    }
    prior_raw = json.dumps(prior_payload, sort_keys=True, separators=(",", ":")).encode()
    prior_call = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=uuid4(),
        sha256=hashlib.sha256(prior_raw).hexdigest(),
        source="advisor.consult",
        source_version="advisor-consult.v1",
        observed_at=datetime.now(UTC),
        object_type="advisor_call",
        object_id="plan_investigation",
    )
    measurement, measurement_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "d" * 64,
            "strings": [{"value": "MeasuredSampleMarker", "offset": 128}],
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("Continuer les mesures.", [str(measurement.id)]),
        )
    )
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=prior_call,
        raw=prior_raw,
    )
    tool._observations.observations[measurement.id] = measurement
    tool._artifact_store.values[measurement.blob_id] = measurement_raw
    tool._blobs.add(
        measurement.blob_id,
        sha256=measurement.sha256,
        size=len(measurement_raw),
        logical_bucket="evidence",
    )
    result = await tool.execute(
        {"consultation_type": "pivot_strategy", "evidence_refs": [str(measurement.id)]},
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert len(transport.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("submission", "expected_status", "expected_code"),
    [
        (
            TurnSubmission(
                TurnSubmissionStatus.NEEDS_REVIEW,
                error_code="ui_timeout",
                retryable=False,
            ),
            ToolInvocationStatus.NEEDS_REVIEW,
            "advisor_timeout_or_ambiguous",
        ),
        (
            TurnSubmission(TurnSubmissionStatus.UNAVAILABLE, error_code="http_429"),
            ToolInvocationStatus.UNAVAILABLE,
            "advisor_rate_limited",
        ),
        (
            TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=""),
            ToolInvocationStatus.PARTIAL,
            None,
        ),
    ],
)
async def test_advisor_transport_failures_are_explicit_and_never_retried(
    submission, expected_status, expected_code
) -> None:
    investigation_id = uuid4()
    transport = Transport(submission)
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "strings": [{"value": "SyntheticTransportEvidence", "offset": 16}],
        },
    )
    tool, _subject = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.status is expected_status
    assert result.error_code == expected_code
    assert len(transport.calls) == 1


def test_advisor_parses_persisted_output_even_when_bridge_turn_needs_review() -> None:
    observation_id = uuid4()
    prompt_data = {
        "units": [{"id": "U1", "block_type": "key_findings", "form": "body", "anchor": None}],
        "assertions": [
            {
                "id": "assertion-1",
                "unit_id": "U1",
                "block_type": "key_findings",
                "form": "body",
                "anchor": None,
                "refs": [{"kind": "observation", "id": str(observation_id), "sha256": "a" * 64}],
            }
        ],
    }
    raw = """advisor-wire.v1
TYPE: draft_text
STATUS: succeeded
UNIT: U1
S: La mesure confirme le comportement observé. [A1]
LIMITATION: none
"""
    submission = TurnSubmission(
        TurnSubmissionStatus.NEEDS_REVIEW,
        output_text=raw,
        idempotency_key="persisted-turn-key",
        submission_state="post_submission",
    )

    tool, _ = make_tool(
        investigation_id=uuid4(),
        transport=Transport(submission),
    )
    result = tool._submission_result(
        submission,
        "prompt",
        (observation_id,),
        (),
        len(raw.encode("utf-8")),
        "draft_text",
        1,
        {},
        {},
        prompt_data=prompt_data,
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["advisor_status"] == "succeeded"
    assert result.output["blocks"][0]["sentences"][0]["assertion_ids"] == ("assertion-1",)
    assert result.output["bridge_turns_consumed"] == 1
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1


def test_yara_v3_preserves_complete_rule_over_summary_limit_and_checks_measurement_refs() -> None:
    measurement = {
        "blob_id": str(uuid4()),
        "sample_sha256": "a" * 64,
        "evidence_id": str(uuid4()),
        "evidence_sha256": "b" * 64,
        "extractor_version": "extractor-1",
        "kind": "hex",
        "value": "41 42 43 44",
        "offset": 32,
        "measurement_id": "m001",
    }
    measurement_sha = hashlib.sha256(
        json.dumps(measurement, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    rule_source = "rule Complete { strings: $bytes = { 41 42 43 44 } condition: $bytes }\n"
    rule_source += "// retained implant rule material\n" * 5_000
    raw = json.dumps(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA_V3,
            "consultation_type": "yara_draft",
            "status": "succeeded",
            "rule_source": rule_source,
            "rule_source_sha256": hashlib.sha256(rule_source.encode()).hexdigest(),
            "motifs": [
                {
                    "name": "$bytes",
                    "measurement_refs": [{"measurement_id": "m001", "sha256": measurement_sha}],
                }
            ],
            "invariants": ["Le motif correspond à une mesure vérifiée."],
            "limitations": [],
            "untrusted": True,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )

    parsed = parse_typed_advisor_response(
        raw,
        consultation_type="yara_draft",
        prompt_data={"measurements": [measurement]},
        safe_text=_safe_text,
    )

    assert len(rule_source) > 1_800
    assert len(rule_source.encode("utf-8")) > 96 * 1024
    assert len(raw.encode("utf-8")) > 96 * 1024
    assert parsed is not None
    assert parsed["rule_source"] == rule_source
    assert parsed["rule_source_sha256"] == hashlib.sha256(rule_source.encode()).hexdigest()
    assert parsed["motifs"][0]["measurement_refs"][0]["sha256"] == measurement_sha


def test_draft_text_rejects_untransmitted_uuid_and_requires_sentence_assertion() -> None:
    observation_id = str(uuid4())
    unknown_id = str(uuid4())
    assertion = {
        "id": "fact-1",
        "text": "La mesure établit le chemin observé.",
        "nature": "analyse",
        "refs": [{"kind": "observation", "id": observation_id, "sha256": "a" * 64}],
        "block_type": "key_findings",
        "form": "body",
        "anchor": None,
    }

    def response(sentence: str, assertion_ids: list[str]) -> str:
        return json.dumps(
            {
                "schema": ADVISOR_RESPONSE_SCHEMA_V3,
                "consultation_type": "draft_text",
                "status": "succeeded",
                "language": "fr",
                "blocks": [
                    {
                        "block_type": "key_findings",
                        "form": "body",
                        "anchor": None,
                        "sentences": [{"text": sentence, "assertion_ids": assertion_ids}],
                    }
                ],
                "limitations": [],
                "untrusted": True,
            }
        )

    prompt_data = {"assertions": [assertion]}
    accepted = parse_typed_advisor_response(
        response("La mesure établit le chemin observé.", ["fact-1"]),
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    rejected_ref = parse_typed_advisor_response(
        response(f"La mesure {unknown_id} établit le chemin observé.", ["fact-1"]),
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )
    rejected_assertion = parse_typed_advisor_response(
        response("La mesure établit le chemin observé.", ["invented"]),
        consultation_type="draft_text",
        prompt_data=prompt_data,
        safe_text=_safe_text,
    )

    assert accepted is not None
    assert rejected_ref is not None
    assert rejected_ref["status"] == "partial"
    assert rejected_ref["usable"] is False
    assert rejected_ref["blocks"] == []
    assert "draft_sentence_reference_invalid" in rejected_ref["warnings"]
    assert rejected_assertion is not None
    assert rejected_assertion["status"] == "partial"
    assert rejected_assertion.get("blocks", []) == []
    assert "reference_alias_unknown" in rejected_assertion["warnings"]


def test_figure_plan_accepts_only_typed_semantics_and_transmitted_refs() -> None:
    observation_id = str(uuid4())
    transmitted = {
        "kind": "observation",
        "id": observation_id,
        "sha256": "c" * 64,
    }
    figure_plan = {
        "schema": "advisor-figure-plan.v1",
        "title": "Relations observées",
        "legend": {"evidence_level": "observed", "text": "Chaque lien cite une observation."},
        "nodes": [
            {
                "id": "sample",
                "label": "Échantillon",
                "refs": [{"kind": "observation", "id": observation_id}],
            },
            {
                "id": "host",
                "label": "Hôte",
                "refs": [{"kind": "observation", "id": observation_id}],
            },
        ],
        "edges": [
            {
                "id": "relation",
                "source": "sample",
                "target": "host",
                "label": "Échange constaté",
                "refs": [{"kind": "observation", "id": observation_id}],
                "direction": None,
                "direction_refs": [],
            }
        ],
    }
    response = json.dumps(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA_V3,
            "consultation_type": "figure_plan",
            "status": "succeeded",
            "figure_plan": figure_plan,
            "limitations": [],
            "untrusted": True,
        },
        ensure_ascii=False,
    )
    parsed = parse_typed_advisor_response(
        response,
        consultation_type="figure_plan",
        prompt_data={"transmitted_refs": [transmitted]},
        safe_text=_safe_text,
    )

    assert parsed is not None
    assert parsed["figure_plan"] == figure_plan


@pytest.mark.asyncio
async def test_suricata_draft_refuses_before_bridge_without_measured_network_facts() -> None:
    investigation_id = uuid4()
    transport = Transport(None)
    tool, _sample_blob_id = make_tool(investigation_id=investigation_id, transport=transport)

    result = await tool.execute(
        {"consultation_type": "suricata_draft"},
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["preflight_status"] == "INSUFFICIENT_EVIDENCE"
    assert result.error_code == "advisor_preflight_insufficient_evidence"
    assert result.output["missing_refs"] == (
        {
            "ref": "network-fact",
            "reason": "No measured network fact was loaded for Suricata drafting.",
        },
    )
    assert result.output["bridge_turns_consumed"] == 0
    assert result.output["usable"] is False
    assert transport.calls == []
