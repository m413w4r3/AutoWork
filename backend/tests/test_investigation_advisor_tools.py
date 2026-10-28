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
from urllib.parse import quote
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
    MAX_ADVISOR_TOOL_SECONDS,
    InvestigationAdvisorTool,
    _advisor_dossier_opinion_source_refs,
    _contains_encoded_operational_secrets,
    _marked_secret_field_survives,
    _parse_advisor_output,
    _redact_advisor_payload,
    _safe_text,
    _truncate_context_text,
    register_investigation_advisor_tool,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    RegistryToolDispatcher,
    ToolExecutionContext,
    ToolResult,
)
from cti_app.domain.analyst_opinion import OpinionBlockType
from cti_app.domain.classification import TLP
from cti_app.domain.external_effects import EvidenceReference
from cti_app.domain.investigation import (
    BudgetDimension,
    DisclosureBounds,
    EnvelopeAuthorityKind,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    FrameworkPreSubmissionProof,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationExecutionMode,
    InvestigationFinding,
    TLPLevel,
    ToolInvocationStatus,
)


class Transport:
    def __init__(self, submission):
        self.submission = submission
        self.calls = []
        self.real_network_calls = 0

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


class SourceDocumentRepository:
    def __init__(self, documents=()) -> None:
        self.documents = {item.id: item for item in documents}

    async def get(self, document_id):
        return self.documents.get(document_id)


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


def make_tool(
    *,
    investigation_id,
    transport,
    observation=None,
    raw=None,
    allowed=True,
    role_timeout_seconds=7_200,
    max_tool_seconds=3_600,
):
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
        subject_id=subject_id,
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
            openai_bridge_wait_timeout_research_seconds=role_timeout_seconds,
            investigation_max_tool_seconds=max_tool_seconds,
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


class _SampleAssociations:
    def __init__(self, hashes):
        self.associations = tuple(
            SimpleNamespace(id=uuid4(), sample_sha256=item) for item in hashes
        )

    async def list_for_investigation(self, _investigation_id):
        return self.associations


class _Samples:
    def __init__(self, samples):
        self.samples = tuple(samples)

    async def list_for_subject(self, _subject_id):
        return self.samples


def _attach_sample_policy(tool, sample_blob_id, sample_hash, **flags):
    tool._blobs.add(
        sample_blob_id,
        sha256=sample_hash,
        size=1,
        logical_bucket="samples-quarantine",
    )
    tool._sample_associations = _SampleAssociations([sample_hash])
    sample = {
        "expected_hash": sample_hash,
        "source_object_id": None,
        "state": "quarantined",
        "blob_id": sample_blob_id,
        "tlp": TLP.GREEN,
        "do_not_submit": False,
        "external_llm_allowed": True,
    }
    sample.update(flags)
    tool._samples = _Samples([SimpleNamespace(**sample)])


def _make_downloaded_sample_fixture(investigation_id, sample_content, **sample_flags):
    sample_content = bytes(sample_content)
    sample_hash = hashlib.sha256(sample_content).hexdigest()
    blob_id = uuid4()
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.OTHER,
        blob_id=blob_id,
        sha256=sample_hash,
        source="virustotal.sample_download",
        source_version="sample-acquisition.v1",
        observed_at=datetime.now(UTC),
        object_type="binary_or_non_json",
        object_id=sample_hash,
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The cited sample bytes were available.", [str(observation.id)]
            ),
        )
    )
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
    )
    tool._observations = ObservationRepository((observation,))
    tool._blobs.add(
        blob_id,
        sha256=sample_hash,
        size=len(sample_content),
        logical_bucket="samples-quarantine",
    )
    tool._blobs.descriptors[blob_id].descriptor.mime_type = "application/x-synthetic-sample"
    tool._artifact_store.values[blob_id] = sample_content
    sample_id = uuid4()
    association_id = uuid4()
    tool._sample_associations = SimpleNamespace(
        list_for_investigation=async_get(
            (SimpleNamespace(id=association_id, sample_sha256=sample_hash),)
        )
    )
    sample = {
        "id": sample_id,
        "expected_hash": sample_hash,
        "source_object_id": None,
        "state": "quarantined",
        "blob_id": blob_id,
        "origin_kind": "source_seed",
        "original_name": "synthetic-sample.bin",
        "source_service": "fixture",
        "tlp": TLP.GREEN,
        "do_not_submit": False,
        "external_llm_allowed": True,
        "license_restriction": None,
    }
    sample.update(sample_flags)
    tool._samples = _Samples((SimpleNamespace(**sample),))
    return tool, transport, observation, sample_id, association_id, blob_id, sample_hash


def make_context(
    investigation_id,
    key="operator:advisor:test",
    capabilities=(),
    *,
    tool_seconds=3_600,
    bridge_turns=1,
):
    budget = InvestigationBudget(limits={})
    budget.reserve(
        key,
        {
            BudgetDimension.BRIDGE_TURNS: bridge_turns,
            BudgetDimension.TOOL_SECONDS: tool_seconds,
        },
    )
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("bridge",),
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="synthetic-test-approver",
    )
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=budget,
        idempotency_key=key,
        available_capabilities=frozenset(capabilities),
    )


def make_osint_policy_context(investigation_id, *, key):
    context = make_context(investigation_id, key=key)
    envelope = replace(
        context.envelope,
        authority_kind=EnvelopeAuthorityKind.OSINT_POLICY,
        inherited_policy_id=uuid4(),
        inherited_policy_version=1,
        inherited_policy_event_id=context.envelope.approval_decision_id,
    )
    return replace(context, envelope=envelope)


def make_dispatch_context(investigation_id, *, context_byte_limit=None):
    limits = (
        {BudgetDimension.CONTEXT_BYTES: context_byte_limit}
        if context_byte_limit is not None
        else {}
    )
    budget = InvestigationBudget(limits=limits)
    envelope = InvestigationEnvelope(
        investigation_id=investigation_id,
        allowed_providers=("bridge",),
        budget=budget,
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="synthetic-test-approver",
    )
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=envelope,
        budget=budget,
        idempotency_key=f"operator:advisor:dispatch:{uuid4()}",
        available_capabilities=frozenset({"bridge.advisor"}),
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
    assert len(transport.calls) == 1, result
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
                do_not_submit=False,
                external_llm_allowed=True,
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
    assert f"blob_id={sample_blob_id}" in prompt
    assert f"sha256={sample_sha256}" in prompt
    prompt_payload = typed_prompt_payload(prompt)
    assert "blob_id" not in prompt_payload["measurements"][0]
    evidence_alias = prompt_payload["measurements"][0]["evidence_id"]
    assert re.fullmatch(r"E[1-9][0-9]*", evidence_alias)
    assert prompt_payload["measurements"][0]["measurement_id"] == evidence_alias
    assert "offset 128:" in prompt
    assert {item["kind"] for item in typed_prompt_payload(prompt)["measurements"]} == {
        "string",
        "hex",
        "wide",
        "base64",
    }
    assert all(
        item.get("value") != "ignored-without-offset"
        for item in typed_prompt_payload(prompt)["measurements"]
    )
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

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    prompt = transport.calls[0]["prompt"]
    assert all(f"evidence_ids={item.id}" in prompt for item in observations)
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
    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert f"evidence_ids={observation.id}" in prompt
    assert f"blob_id={sample_blob_id}" in prompt
    assert {"ref": "E1", "kind": "observation"} in typed_prompt_payload(prompt)["wire_ref_catalog"]
    assert "UnrelatedTelemetryMarker" not in prompt
    assert "analysis_status_by_engine" in prompt
    assert "evidence_recorded" in prompt
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
    assert excerpts == [
        {
            "kind": "metadata",
            "title": "Métadonnée CTI — contenu observé",
            "text": f"sample_sha256={sample_sha256}",
        }
    ]


async def _submit_preservation_observation(payload, *, question: str, key: str):
    investigation_id = uuid4()
    observation, raw = make_observation(investigation_id, payload)
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The submitted evidence supports this review.", [str(observation.id)]
            ),
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
            "consultation_type": "plan_investigation",
            "question": question,
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key=key),
    )

    # Transport is the in-process fake above; no real network transport is configured.
    assert transport.real_network_calls == 0
    assert len(transport.calls) == 1
    assert result.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
    provenance = result.output["bridge_provenance"]
    assert provenance["preflight_status"] == "READY"
    manifest = next(
        item
        for item in provenance["dossier_manifest"]
        if item["kind"] == "observation" and item["id"] == str(observation.id)
    )
    return transport.calls[0]["prompt"], manifest


@pytest.mark.parametrize("body_size", [64, 4_096, 128 * 1024])
@pytest.mark.asyncio
async def test_explicit_evidence_get_tool_result_body_reaches_bridge_at_any_size(body_size):
    investigation_id = uuid4()
    body = f"evidence-get-result-{body_size}:" + ("x" * body_size)
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "investigation-tool-observation.v1",
            "tool_name": "evidence.get",
            "schema_version": "1",
            "operation_key": "operator:evidence-get-fixture",
            "result": {"opaque_result_field": body},
        },
    )
    observation = replace(
        observation,
        kind=EvidenceKind.OTHER,
        source="evidence.get",
        source_version="1",
        object_type="tool_result",
        object_id="evidence.get",
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("The cited result is available.", [str(observation.id)]),
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
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key=f"operator:advisor:evidence-get-{body_size}"),
    )

    assert result.error_code is None, result.output
    assert result.output["bridge_provenance"]["preflight_status"] == "READY"
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert body in prompt
    cited = next(
        item
        for item in result.output["bridge_provenance"]["dossier_manifest"]
        if item["kind"] == "observation" and item["id"] == str(observation.id)
    )
    assert cited["body_bytes"] >= len(body.encode("utf-8"))


@pytest.mark.asyncio
async def test_600_byte_dns_raw_base64_reaches_fake_transport() -> None:
    encoded = base64.b64encode(b"A" * 600).decode("ascii")

    prompt, manifest = await _submit_preservation_observation(
        {
            "schema": "generic-observation.v1",
            "dns": [{"name": "DNS raw evidence", "raw": encoded, "offset": 256}],
        },
        question="Review DNS raw evidence.",
        key="operator:advisor:dns-raw-base64",
    )

    assert encoded in prompt
    assert "Texte observé — DNS raw evidence — offset 256" in prompt
    expected_body = f"Texte observé — DNS raw evidence — offset 256:\n| {encoded}"
    assert manifest["body_bytes"] == len(expected_body.encode("utf-8"))
    assert manifest["body_sha256"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_spaced_fragmented_600_byte_base64_reaches_fake_transport() -> None:
    encoded = base64.b64encode(b"A" * 600).decode("ascii")
    fragmented = " ".join(encoded[index : index + 60] for index in range(0, len(encoded), 60))

    prompt, _manifest = await _submit_preservation_observation(
        {
            "schema": "generic-observation.v1",
            "base64": [
                {"name": "Fragmented Base64 evidence", "value": fragmented, "encoding": "base64"}
            ],
        },
        question="Review fragmented Base64 evidence.",
        key="operator:advisor:fragmented-base64",
    )

    assert fragmented in prompt


@pytest.mark.asyncio
async def test_two_independent_300_byte_base64_values_reach_fake_transport() -> None:
    first = base64.b64encode(b"A" * 300).decode("ascii")
    second = base64.b64encode(b"B" * 300).decode("ascii")

    prompt, _manifest = await _submit_preservation_observation(
        {
            "schema": "generic-observation.v1",
            "dns": [
                {"name": "DNS evidence first", "raw": first},
                {"name": "DNS evidence second", "raw": second},
            ],
        },
        question="Review both DNS evidence values.",
        key="operator:advisor:two-base64-values",
    )

    assert first in prompt
    assert second in prompt


@pytest.mark.asyncio
async def test_large_legacy_hex_and_base64_measurements_reach_fake_transport() -> None:
    investigation_id = uuid4()
    sample_sha256 = "a" * 64
    hex_value = "41" * 600
    base64_value = base64.b64encode(b"B" * 600).decode("ascii")
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_sha256,
            "hex": [{"value": hex_value, "offset": 128}],
            "base64": [{"value": base64_value, "offset": 256}],
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
                do_not_submit=False,
                external_llm_allowed=True,
                blob_id=sample_blob,
            ),
        )
    )
    tool._blobs.add(sample_blob, sha256=sample_sha256, size=4096)

    result = await tool.execute(
        {
            "consultation_type": "yara_draft",
            "question": "Use these long hex and Base64 measurements to draft a YARA rule.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:large-legacy-measurements"),
    )

    assert len(transport.calls) == 1
    assert transport.real_network_calls == 0
    assert result.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
    measurements = typed_prompt_payload(transport.calls[0]["prompt"])["measurements"]
    by_kind = {item["kind"]: item for item in measurements}
    assert by_kind["hex"]["value"] == hex_value
    assert by_kind["hex"]["offset"] == 128
    assert by_kind["base64"]["value"] == base64_value
    assert by_kind["base64"]["offset"] == 256


@pytest.mark.asyncio
async def test_large_text_and_hex_reach_fake_transport_with_matching_manifest() -> None:
    text_value = "DEADBEEF text marker " + "t" * 18_000
    hex_value = " ".join("DEADBEEF" for _ in range(3_000))

    prompt, manifest = await _submit_preservation_observation(
        {
            "schema": "generic-observation.v1",
            "text": [{"name": "Long text evidence", "value": text_value}],
            "hex": [{"offset": 128, "value": hex_value}],
        },
        question="Review DEADBEEF text and hex evidence.",
        key="operator:advisor:large-text-and-hex",
    )

    byte_title = next(
        line[2:]
        for line in prompt.splitlines()
        if line.startswith("| Octets observés; encoding=hex;")
    )
    assert f"byte_length={len(bytes.fromhex(hex_value))}" in byte_title
    assert f"sha256={hashlib.sha256(bytes.fromhex(hex_value)).hexdigest()}" in byte_title
    assert "offset 128" in byte_title
    assert "evidence_id=" in byte_title
    assert "blob_id=" in byte_title
    expected_body = (
        f"Texte observé — Long text evidence:\n| {text_value}\n\n{byte_title}\n| {hex_value}"
    )
    assert text_value in prompt
    assert hex_value in prompt
    assert manifest["body_bytes"] == len(expected_body.encode("utf-8"))
    assert manifest["body_sha256"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_contradictory_permitted_dns_evidence_pieces_are_both_transmitted() -> None:
    first = "DNS answer for example.invalid is 192.0.2.10."
    second = "DNS answer for example.invalid is 198.51.100.20."

    prompt, manifest = await _submit_preservation_observation(
        {
            "schema": "generic-observation.v1",
            "dns": [
                {"name": "DNS answer observation", "raw": first},
                {"name": "DNS answer observation", "raw": second},
            ],
        },
        question="Compare DNS answer observations for example.invalid.",
        key="operator:advisor:contradictory-dns-evidence",
    )

    assert first in prompt
    assert second in prompt
    expected_body = (
        f"Texte observé — DNS answer observation:\n| {first}\n\n"
        f"Texte observé — DNS answer observation:\n| {second}"
    )
    assert manifest["body_bytes"] == len(expected_body.encode("utf-8"))
    assert manifest["body_sha256"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
async def test_nested_operational_secret_is_redacted_before_transport() -> None:
    investigation_id = uuid4()
    marker = "FIXTURE_OPERATIONAL_SECRET_123"
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "generic-observation.v1",
            "vt_api_key": {"base64": marker},
            "content": "CTI context remains visible.",
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The context supports this review.", [str(observation.id)]
            ),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the CTI context.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:nested-secret"),
    )

    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert marker not in prompt
    assert "CTI context remains visible." in prompt


@pytest.mark.asyncio
async def test_generic_api_key_in_cti_evidence_is_not_an_autowork_secret() -> None:
    investigation_id = uuid4()
    marker = "ATTACKER_TOKEN_EVIDENCE"
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "generic-observation.v1",
            "api_key": {"base64": marker},
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The context supports this review.", [str(observation.id)]
            ),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the CTI context.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:cti-api-key"),
    )

    assert len(transport.calls) == 1
    assert marker in transport.calls[0]["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction",
    [
        {"external_llm_allowed": False},
        {"tlp": TLP.RED},
        {"license_restriction": "Owner prohibits external disclosure"},
    ],
)
async def test_sample_disclosure_keeps_real_explicit_vetoes(restriction) -> None:
    investigation_id = uuid4()
    sample_hash = "c" * 64
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "hex": [{"offset": 0, "value": "41" * 600}],
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The measured sample is available.", [str(observation.id)]
            ),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(tool, sample_blob_id, sample_hash, **restriction)

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the measured sample.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:restricted-sample"),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert result.output["preflight_status"] == "POLICY_BLOCKED"
    assert tuple(result.output["disclosure_restricted_refs"]) == (str(observation.id),)
    assert result.output["confirmed_non_execution"] is True
    if restriction.get("license_restriction"):
        assert restriction["license_restriction"] not in repr(result.output)


@pytest.mark.asyncio
async def test_restriction_trace_counts_sample_associations_not_observations() -> None:
    investigation_id = uuid4()
    sample_hash = "9" * 64
    restriction_text = "Synthetic count semantics restriction marker"
    first, first_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "strings": [{"value": "RestrictedCountMarkerOne", "offset": 16}],
        },
    )
    second, second_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "strings": [{"value": "RestrictedCountMarkerTwo", "offset": 32}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=first,
        raw=first_raw,
    )
    tool._observations = ObservationRepository((first, second))
    tool._blobs.add(
        second.blob_id,
        sha256=second.sha256,
        size=len(second_raw),
        logical_bucket="evidence",
    )
    tool._artifact_store.values[second.blob_id] = second_raw
    _attach_sample_policy(
        tool,
        sample_blob_id,
        sample_hash,
        license_restriction=restriction_text,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review both observations for one restricted sample.",
            "evidence_refs": [str(first.id), str(second.id)],
        },
        make_context(investigation_id, key="operator:advisor:restricted-counts"),
    )

    association = tool._sample_associations.associations[0]
    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert result.output["disclosure_restricted_observation_count"] == 2
    assert result.output["disclosure_restricted_sample_count"] == 1
    assert result.output["disclosure_restricted_sample_association_count"] == 1
    rows = result.output["disclosure_restricted_sample_associations"]
    assert len(rows) == 1
    assert rows[0]["association_id"] == str(association.id)
    assert rows[0]["sample_sha256"] == sample_hash
    assert tuple(rows[0]["restriction_sources"]) == ("license_restriction",)
    assert set(rows[0]["linked_observation_refs"]) == {str(first.id), str(second.id)}
    assert restriction_text not in repr(result.output)


@pytest.mark.asyncio
async def test_restriction_trace_aggregates_conflicting_sample_records() -> None:
    investigation_id = uuid4()
    sample_hash = "8" * 64
    restriction_text = "Synthetic conflicting-record restriction marker"
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "strings": [{"value": "RestrictedConflictingMarker", "offset": 16}],
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(
        tool,
        sample_blob_id,
        sample_hash,
        license_restriction=restriction_text,
    )
    restricted_sample = tool._samples.samples[0]
    allowed_sample = SimpleNamespace(
        expected_hash=sample_hash,
        source_object_id=None,
        state="quarantined",
        blob_id=sample_blob_id,
        tlp=TLP.GREEN,
        do_not_submit=False,
        external_llm_allowed=True,
    )
    submit_blocked_sample = SimpleNamespace(
        expected_hash=sample_hash,
        source_object_id=None,
        state="quarantined",
        blob_id=sample_blob_id,
        tlp=TLP.GREEN,
        do_not_submit=True,
        external_llm_allowed=True,
    )
    # Every remaining explicit restriction on a record sharing the cited
    # sample hash vetoes disclosure and appears in the refusal trace.
    tool._samples = _Samples([allowed_sample, restricted_sample, submit_blocked_sample])

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the conflicting sample records.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:restricted-conflicting-records"),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert result.output["disclosure_restricted_sample_count"] == 1
    rows = result.output["disclosure_restricted_sample_associations"]
    assert len(rows) == 1
    assert tuple(rows[0]["restriction_sources"]) == ("license_restriction",)
    assert restriction_text not in repr(result.output)


@pytest.mark.asyncio
async def test_nested_sample_hash_with_license_restriction_refuses_before_bridge() -> None:
    investigation_id = uuid4()
    sample_hash = "e" * 64
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "investigation-tool-observation.v1",
            "result": {
                "schema": "sample-obfuscated-strings.v1",
                "sample_sha256": sample_hash,
                "strings": [{"value": "RESTRICTED_NESTED_SAMPLE_MARKER", "offset": 32}],
            },
        },
    )
    observation = replace(observation, object_id="sample")
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._blobs.descriptors[
        observation.blob_id
    ].descriptor.logical_bucket = "investigation-evidence"
    tool._observations.observations[observation.id] = observation
    _attach_sample_policy(
        tool,
        sample_blob_id,
        sample_hash,
        license_restriction="Owner prohibits external disclosure",
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the nested sample observation.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:nested-restricted-sample"),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert tuple(result.output["disclosure_restricted_refs"]) == (str(observation.id),)
    safe_output = repr(result.output)
    assert "Owner prohibits external disclosure" not in safe_output
    assert "RESTRICTED_NESTED_SAMPLE_MARKER" not in safe_output


@pytest.mark.asyncio
async def test_unrestricted_sample_measurement_reaches_fake_transport() -> None:
    investigation_id = uuid4()
    sample_hash = "c" * 64
    value = "41" * 600
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "hex": [{"offset": 0, "value": value}],
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The measurement supports this review.", [str(observation.id)]
            ),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(tool, sample_blob_id, sample_hash)

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the measured sample.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:allowed-sample"),
    )

    assert len(transport.calls) == 1
    assert value in transport.calls[0]["prompt"]
    assert result.output["bridge_provenance"]["preflight_status"] == "READY"


@pytest.mark.asyncio
async def test_lossless_sample_bytes_metadata_and_provenance_reach_fake_transport() -> None:
    investigation_id = uuid4()
    attacker_token = "ghp_" + "a" * 36
    raw_sample = bytes((0x90, 0x90, 0xC3, 0x41, 0x42)) + b" " + attacker_token.encode()
    sample_sha256 = hashlib.sha256(raw_sample).hexdigest()
    base64_content = base64.b64encode(raw_sample).decode("ascii")
    contradictory_source_hash = "d" * 64
    observation, raw_observation = make_observation(
        investigation_id,
        {
            "schema": "sample-byte-metadata.v1",
            "sample_sha256": sample_sha256,
            "raw_bytes": {"value": list(raw_sample), "offset": 37},
            "bytes": {
                "value": {
                    "encoding": "base64",
                    "byte_length": len(raw_sample),
                    "sha256": sample_sha256,
                    "content_base64": base64_content,
                },
                "offset": 38,
            },
            "hex": [
                {"offset": 37, "value": raw_sample.hex()},
                {"offset": 91, "length": 4, "value": "00ff1020"},
            ],
            "base64": base64_content,
            "mime_type": "application/x-synthetic-sample",
            "byte_length": len(raw_sample),
            "status": "clean",
            "analysis_status": "malicious",
            "engines": ["EngineAlpha", "EngineBeta"],
            "traces": ["trace-fragment-1"],
            "fragments": [{"offset": 37, "length": len(raw_sample)}],
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The verified sample context supports the review.", [str(observation.id)]
            ),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw_observation,
    )
    association_id = uuid4()
    sample_id = uuid4()
    tool._blobs.add(
        sample_blob_id,
        sha256=sample_sha256,
        size=len(raw_sample),
        logical_bucket="samples-quarantine",
    )
    mime_type = "application/x-synthetic-sample; version=2; charset=binary"
    tool._blobs.descriptors[sample_blob_id].descriptor.mime_type = mime_type
    tool._artifact_store.values[sample_blob_id] = raw_sample

    async def sample_associations(_investigation_id):
        return (
            SimpleNamespace(
                id=association_id,
                sample_sha256=sample_sha256,
                qualification="confirmed",
                related_sample_sha256=(contradictory_source_hash,),
            ),
        )

    async def samples_for_subject(_subject_id):
        return (
            SimpleNamespace(
                id=sample_id,
                expected_hash=sample_sha256,
                source_object_id=contradictory_source_hash,
                blob_id=sample_blob_id,
                state="quarantined",
                origin_kind="source_seed",
                original_name="fixture.exe",
                source_service="synthetic-source",
                tlp=TLP.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
                license_restriction=None,
            ),
        )

    tool._sample_associations = SimpleNamespace(list_for_investigation=sample_associations)
    tool._samples = SimpleNamespace(list_for_subject=samples_for_subject)

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the observed sample bytes and contradictory status metadata.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:lossless-sample-bytes"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    expected_bytes_label = f"encoding=base64; byte_length={len(raw_sample)}; sha256={sample_sha256}"
    for required in (
        base64_content,
        raw_sample.hex(),
        expected_bytes_label,
        "offset 37",
        "offset 38",
        "offset 91",
        f"sha256={hashlib.sha256(bytes.fromhex('00ff1020')).hexdigest()}",
        "status=clean",
        "analysis_status=malicious",
        "EngineAlpha",
        "EngineBeta",
        "trace-fragment-1",
        mime_type,
        f"sample_id={sample_id}",
        f"association_id={association_id}",
        f"blob_id={sample_blob_id}",
        f"sha256={sample_sha256}",
        f"blob_sha256={sample_sha256}",
        f"evidence_ids={observation.id}",
        f"evidence_blob_ids={observation.blob_id}",
        f"evidence_sha256s={observation.sha256}",
        f"related_sample_sha256={contradictory_source_hash}",
        f"source_object_id={contradictory_source_hash}",
        f"length={len(raw_sample)}",
        "external_llm_allowed=true",
    ):
        assert required in prompt
    assert attacker_token in base64.b64decode(base64_content).decode("latin-1")
    assert result.output["bridge_provenance"]["redacted_secret_count"] == 0
    assert any(
        all(
            value in line
            for value in (
                f"evidence_id={observation.id}",
                f"blob_id={observation.blob_id}",
                f"association_id={association_id}",
            )
        )
        for line in prompt.splitlines()
        if "Octets observés" in line
    )
    fragment_line = next(
        line for line in prompt.splitlines() if "Octets observés" in line and "offset 91" in line
    )
    assert f"mime_type={mime_type}" in fragment_line
    assert f"sha256={hashlib.sha256(bytes.fromhex('00ff1020')).hexdigest()}" in fragment_line
    assert f"; sha256={sample_sha256}" not in fragment_line


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("representation", "material_kind"),
    [
        ("sample_blob", "operational_secret"),
        ("sample_blob", "signed_url"),
        ("byte_array", "operational_secret"),
        ("byte_array", "signed_url"),
        ("hex", "operational_secret"),
        ("hex", "signed_url"),
        ("base64", "operational_secret"),
        ("base64", "signed_url"),
    ],
)
async def test_encoded_sample_material_with_operational_secret_or_signed_url_is_refused(
    representation, material_kind
) -> None:
    investigation_id = uuid4()
    operator_secret = "autowork_fixture_" + "S" * 48
    material = (
        operator_secret
        if material_kind == "operational_secret"
        else "https://storage.invalid/sample?X-Amz-Signature=synthetic-signed-sample-url"
    )
    raw_material = material.encode("ascii")
    sample_hash = hashlib.sha256(raw_material).hexdigest()
    measured_payload: dict[str, Any]
    if representation == "hex":
        measured_payload = {"hex": [{"offset": 3, "value": raw_material.hex()}]}
    elif representation == "byte_array":
        measured_payload = {"raw_bytes": [{"offset": 3, "value": list(raw_material)}]}
    elif representation == "base64":
        measured_payload = {
            "base64": [{"offset": 3, "value": base64.b64encode(raw_material).decode("ascii")}]
        }
    else:
        measured_payload = {"content": "A sample blob is associated with this evidence."}
    observation, observation_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-encoded-secret-fixture.v1",
            "sample_sha256": sample_hash,
            **measured_payload,
        },
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=observation_raw,
    )
    tool._settings.operator_api_token = SecretStr(operator_secret)
    if representation == "sample_blob":
        association_id = uuid4()
        sample_id = uuid4()
        tool._blobs.add(
            sample_blob_id,
            sha256=sample_hash,
            size=len(raw_material),
            logical_bucket="samples-quarantine",
        )
        tool._artifact_store.values[sample_blob_id] = raw_material

        async def list_sample_associations(_id):
            return (SimpleNamespace(id=association_id, sample_sha256=sample_hash),)

        async def list_samples(_id):
            return (
                SimpleNamespace(
                    id=sample_id,
                    expected_hash=sample_hash,
                    source_object_id=None,
                    blob_id=sample_blob_id,
                    state="quarantined",
                    origin_kind="source_seed",
                    original_name="synthetic-sample.bin",
                    source_service="fixture",
                    tlp=TLP.GREEN,
                    do_not_submit=False,
                    external_llm_allowed=True,
                    license_restriction=None,
                ),
            )

        tool._sample_associations = SimpleNamespace(list_for_investigation=list_sample_associations)
        tool._samples = SimpleNamespace(list_for_subject=list_samples)

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the encoded sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key=f"operator:advisor:encoded-veto:{representation}"),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "refused"
    assert result.output["refusal_code"] == "autowork_credential_or_signed_url_detected"
    assert result.output["redacted_secret_count"] == len(result.output["secret_findings"])
    assert result.output["secret_findings"]
    assert all(item["type"] == "encoded_credential" for item in result.output["secret_findings"])
    assert all(item["location"].startswith("$") for item in result.output["secret_findings"])
    safe_output = repr(result.output)
    assert operator_secret not in safe_output
    assert "synthetic-signed-sample-url" not in safe_output


@pytest.mark.asyncio
async def test_cited_downloaded_sample_bytes_are_verified_and_sent_with_manifest_status() -> None:
    investigation_id = uuid4()
    sample_content = bytes(range(256)) * 40
    (
        tool,
        transport,
        observation,
        sample_id,
        association_id,
        blob_id,
        sample_hash,
    ) = _make_downloaded_sample_fixture(
        investigation_id,
        sample_content,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use the cited raw sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:raw-sample-observation"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert f"sample_id={sample_id}" in prompt
    assert f"association_id={association_id}" in prompt
    assert f"blob_id={blob_id}" in prompt
    assert f"sample_sha256={sample_hash}" in prompt
    assert "mime_type=application/x-synthetic-sample" in prompt
    assert "encoding=base64" in prompt
    assert "offset=0" in prompt
    assert "length=4096" in prompt
    assert "byte_length=10240" in prompt
    assert "omitted_range=[4096,10240)" in prompt
    fragment = base64.b64encode(sample_content[:4096]).decode("ascii")
    assert fragment in prompt
    assert base64.b64encode(sample_content).decode("ascii") not in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    item = next(
        row
        for row in persisted["completeness_manifest"]
        if row["kind"] == "observation" and row["id"] == str(observation.id)
    )
    statuses = {row["status"] for row in item["statuses"]}
    assert {"PRESENT_IN_CANONICAL", "IN_DOSSIER", "SENT_TO_BRIDGE"} <= statuses


@pytest.mark.asyncio
async def test_osint_policy_allows_cited_do_not_submit_sample_bytes() -> None:
    investigation_id = uuid4()
    sample_content = bytes(range(256)) * 40
    (
        tool,
        transport,
        observation,
        sample_id,
        association_id,
        blob_id,
        sample_hash,
    ) = _make_downloaded_sample_fixture(
        investigation_id,
        sample_content,
        do_not_submit=True,
    )

    context = make_osint_policy_context(
        investigation_id,
        key="operator:advisor:osint-do-not-submit-sample",
    )
    assert context.envelope is not None
    context = replace(
        context,
        envelope=replace(
            context.envelope,
            disclosure=replace(context.envelope.disclosure, do_not_submit=True),
        ),
    )
    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use the cited raw sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        context,
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert f"sample_id={sample_id}" in prompt
    assert f"association_id={association_id}" in prompt
    assert f"blob_id={blob_id}" in prompt
    assert f"sample_sha256={sample_hash}" in prompt
    assert "do_not_submit" not in prompt
    assert "length=4096" in prompt
    assert base64.b64encode(sample_content[:4096]).decode("ascii") in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=64 * 1024)
    )
    item = next(
        row
        for row in persisted["completeness_manifest"]
        if row["kind"] == "observation" and row["id"] == str(observation.id)
    )
    statuses = {row["status"] for row in item["statuses"]}
    assert {"PRESENT_IN_CANONICAL", "IN_DOSSIER", "SENT_TO_BRIDGE"} <= statuses


@pytest.mark.asyncio
async def test_osint_policy_keeps_external_llm_veto_for_do_not_submit_sample() -> None:
    investigation_id = uuid4()
    (
        tool,
        transport,
        observation,
        _sample_id,
        _association_id,
        _blob_id,
        _sample_hash,
    ) = _make_downloaded_sample_fixture(
        investigation_id,
        bytes(range(256)) * 40,
        do_not_submit=True,
        external_llm_allowed=False,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use the cited raw sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        make_osint_policy_context(
            investigation_id,
            key="operator:advisor:osint-external-veto-sample",
        ),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert result.output["failure_reason"] == "sample_disclosure_policy"
    rows = result.output["disclosure_restricted_sample_associations"]
    assert len(rows) == 1
    assert tuple(rows[0]["restriction_sources"]) == ("external_llm_disallowed",)


@pytest.mark.asyncio
async def test_human_g2_allows_cited_do_not_submit_sample_bytes() -> None:
    investigation_id = uuid4()
    sample_content = bytes(range(256)) * 40
    (
        tool,
        transport,
        observation,
        sample_id,
        association_id,
        blob_id,
        sample_hash,
    ) = _make_downloaded_sample_fixture(
        investigation_id,
        sample_content,
        do_not_submit=True,
    )
    context = make_context(
        investigation_id,
        key="operator:advisor:human-g2-do-not-submit-sample",
    )
    assert context.envelope is not None
    assert context.envelope.authority_kind is EnvelopeAuthorityKind.HUMAN_G2

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use the cited raw sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        context,
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert f"sample_id={sample_id}" in prompt
    assert f"association_id={association_id}" in prompt
    assert f"blob_id={blob_id}" in prompt
    assert f"sample_sha256={sample_hash}" in prompt
    assert "do_not_submit" not in prompt
    assert "length=4096" in prompt
    assert base64.b64encode(sample_content[:4096]).decode("ascii") in prompt
    assert base64.b64encode(sample_content).decode("ascii") not in prompt


@pytest.mark.asyncio
async def test_human_g2_envelope_do_not_submit_allows_cited_sample_bytes() -> None:
    investigation_id = uuid4()
    (
        tool,
        transport,
        observation,
        _sample_id,
        _association_id,
        _blob_id,
        _sample_hash,
    ) = _make_downloaded_sample_fixture(
        investigation_id,
        bytes(range(256)),
        do_not_submit=True,
    )
    context = make_context(
        investigation_id,
        key="operator:advisor:human-g2-envelope-do-not-submit",
    )
    assert context.envelope is not None
    assert context.envelope.authority_kind is EnvelopeAuthorityKind.HUMAN_G2
    context = replace(
        context,
        envelope=replace(
            context.envelope,
            disclosure=replace(context.envelope.disclosure, do_not_submit=True),
        ),
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use the cited raw sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        context,
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert "do_not_submit" not in prompt
    assert "length=256" in prompt
    assert base64.b64encode(bytes(range(256))).decode("ascii") in prompt


@pytest.mark.asyncio
async def test_human_g2_allows_cited_do_not_submit_sample_vt_report() -> None:
    investigation_id = uuid4()
    (
        tool,
        transport,
        observation,
        _sample_id,
        _association_id,
        _blob_id,
        sample_hash,
    ) = _make_downloaded_sample_fixture(
        investigation_id,
        bytes(range(256)),
        do_not_submit=True,
    )
    report_payload = {
        "schema": "virustotal-file-report.v1",
        "attributes": {"meaningful_name": "synthetic-report-marker"},
    }
    report_raw = json.dumps(report_payload, separators=(",", ":")).encode()
    report_blob_id = uuid4()
    tool._blobs.add(
        report_blob_id,
        sha256=hashlib.sha256(report_raw).hexdigest(),
        size=len(report_raw),
        logical_bucket="evidence",
    )
    tool._artifact_store.values[report_blob_id] = report_raw
    report_observation = replace(
        observation,
        blob_id=report_blob_id,
        sha256=hashlib.sha256(report_raw).hexdigest(),
        source="virustotal.file_report",
        source_version="vt-v3",
        object_type="sha256",
        object_id=sample_hash,
    )
    tool._observations = ObservationRepository((report_observation,))
    context = make_context(
        investigation_id,
        key="operator:advisor:human-g2-do-not-submit-vt-report",
    )
    assert context.envelope is not None
    assert context.envelope.authority_kind is EnvelopeAuthorityKind.HUMAN_G2
    context = replace(
        context,
        envelope=replace(
            context.envelope,
            disclosure=replace(context.envelope.disclosure, do_not_submit=True),
        ),
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the cited VirusTotal report observation.",
            "evidence_refs": [str(report_observation.id)],
        },
        context,
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert result.output["preflight_status"] == "READY"
    assert len(transport.calls) == 1
    assert report_observation.id in result.observation_refs
    assert "synthetic-report-marker" in transport.calls[0]["prompt"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("failure", "expected_reason"),
    [
        ("unreadable", "Required quarantined sample bytes could not be read."),
        (
            "mismatched_bytes",
            "Required quarantined sample bytes do not match the declared size or SHA-256.",
        ),
    ],
)
async def test_downloaded_sample_blob_read_failures_refuse_with_precise_reason(
    failure, expected_reason
) -> None:
    investigation_id = uuid4()
    sample_content = bytes(range(128)) * 8
    (
        tool,
        transport,
        observation,
        _sample_id,
        _association_id,
        blob_id,
        _sample_hash,
    ) = _make_downloaded_sample_fixture(investigation_id, sample_content)
    if failure == "unreadable":
        original_read_bytes = tool._artifact_store.read_bytes

        async def fail_sample_read(requested_blob_id, *, max_bytes):
            if requested_blob_id == blob_id:
                raise OSError("synthetic quarantined blob read failure")
            return await original_read_bytes(requested_blob_id, max_bytes=max_bytes)

        tool._artifact_store.read_bytes = fail_sample_read
    else:
        tool._artifact_store.values[blob_id] = bytes(reversed(sample_content))

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use the cited raw sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key=f"operator:advisor:raw-sample-{failure}"),
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.output["advisor_status"] == "technical_blocked"
    assert result.output["preflight_status"] == "TECHNICAL_BLOCKED"
    assert result.output["failure_reason"] == "required_canonical_evidence_not_in_payload"
    assert len(result.output["missing_refs"]) == 1
    missing = result.output["missing_refs"][0]
    assert str(observation.id) in missing["ref"]
    assert missing["reason"] == expected_reason
    assert transport.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "restriction",
    [
        {"external_llm_allowed": False},
        {"tlp": TLP.RED},
        {"license_restriction": "Owner prohibits external disclosure"},
    ],
)
async def test_disclosure_restricted_downloaded_sample_bytes_refuse_before_bridge(
    restriction,
) -> None:
    investigation_id = uuid4()
    (
        tool,
        transport,
        observation,
        _sample_id,
        _association_id,
        _blob_id,
        _sample_hash,
    ) = _make_downloaded_sample_fixture(
        investigation_id,
        bytes(range(256)),
        **restriction,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Use the cited raw sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:restricted-raw-sample"),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert result.output["preflight_status"] == "POLICY_BLOCKED"
    assert result.output["failure_reason"] == "sample_disclosure_policy"
    assert tuple(result.output["disclosure_restricted_refs"]) == (str(observation.id),)
    if "license_restriction" in restriction:
        assert restriction["license_restriction"] not in repr(result.output)


@pytest.mark.asyncio
async def test_sample_blob_bytes_are_included_when_they_fit_the_fragment_bound() -> None:
    investigation_id = uuid4()
    sample_content = b"synthetic sample content above the configured test bound"
    sample_hash = hashlib.sha256(sample_content).hexdigest()
    observation, observation_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-technical-bound-fixture.v1",
            "sample_sha256": sample_hash,
            "content": "Verified observation remains available while blob content is omitted.",
        },
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The verified metadata is available.", [str(observation.id)]
            ),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=observation_raw,
    )
    sample_id = uuid4()
    association_id = uuid4()
    tool._blobs.add(
        sample_blob_id,
        sha256=sample_hash,
        size=len(sample_content),
        logical_bucket="samples-quarantine",
    )
    tool._blobs.descriptors[sample_blob_id].descriptor.mime_type = "application/x-bound; v=1"
    tool._artifact_store.values[sample_blob_id] = sample_content
    sample_reads = []
    original_read_bytes = tool._artifact_store.read_bytes

    async def track_sample_read(blob_id, *, max_bytes):
        if blob_id == sample_blob_id:
            sample_reads.append(blob_id)
        return await original_read_bytes(blob_id, max_bytes=max_bytes)

    tool._artifact_store.read_bytes = track_sample_read

    async def list_sample_associations(_id):
        return (SimpleNamespace(id=association_id, sample_sha256=sample_hash),)

    async def list_samples(_id):
        return (
            SimpleNamespace(
                id=sample_id,
                expected_hash=sample_hash,
                source_object_id=None,
                blob_id=sample_blob_id,
                state="quarantined",
                origin_kind="source_seed",
                original_name="synthetic-large-sample.bin",
                source_service="fixture",
                tlp=TLP.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
                license_restriction=None,
            ),
        )

    tool._sample_associations = SimpleNamespace(list_for_investigation=list_sample_associations)
    tool._samples = SimpleNamespace(list_for_subject=list_samples)

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the sample metadata.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:sample-technical-bound"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    assert sample_reads == [sample_blob_id]
    prompt = transport.calls[0]["prompt"]
    assert f"sample_id={sample_id}" in prompt
    assert f"association_id={association_id}" in prompt
    assert "content_status=verified" in prompt
    assert base64.b64encode(sample_content).decode("ascii") in prompt


@pytest.mark.asyncio
async def test_unreferenced_restricted_sample_does_not_block_unrelated_consultation() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", "content": "Permitted unrelated evidence."},
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The unrelated evidence is usable.", [str(observation.id)]
            ),
        )
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(
        tool,
        sample_blob_id,
        "c" * 64,
        license_restriction="Owner prohibits unrelated external disclosure",
    )

    arguments = {
        "consultation_type": "plan_investigation",
        "question": "Review the unrelated permitted observation.",
        "evidence_refs": [str(observation.id)],
    }
    measured_cost = await tool.reservation_cost(arguments, make_dispatch_context(investigation_id))
    result = await tool.execute(
        arguments,
        make_context(investigation_id, key="operator:advisor:unrelated-restricted-sample"),
    )

    assert measured_cost[BudgetDimension.CONTEXT_BYTES] > 0
    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    assert "Owner prohibits unrelated external disclosure" not in transport.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_dotnet_analysis_of_do_not_submit_sample_is_allowed_for_bridge() -> None:
    investigation_id = uuid4()
    sample_hash = "c" * 64
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "dotnet-analysis-observation.v1",
            "result": {
                "methods": [
                    {
                        "name": "SyntheticRestrictedMethod",
                        "body": "SyntheticRestrictedMethodBody",
                    }
                ]
            },
        },
    )
    observation = replace(
        observation,
        object_type="dotnet_analysis",
        object_id=f"{sample_hash}:decompile:{'f' * 16}",
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("The cited analysis is available.", [str(observation.id)]),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    tool._samples = _Samples(
        [
            SimpleNamespace(
                expected_hash=sample_hash,
                source_object_id=None,
                state="quarantined",
                blob_id=uuid4(),
                tlp=TLP.GREEN,
                do_not_submit=True,
                external_llm_allowed=True,
            )
        ]
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the decompiled method.",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id, key="operator:advisor:restricted-dotnet"),
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output
    assert len(transport.calls) == 1
    assert result.output["preflight_status"] == "READY"


@pytest.mark.asyncio
async def test_do_not_submit_sample_inventory_allows_linked_finding_for_bridge() -> None:
    investigation_id = uuid4()
    sample_hash = "c" * 64
    restricted, restricted_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "strings": [{"value": "RestrictedSampleMarker", "offset": 16}],
        },
    )
    permitted, permitted_raw = make_observation(
        investigation_id,
        {
            "schema": "generic-observation.v1",
            "content": "Permitted observation content.",
        },
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="RESTRICTED_FINDING_MARKER pivot derived from the restricted sample.",
        observation_refs=(restricted.id,),
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The sample-linked finding supports this plan.", [str(restricted.id)]
            ),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=permitted,
        raw=permitted_raw,
    )
    tool._observations.observations[restricted.id] = restricted
    tool._artifact_store.values[restricted.blob_id] = restricted_raw
    tool._blobs.add(
        restricted.blob_id,
        sha256=restricted.sha256,
        size=len(restricted_raw),
        logical_bucket="evidence",
    )
    tool._findings = FindingRepository((finding,))
    tool._samples = _Samples(
        [
            SimpleNamespace(
                expected_hash=sample_hash,
                source_object_id=None,
                state="quarantined",
                blob_id=uuid4(),
                tlp=TLP.GREEN,
                do_not_submit=True,
                external_llm_allowed=True,
            )
        ]
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Plan the next pivot.",
            "evidence_refs": [str(permitted.id)],
            "finding_refs": [str(finding.id)],
        },
        make_context(investigation_id, key="operator:advisor:restricted-finding"),
    )

    assert result.status is ToolInvocationStatus.PARTIAL, result.output
    assert len(transport.calls) == 1
    assert result.output["preflight_status"] == "READY"
    assert "RESTRICTED_FINDING_MARKER" in transport.calls[0]["prompt"]


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
    assert (
        result.framework_pre_submission_proof
        is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
    )
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
    archived_call = next(
        item
        for item in tool._observations.observations.values()
        if item.object_type == "advisor_call"
    )
    archived = json.loads(
        await tool._artifact_store.read_bytes(archived_call.blob_id, max_bytes=1_000_000)
    )
    assert archived["output"]["failure_reason"] == "required_canonical_evidence_not_in_payload"
    assert archived["output"]["missing_refs"] == [
        dict(item) for item in result.output["missing_refs"]
    ]
    assert "raw sample bytes" in archived["message"]
    assert "VT report" in archived["message"]


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
    assert f"blob_id={sample_blob_id}" in prompt
    assert f"sha256={sample_sha256}" in prompt
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
    if consultation_type == "suricata_draft":
        tool._blobs.descriptors[
            observation.blob_id
        ].descriptor.logical_bucket = "investigation-evidence"
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
    assert f"evidence_ids={observation.id}" in transport.calls[0]["prompt"]
    assert f"blob_id={sample_blob_id}" in transport.calls[0]["prompt"]


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
    association_id = uuid4()
    sample_mime_type = "application/x-measurement-fixture; version=5"
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256, id=association_id),)
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
    tool._blobs.descriptors[sample_blob_id].descriptor.mime_type = sample_mime_type
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
    assert f"blob_id={sample_blob_id}" in prompt
    if expected_kind == "hex":
        fragment_sha256 = hashlib.sha256(bytes.fromhex(expected_value)).hexdigest()
        assert f"association_id={association_id}" in prompt
        assert f"mime_type={sample_mime_type}" in prompt
        assert f"fragment_sha256={fragment_sha256}" in prompt
        assert f"sample_sha256={sample_sha256}" in prompt
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
    assert len(prompt.encode("utf-8")) == result.output["bridge_provenance"]["prompt_bytes"]
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


@pytest.mark.asyncio
async def test_draft_text_assertion_ref_to_restricted_sample_is_vetoed_after_resolution() -> None:
    investigation_id = uuid4()
    sample_hash = "d" * 64
    restriction_text = "Synthetic source restriction marker"
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "strings": [{"value": "RestrictedEditorialSampleMarker", "offset": 128}],
        },
    )
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="must not be called")
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(
        tool,
        sample_blob_id,
        sample_hash,
        license_restriction=restriction_text,
    )

    result = await tool.execute(
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
                            "id": "restricted-a1",
                            "text": "Synthetic editorial assertion for offline testing.",
                            "nature": "analyse",
                            "refs": [{"kind": "observation", "id": str(observation.id)}],
                            "block_type": "uncertainties_and_next_leads",
                            "form": "body",
                            "anchor": None,
                        }
                    ],
                }
            ],
        },
        make_context(investigation_id, key="operator:advisor:restricted-editorial-reference"),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert result.output["failure_stage"] == "sample_disclosure_restricted"
    assert result.output["confirmed_non_execution"] is True
    assert (
        result.framework_pre_submission_proof
        is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
    )
    safe_output = repr(result.output)
    assert restriction_text not in safe_output
    assert "RestrictedEditorialSampleMarker" not in safe_output


@pytest.mark.asyncio
async def test_draft_text_finding_ref_to_restricted_sample_is_vetoed_after_resolution() -> None:
    investigation_id = uuid4()
    sample_hash = "d" * 64
    restriction_text = "Synthetic finding source restriction marker"
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_hash,
            "strings": [{"value": "RestrictedFindingSampleMarker", "offset": 128}],
        },
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="Restricted finding statement marker.",
        observation_refs=(observation.id,),
    )
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="must not be called")
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(
        tool,
        sample_blob_id,
        sample_hash,
        license_restriction=restriction_text,
    )
    tool._findings = FindingRepository((finding,))

    result = await tool.execute(
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
                            "id": "finding-a1",
                            "text": "Synthetic assertion citing a finding.",
                            "nature": "analyse",
                            "refs": [{"kind": "finding", "id": str(finding.id)}],
                            "block_type": "uncertainties_and_next_leads",
                            "form": "body",
                            "anchor": None,
                        }
                    ],
                }
            ],
        },
        make_context(investigation_id, key="operator:advisor:restricted-finding-reference"),
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "disclosure_restricted"
    assert result.output["failure_stage"] == "sample_disclosure_restricted"
    assert result.output["confirmed_non_execution"] is True
    assert str(finding.id) in result.output["disclosure_restricted_finding_refs"]
    association = tool._sample_associations.associations[0]
    assert result.output["disclosure_restricted_sample_count"] == 1
    assert result.output["disclosure_restricted_sample_association_count"] == 1
    assert result.output["disclosure_restricted_observation_count"] == 1
    rows = result.output["disclosure_restricted_sample_associations"]
    assert len(rows) == 1
    assert rows[0]["association_id"] == str(association.id)
    assert rows[0]["sample_sha256"] == sample_hash
    assert tuple(rows[0]["restriction_sources"]) == ("license_restriction",)
    assert tuple(rows[0]["linked_observation_refs"]) == (str(observation.id),)
    assert tuple(rows[0]["linked_finding_refs"]) == (str(finding.id),)
    safe_output = repr(result.output)
    assert restriction_text not in safe_output
    assert "RestrictedFindingSampleMarker" not in safe_output
    assert "Restricted finding statement marker" not in safe_output


@pytest.mark.asyncio
async def test_draft_text_unrelated_finding_remains_eligible() -> None:
    investigation_id = uuid4()
    restricted_hash = "d" * 64
    unrelated_hash = "e" * 64
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": unrelated_hash,
            "strings": [{"value": "PermittedFindingSampleMarker", "offset": 128}],
        },
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="Unrelated finding remains eligible for consultation.",
        observation_refs=(observation.id,),
    )
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="synthetic response")
    )
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(
        tool,
        sample_blob_id,
        restricted_hash,
        license_restriction="Synthetic restriction for an unreferenced sample",
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
                            "id": "unrelated-finding-a1",
                            "text": "Synthetic assertion citing an unrelated finding.",
                            "nature": "analyse",
                            "refs": [{"kind": "finding", "id": str(finding.id)}],
                            "block_type": "uncertainties_and_next_leads",
                            "form": "body",
                            "anchor": None,
                        }
                    ],
                }
            ],
        },
        make_context(investigation_id, key="operator:advisor:unrelated-finding-reference"),
    )

    assert len(transport.calls) == 1
    assert "Unrelated finding remains eligible" in transport.calls[0]["prompt"]
    assert "Synthetic restriction for an unreferenced sample" not in transport.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_editorial_measurements_keep_fragment_provenance_for_byte_representations(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from cti_app.application.advisor_dossier import build_advisor_dossier

    investigation_id = uuid4()
    sample_hash = "f" * 64
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", "content": "Editorial byte measurement fixture."},
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    _attach_sample_policy(tool, sample_blob_id, sample_hash)
    association_id = uuid4()
    tool._sample_associations = SimpleNamespace(
        list_for_investigation=async_get(
            (SimpleNamespace(id=association_id, sample_sha256=sample_hash),)
        )
    )
    tool._blobs.descriptors[
        sample_blob_id
    ].descriptor.mime_type = "application/x-editorial-fragment; version=2"
    expected_bytes = bytes((0, 255))
    expected_digest = hashlib.sha256(expected_bytes).hexdigest()
    measurement_rows = (
        {
            "kind": "hex",
            "value": "00ff",
            "encoding": "raw",
            "offset": 11,
            "length": 2,
            "sample_sha256": sample_hash,
        },
        {
            "kind": "base64",
            "value": "AP8=",
            "encoding": "base64",
            "offset": 22,
            "length": 2,
            "sample_sha256": sample_hash,
        },
        {
            "kind": "bytes",
            "value": expected_bytes,
            "encoding": "bytes",
            "offset": 33,
            "length": 2,
            "sample_sha256": sample_hash,
        },
        {
            "kind": "bytes",
            "value": [0, 255],
            "encoding": "bytes",
            "offset": 44,
            "length": 2,
            "sample_sha256": sample_hash,
        },
    )

    async def editorial_measurement_rows(_observation):
        return [dict(row) for row in measurement_rows]

    monkeypatch.setattr(tool, "_measurement_rows", editorial_measurement_rows)
    (
        _observation_items,
        evidence_rows,
        measured_rows,
        _finding_rows,
    ) = await tool._editorial_reference_facts(
        investigation_id=investigation_id,
        evidence=(),
        prompt_data={
            "assertions": [{"refs": [{"kind": "observation", "id": str(observation.id)}]}]
        },
    )

    assert len(measured_rows) == 4
    for row, offset, encoding in zip(
        measured_rows, (11, 22, 33, 44), ("hex", "base64", "base64", "base64"), strict=True
    ):
        assert row["offset"] == offset
        assert row["encoding"] == encoding
        assert row["byte_length"] == 2
        assert row["length"] == 2
        assert row["fragment_sha256"] == expected_digest
        assert row["evidence_id"] == str(observation.id)
        assert row["blob_id"] == str(sample_blob_id)
        assert row["association_id"] == str(association_id)
        assert row["mime_type"] == "application/x-editorial-fragment; version=2"

    dossier = build_advisor_dossier(
        {
            "evidence": evidence_rows,
            "measured_facts": measured_rows,
            "assertions": [{"refs": [{"kind": "observation", "id": str(observation.id)}]}],
        }
    )
    for offset, encoding in zip(
        (11, 22, 33, 44), ("hex", "base64", "base64", "base64"), strict=True
    ):
        assert f"offset {offset}" in dossier.text
        assert f"encoding={encoding}" in dossier.text
    assert f"fragment_sha256={expected_digest}" in dossier.text
    assert f"association_id={association_id}" in dossier.text
    assert f"evidence_id={observation.id}" in dossier.text
    assert f"blob_id={sample_blob_id}" in dossier.text
    assert "byte_length=2" in dossier.text
    assert "length=2" in dossier.text
    assert "mime_type=application/x-editorial-fragment; version=2" in dossier.text


def test_context_truncation_stops_at_sentence_or_word_and_marks_ellipsis() -> None:
    sentence = _truncate_context_text(
        "Première phrase complète. Deuxième phrase qui continue sans fin.", 30
    )
    word = _truncate_context_text("clé active matière implantale", 16)

    assert sentence == "Première phrase complète.…"
    assert word.endswith("…")
    assert word == "clé active…"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "policy",
    [
        "source_external_disabled",
        "source_do_not_submit",
        "envelope_external_disabled",
        "envelope_do_not_submit",
        "tlp_red",
    ],
)
async def test_bridge_ignores_do_not_submit_and_enforces_external_policy(policy) -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", "content": "Permitted but non-disclosable evidence."},
    )
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="should not run")
    )
    tool, _subject = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
        allowed=True,
    )
    snapshot = await tool._article_snapshots.resolve(uuid4())
    context = make_context(investigation_id)
    envelope = context.envelope
    assert envelope is not None
    if policy == "source_external_disabled":
        snapshot.external_llm_allowed = False
    elif policy == "source_do_not_submit":
        snapshot.do_not_submit = True
    elif policy == "envelope_external_disabled":
        context = replace(
            context,
            envelope=replace(
                envelope,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel.GREEN,
                    do_not_submit=False,
                    external_llm_allowed=False,
                ),
            ),
        )
    elif policy == "envelope_do_not_submit":
        context = replace(
            context,
            envelope=replace(
                envelope,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel.GREEN,
                    do_not_submit=True,
                    external_llm_allowed=True,
                ),
            ),
        )
    else:
        snapshot.tlp = "RED"
    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        context,
    )

    if policy in {"source_external_disabled", "envelope_external_disabled", "tlp_red"}:
        assert result.status is ToolInvocationStatus.POLICY_DENIED
        assert result.error_code == "POLICY_DENIED"
        assert result.output["advisor_status"] == "policy_denied"
        assert result.output["preflight_status"] == "POLICY_BLOCKED"
        assert observation.id in result.observation_refs
        assert transport.calls == []
    else:
        assert len(transport.calls) == 1
        assert result.status is not ToolInvocationStatus.POLICY_DENIED


@pytest.mark.asyncio
@pytest.mark.parametrize("document_resolves", [True, False])
async def test_transmitted_article_passage_resolves_canonical_source_document(
    document_resolves: bool,
) -> None:
    investigation_id = uuid4()

    class BindingContextTransport(Transport):
        async def build_external_effect_binding(self, **context):
            self.binding_context = context
            return None

    transport = BindingContextTransport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="Synthetic response.")
    )
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    investigation = await tool._investigations.get(investigation_id)
    snapshot = await tool._article_snapshots.resolve(investigation.synthesis_artifact_id)
    document_id = uuid4()
    blob_id = uuid4()
    blob_sha256 = "b" * 64
    snapshot.article_passages = (
        SimpleNamespace(
            claim_id="S1P1",
            heading="Résumé",
            text="S1P1. The Blackwood loader behavior was observed.",
            source_document_ids=(document_id,),
        ),
    )
    document = SimpleNamespace(
        id=document_id,
        subject_id=investigation.subject_id,
        blob_id=blob_id,
    )
    tool._source_documents = SourceDocumentRepository((document,) if document_resolves else ())
    tool._blobs.add(blob_id, sha256=blob_sha256, size=128, logical_bucket="source-documents")

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": "Review the Blackwood loader behavior.",
        },
        make_context(investigation_id, key="operator:advisor:source-document-passage"),
    )

    if document_resolves:
        assert len(transport.calls) == 1
        external_effect_context = transport.binding_context
        assert external_effect_context["transmitted_refs"] == (
            {"kind": "source_document", "id": str(document_id), "sha256": "a" * 64},
        )
        assert external_effect_context["evidence_refs"] == (
            EvidenceReference(
                kind="source_document",
                id=str(document_id),
                version=f"blob:{blob_id}",
                sha256=blob_sha256,
            ),
        )
    else:
        assert result.output["input_error_code"] == "advisor_evidence_reference_mismatch"
        assert transport.calls == []


@pytest.mark.asyncio
async def test_dispatcher_reserves_measured_prompt_bytes_before_transport_and_commits_them() -> (
    None
):
    investigation_id = uuid4()
    context = make_dispatch_context(investigation_id)
    observation, raw = make_observation(
        investigation_id,
        {"schema": "generic-observation.v1", "content": "Synthetic G2 reservation evidence."},
    )

    def verify_reservation_before_transport(call):
        prompt_bytes = len(call["prompt"].encode("utf-8"))
        reservation = next(
            item
            for item in context.budget.reservations
            if item.operation_key == context.idempotency_key
        )
        assert reservation.status.value == "reserved"
        assert reservation.amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
        assert reservation.amounts[BudgetDimension.CONTEXT_TOKENS] == (prompt_bytes + 3) // 4
        return TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "The measured reservation passed G2.", [str(observation.id)]
            ),
        )

    transport = Transport(verify_reservation_before_transport)
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    registry = InMemoryToolRegistry()
    register_investigation_advisor_tool(registry, adapter=tool)
    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments={
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        purpose="Measure and reserve the advisor prompt before transport.",
        operation_key=context.idempotency_key,
    )

    assert len(transport.calls) == 1, result
    prompt_bytes = len(transport.calls[0]["prompt"].encode("utf-8"))
    reservation = next(
        item
        for item in context.budget.reservations
        if item.operation_key == context.idempotency_key
    )
    assert reservation.status.value == "committed"
    assert reservation.amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert reservation.amounts[BudgetDimension.CONTEXT_TOKENS] == (prompt_bytes + 3) // 4
    assert reservation.committed_amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert reservation.committed_amounts[BudgetDimension.CONTEXT_TOKENS] == (prompt_bytes + 3) // 4
    assert result.budget_consumed[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert result.budget_consumed[BudgetDimension.CONTEXT_TOKENS] == (prompt_bytes + 3) // 4


@pytest.mark.asyncio
async def test_dispatcher_sends_prompt_above_context_accounting_limit() -> None:
    investigation_id = uuid4()
    context = make_dispatch_context(investigation_id, context_byte_limit=1)
    observation, raw = make_observation(
        investigation_id,
        {"schema": "context-accounting-fixture.v1", "content": "Verified prompt evidence."},
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("The evidence is available.", [str(observation.id)]),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )
    registry = InMemoryToolRegistry()
    register_investigation_advisor_tool(registry, adapter=tool)

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments={
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        purpose="Check G2 admission against the measured advisor prompt.",
        operation_key=context.idempotency_key,
    )

    assert len(transport.calls) == 1
    assert result.output.get("advisor_status") != "budget_refused"
    assert context.budget.consumed[BudgetDimension.CONTEXT_BYTES] > 1


@pytest.mark.asyncio
async def test_dispatcher_revocation_proof_stops_before_fake_advisor_transport() -> None:
    investigation_id = uuid4()
    context = make_dispatch_context(investigation_id)
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    registry = InMemoryToolRegistry()
    register_investigation_advisor_tool(registry, adapter=tool)

    async def revoked_before_dispatch(_context):
        return FrameworkPreSubmissionProof.ENVELOPE_REVOKED

    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments={"consultation_type": "plan_investigation", "evidence_refs": []},
        purpose="Exercise the final revocation gate before adapter dispatch.",
        operation_key=context.idempotency_key,
        pre_dispatch_check=revoked_before_dispatch,
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.framework_pre_submission_proof is FrameworkPreSubmissionProof.ENVELOPE_REVOKED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_revocation_during_prompt_preparation_stops_before_fake_transport() -> None:
    investigation_id = uuid4()
    context = make_dispatch_context(investigation_id)
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    registry = InMemoryToolRegistry()
    register_investigation_advisor_tool(registry, adapter=tool)
    original_put_text = tool._artifact_store.put_text
    admission_checks = 0
    revoked = False

    async def archive_and_revoke(value, *, bucket):
        nonlocal revoked
        blob_id = await original_put_text(value, bucket=bucket)
        revoked = True
        return blob_id

    async def check_current_admission(_context):
        nonlocal admission_checks
        admission_checks += 1
        return FrameworkPreSubmissionProof.ENVELOPE_REVOKED if revoked else None

    tool._artifact_store.put_text = archive_and_revoke
    result = await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments={
            "consultation_type": "plan_investigation",
            "question": "Prepare and then recheck admission before transport.",
            "evidence_refs": [],
        },
        purpose="Exercise the post-preparation current-envelope check.",
        operation_key=context.idempotency_key,
        pre_dispatch_check=check_current_admission,
    )

    assert admission_checks == 2
    assert revoked is True
    assert transport.calls == []
    assert result.output["failure_stage"] == "post_preparation_admission"
    assert result.output["failure_reason"] == "current_admission_revoked"
    assert result.framework_pre_submission_proof is None
    assert "secret" not in repr(result.output).casefold()


@pytest.mark.asyncio
async def test_service_current_envelope_recheck_preserves_needs_review_recovery() -> None:
    try:
        from tests.test_investigation_service import _Fixture
    except ImportError as exc:
        pytest.skip(f"service fixture dependencies are unavailable: {exc.name}")

    from cti_app.domain.investigation_state import Hypothesis

    fixture = _Fixture(responses=[], tools=())
    fixture.envelope = replace(fixture.envelope, allowed_providers=("bridge",))
    state = await fixture.start(
        hypotheses=(Hypothesis(id="revocation", statement="A synthetic review hypothesis."),),
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )
    investigation_id = fixture.investigation_id
    operation_key = "operator:advisor:late-revocation"
    observation, observation_raw = make_observation(
        investigation_id,
        {"schema": "revocation-fixture.v1", "content": "A verified local evidence marker."},
    )
    await fixture.observations.append_if_absent(observation)
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=observation_raw,
    )
    tool._investigations = fixture.investigations
    tool._observations = fixture.observations
    tool._findings = fixture.findings
    tool._investigation_states = fixture.states
    tool._opinions = fixture.opinions
    tool._article_snapshots = fixture.context_builder.article_snapshots
    tool._registry = fixture.registry
    tool._artifact_store.values[observation.blob_id] = observation_raw
    tool._blobs.add(
        observation.blob_id,
        sha256=observation.sha256,
        size=len(observation_raw),
        logical_bucket="investigation-evidence",
    )

    async def prepared_context(**_kwargs):
        return {
            "investigation_id": str(investigation_id),
            "consultation_type": "plan_investigation",
            "question": "Review the verified synthetic marker.",
            "article_source_ref": "article:" + "a" * 64,
            "article_sha256": "a" * 64,
            "article_summary": "A synthetic verified summary.",
            "article_passages": [],
            "open_questions": [],
            "samples": [],
            "evidence": [
                {
                    "evidence_id": str(observation.id),
                    "kind": "other",
                    "object_type": "generic",
                    "sha256": observation.sha256,
                    "source": observation.source,
                    "content_excerpts": [
                        {
                            "kind": "text",
                            "title": "Verified local evidence",
                            "text": "A verified local evidence marker.",
                        }
                    ],
                    "blob_read_verified": True,
                }
            ],
            "evidence_refs": [str(observation.id)],
            "findings": [],
            "hypotheses": [
                {
                    "id": "revocation",
                    "statement": "A synthetic review hypothesis.",
                    "status": "open",
                    "evidence_refs": [str(observation.id)],
                    "finding_refs": [],
                }
            ],
            "working_state_version": 1,
            "working_state_sha256": "f" * 64,
            "truncation": {},
        }

    tool._consultation_data = prepared_context
    fixture.service._available_capabilities = frozenset({"bridge.advisor"})
    register_investigation_advisor_tool(fixture.registry, adapter=tool)
    original_put_text = tool._artifact_store.put_text
    revocations = []

    async def archive_then_revoke(value, *, bucket):
        blob_id = await original_put_text(value, bucket=bucket)
        current = await fixture.envelopes.get(investigation_id)
        assert current is not None
        revoked = replace(
            current,
            version=current.version + 1,
            disclosure=replace(
                current.disclosure,
                do_not_submit=True,
                external_llm_allowed=False,
            ),
        )
        await fixture.envelopes.save_if_version(revoked, expected_version=current.version)
        revocations.append(revoked.version)
        return blob_id

    tool._artifact_store.put_text = archive_then_revoke
    investigation = await fixture.investigations.get(investigation_id)
    envelope = await fixture.envelopes.get(investigation_id)
    invocation, result, _refs, uncertain = await fixture.service.execute_tool_call(
        investigation=investigation,
        envelope=envelope,
        budget=envelope.budget,
        state=state,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments={
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        purpose="Recheck current admission after local prompt preparation.",
        operation_key=operation_key,
    )

    assert revocations
    assert transport.calls == []
    assert invocation.status is ToolInvocationStatus.NEEDS_REVIEW
    assert uncertain is True
    assert result.output["failure_stage"] == "post_preparation_admission"
    assert result.output["failure_reason"] == "current_admission_revoked"
    assert "secret" not in repr(result.output).casefold()
    latest_envelope = await fixture.envelopes.get(investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.status.value == "reserved"


def test_advisor_payload_redacts_credentials_embedded_in_urls() -> None:
    cleaned, findings = _redact_advisor_payload(
        {"request": "fetch https://analyst:secret@example.test/path"}
    )
    assert cleaned["request"] == "fetch [clé masquée]"
    assert [(item.location, item.secret_type) for item in findings] == [("$.request", "signed_url")]


@pytest.mark.asyncio
async def test_signed_url_in_payload_refuses_before_bridge_without_leaking_signature(
    monkeypatch,
) -> None:
    investigation_id = uuid4()
    signature = "synthetic-signature-value"
    signed_url = (
        "https://storage.invalid/sample?X-Amz-Credential=synthetic-credential&"
        f"X-Amz-Signature={signature}"
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)

    async def consultation_data(**_kwargs):
        return {
            "question": "Review the sample reference.",
            "evidence": [
                {
                    "evidence_id": str(uuid4()),
                    "source": "sample.store",
                    "object_type": "sample",
                    "sha256": "f" * 64,
                    "content_excerpts": [{"title": "Sample URL", "text": signed_url}],
                }
            ],
        }

    monkeypatch.setattr(tool, "_consultation_data", consultation_data)
    result = await tool.execute(
        {"consultation_type": "plan_investigation"}, make_context(investigation_id)
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "refused"
    assert result.output["refusal_code"] == "autowork_credential_or_signed_url_detected"
    assert result.output["redacted_secret_count"] == 1
    assert [dict(item) for item in result.output["secret_findings"]] == [
        {"location": "$.evidence[0].content_excerpts[0].text", "type": "signed_url"}
    ]
    safe_output = repr(result.output)
    assert signed_url not in safe_output
    assert signature not in safe_output


@pytest.mark.asyncio
async def test_plan_investigation_accepts_cti_paths_and_generic_url_parameters(monkeypatch) -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "synthetic-article-evidence.v1", "sample_sha256": "c" * 64},
    )
    attacker_token = "ghp_" + "a" * 36
    cti_text = (
        "Implant path /application/bin/agent and key path /root/.ssh/authorized_keys; "
        "sample path C:\\Windows\\System32\\drivers\\etc\\hosts; "
        "attacker URL https://evil.example/x?key=abc&token=def; "
        "attacker OAuth field https://evil.example/x?access_token=jeton-attaquant; "
        "credential-free reference postgresql://db.invalid:5432/cti; "
        f"GitHub token {attacker_token}."
    )
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("Review the CTI passage.", []),
        )
    )
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    async def consultation_data(**_kwargs):
        return {
            "question": "Assess the implant paths and observed network indicators.",
            "evidence": [
                {
                    "evidence_id": str(observation.id),
                    "source": observation.source,
                    "object_type": "sample",
                    "sha256": observation.sha256,
                    "content_excerpts": [{"title": "Article passage", "text": cti_text}],
                }
            ],
            "hypotheses": [{"statement": cti_text}],
        }

    monkeypatch.setattr(tool, "_consultation_data", consultation_data)
    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert len(transport.calls) == 1, (
        result.error_code,
        result.output.get("failure_reason"),
        result.output.get("secret_findings"),
    )
    assert result.error_code != "advisor_secret_field_remaining"
    assert "secret_findings" not in result.output
    assert cti_text in transport.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_dsn_with_password_refuses_with_nested_location_and_type(monkeypatch) -> None:
    investigation_id = uuid4()
    dsn = "postgresql://fixture-user:fixture-password@db.invalid:5432/cti"
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)

    async def consultation_data(**_kwargs):
        return {
            "question": "Review the connection reference.",
            "evidence": [
                {
                    "evidence_id": str(uuid4()),
                    "content_excerpts": [{"title": "Reference", "text": dsn}],
                }
            ],
        }

    monkeypatch.setattr(tool, "_consultation_data", consultation_data)
    result = await tool.execute(
        {"consultation_type": "plan_investigation"}, make_context(investigation_id)
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "refused"
    assert result.output["redacted_secret_count"] == 1
    assert [dict(item) for item in result.output["secret_findings"]] == [
        {"location": "$.evidence[0].content_excerpts[0].text", "type": "dsn"}
    ]
    assert "fixture-password" not in repr(result.output)


def test_encoded_credential_diagnostics_mask_sensitive_mapping_keys() -> None:
    safe_key_secret = "fixture-key-" + "S" * 8
    encoded_key_secret = "fixture/key-" + "T" * 8
    settings = SimpleNamespace(
        operator_api_token=SecretStr(safe_key_secret),
        operator_client_api_token=SecretStr(encoded_key_secret),
    )
    keyed_values = (
        (safe_key_secret, safe_key_secret.encode()),
        (encoded_key_secret, encoded_key_secret.encode()),
        (quote(encoded_key_secret, safe=""), encoded_key_secret.encode()),
        (encoded_key_secret.encode().hex(), encoded_key_secret.encode()),
        (base64.b64encode(encoded_key_secret.encode()).decode(), encoded_key_secret.encode()),
    )
    payload = {"nested": {key: {"raw_bytes": raw} for key, raw in keyed_values}}

    findings = _contains_encoded_operational_secrets(payload, settings=settings)

    expected = tuple(f"$.nested[<key#{index}>].raw_bytes" for index in range(len(keyed_values)))
    assert tuple(item.location for item in findings) == expected, (
        "encoded credential diagnostics exposed a mapping key"
    )
    assert all(
        safe_key_secret not in item.location and encoded_key_secret not in item.location
        for item in findings
    ), "credential appeared in an encoded finding path"


@pytest.mark.asyncio
@pytest.mark.parametrize("representation", ["raw_bytes", "hex", "base64"])
@pytest.mark.parametrize("material", ["configured_secret", "signed_url"])
async def test_utf16le_encoded_sample_credentials_and_signed_urls_refuse_locally(
    monkeypatch, representation, material
) -> None:
    secret = "autowork_fixture_utf16_" + "Q" * 48
    signed_url = (
        "https://storage.invalid/fixture?X-Amz-Credential=synthetic&"
        "X-Amz-Signature=synthetic-signature"
    )
    value = secret if material == "configured_secret" else signed_url
    encoded_bytes = value.encode("utf-16le")
    content: dict[str, object]
    if representation == "raw_bytes":
        content = {"raw_bytes": list(encoded_bytes)}
    elif representation == "hex":
        content = {"encoding": "hex", "value": encoded_bytes.hex()}
    else:
        content = {
            "encoding": "base64",
            "content_base64": base64.b64encode(encoded_bytes).decode("ascii"),
        }
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    investigation_id = uuid4()
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    tool._settings.operator_api_token = SecretStr(secret)

    async def consultation_data(**_kwargs):
        return {
            "question": "Review the synthetic verified sample fragment.",
            "evidence": [
                {
                    "evidence_id": str(uuid4()),
                    "content_excerpts": [{"kind": representation, **content}],
                }
            ],
        }

    monkeypatch.setattr(tool, "_consultation_data", consultation_data)
    result = await tool.execute(
        {"consultation_type": "plan_investigation"}, make_context(investigation_id)
    )

    assert transport.calls == []
    assert result.output["advisor_status"] == "refused"
    assert result.output["refusal_code"] == "autowork_credential_or_signed_url_detected"
    safe_output = repr(result.output)
    assert secret not in safe_output
    assert signed_url not in safe_output
    assert "synthetic-signature" not in safe_output


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

    cleaned, findings = _redact_advisor_payload(payload, settings=settings)

    encoded = json.dumps(cleaned, ensure_ascii=False)
    assert len(findings) == 2
    assert [(item.location, item.secret_type) for item in findings] == [
        ("$.operator_api_token", "configured_credential"),
        ("$.operator_token_assignment", "configured_credential"),
    ]
    assert encoded.count("[clé masquée]") == 2
    assert rc4_key in encoded
    assert attacker_token in encoded
    assert operator_secret not in encoded
    assert not _marked_secret_field_survives(cleaned, settings=settings)
    assert _marked_secret_field_survives(
        {"operator_api_token": "unredacted test sentinel"}, settings=settings
    )


@pytest.mark.asyncio
async def test_autowork_credentials_in_payload_refuse_before_bridge_without_leaking_values(
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
                            "text": (
                                f"Clé RC4: {rc4_key}; jeton GitHub extrait: {attacker_token}; "
                                f"jeton opérateur cité: {operator_secret}."
                            ),
                        }
                    ],
                    "raw_base64": base64.b64encode(operator_secret.encode()).decode("ascii"),
                }
            ],
            "operator_api_token": operator_secret,
        }

    monkeypatch.setattr(tool, "_consultation_data", consultation_data)
    result = await tool.execute(
        {"consultation_type": "plan_investigation"}, make_context(investigation_id)
    )

    assert transport.calls == []
    assert result.status is ToolInvocationStatus.FAILED
    assert result.output["advisor_status"] == "refused"
    assert result.output["confirmed_non_execution"] is True
    assert result.output["redacted_secret_count"] == 3, (
        result.output["redacted_secret_count"],
        [dict(item) for item in result.output["secret_findings"]],
    )
    assert [dict(item) for item in result.output["secret_findings"]] == [
        {
            "location": "$.evidence[0].raw_base64",
            "type": "encoded_credential",
        },
        {
            "location": "$.evidence[0].content_excerpts[0].text",
            "type": "configured_credential",
        },
        {"location": "$.operator_api_token", "type": "configured_credential"},
    ]
    safe_output = repr(result.output)
    assert operator_secret not in safe_output
    assert rc4_key not in safe_output
    assert attacker_token not in safe_output


@pytest.mark.asyncio
async def test_advisor_refuses_and_persists_when_marked_secret_survives(monkeypatch) -> None:
    import cti_app.application.investigation_advisor_tools as advisor_tools_module

    investigation_id = uuid4()
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    monkeypatch.setattr(
        advisor_tools_module,
        "_redact_advisor_payload",
        lambda _payload, settings=None: ({"operator_api_token": "test sentinel"}, ()),
    )

    result = await tool.execute(
        {"consultation_type": "plan_investigation"}, make_context(investigation_id)
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_secret_field_remaining"
    assert (
        result.framework_pre_submission_proof
        is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
    )
    assert result.output["refusal_code"] == "autowork_credential_or_signed_url_detected"
    assert result.output["redacted_secret_count"] == 1
    assert [dict(item) for item in result.output["secret_findings"]] == [
        {"location": "$.operator_api_token", "type": "configured_credential"}
    ]
    assert "test sentinel" not in repr(result.output)
    assert transport.calls == []
    call = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(await tool._artifact_store.read_bytes(call.blob_id, max_bytes=16 * 1024))
    assert persisted["advisor_status"] == "refused"
    assert persisted["bridge_turns_consumed"] == 0
    assert persisted["redacted_secret_count"] == 1
    assert persisted["secret_findings"] == [
        {"location": "$.operator_api_token", "type": "configured_credential"}
    ]
    assert persisted["output"]["secret_findings"] == persisted["secret_findings"]


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
    assert (
        len(transport.calls[0]["prompt"].encode("utf-8"))
        == result.output["bridge_provenance"]["batches"][0]["prompt_bytes"]
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

    async def needs_review(
        arguments,
        context,
        *,
        _editorial_batch_child=False,
        _reservation_operation_key=None,
        _execution_timeout_seconds=None,
        _execution_deadline_monotonic=None,
        _bridge_turns_remaining=None,
    ):
        submitted.append((arguments, context))
        return ToolResult(
            ToolInvocationStatus.NEEDS_REVIEW,
            error_code="advisor_result_ambiguous",
            output={"advisor_status": "needs_review", "usable": False},
        )

    tool.execute = needs_review
    units = [{"id": f"U{index}"} for index in range(1, 4)]
    context = make_context(
        investigation_id,
        key="operator:advisor:cursor",
        bridge_turns=3,
    )

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
async def test_editorial_batch_keeps_prior_consumption_when_remaining_budget_is_too_small() -> None:
    investigation_id = uuid4()
    tool, _sample_blob = make_tool(
        investigation_id=investigation_id,
        transport=Transport(None),
    )
    submitted: list[tuple[Mapping[str, Any], int | None]] = []

    async def succeed_first(
        arguments,
        context,
        *,
        _editorial_batch_child=False,
        _reservation_operation_key=None,
        _execution_timeout_seconds=None,
        _execution_deadline_monotonic=None,
        _bridge_turns_remaining=None,
    ):
        submitted.append((arguments, _execution_timeout_seconds))
        return ToolResult(
            ToolInvocationStatus.SUCCEEDED,
            budget_consumed={
                BudgetDimension.BRIDGE_TURNS: 1,
                BudgetDimension.TOOL_SECONDS: 3_541,
            },
            output={
                "advisor_status": "succeeded",
                "consultation_type": "draft_text",
                "usable": True,
                "blocks": [{"unit_id": "U1"}],
            },
        )

    tool.execute = succeed_first
    units = [{"id": "U1"}, {"id": "U2"}]
    result = await tool._execute_editorial_batches(
        {"consultation_type": "draft_text"},
        make_context(
            investigation_id,
            key="operator:advisor:budget-exhaustion",
            bridge_turns=2,
        ),
        units,
        maximum_units=1,
        execution_timeout_seconds=3_600,
    )

    assert len(submitted) == 1
    assert submitted[0][0]["units"] == [units[0]]
    assert result.status is ToolInvocationStatus.PARTIAL
    assert result.budget_consumed[BudgetDimension.TOOL_SECONDS] == 3_541
    assert result.budget_consumed[BudgetDimension.TOOL_SECONDS] <= 3_600
    assert result.output["advisor_status"] == "partial"
    assert result.output["failure_stage"] == "budget_admission"
    assert result.output["failure_reason"] == "execution_window_below_minimum_before_next_batch"
    assert result.output["confirmed_non_execution"] is False
    assert result.framework_pre_submission_proof is None
    assert result.output["unprocessed_unit_ids"] == ("U2",)


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
async def test_large_prompt_reservation_is_persisted_before_fake_transport() -> None:
    try:
        from tests.test_investigation_service import _Fixture
    except ImportError as exc:
        pytest.skip(f"service fixture dependencies are unavailable: {exc.name}")

    fixture = _Fixture(responses=[], tools=())
    fixture.envelope = replace(fixture.envelope, allowed_providers=("bridge",))
    from cti_app.domain.investigation_state import Hypothesis

    state = await fixture.start(
        hypotheses=(Hypothesis(id="large-sample", statement="The verified sample is relevant."),),
        execution_mode=InvestigationExecutionMode.OPERATOR_LED,
    )
    investigation_id = fixture.investigation_id
    operation_key = "operator:advisor:d7-large-sample-context"
    sample_bytes = bytes(index % 251 for index in range(3_200_000))
    sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()
    sample_id = uuid4()
    association_id = uuid4()
    blob_id = uuid4()
    observation, observation_raw = make_observation(
        investigation_id,
        {
            "schema": "sample-large-placeholder.v1",
            "sample_sha256": sample_sha256,
            "content": "A verified large sample placeholder is present.",
        },
    )
    await fixture.observations.append_if_absent(observation)
    response = TurnSubmission(
        TurnSubmissionStatus.SUCCEEDED,
        output_text=advisor_response("The sample placeholder was received.", [str(observation.id)]),
    )

    class DurableReservationTransport(Transport):
        async def submit(self, **kwargs):
            self.calls.append(kwargs)
            prompt_bytes = len(kwargs["prompt"].encode("utf-8"))
            current_invocation = await fixture.invocations.get_by_operation_key(operation_key)
            current_envelope = await fixture.envelopes.get(investigation_id)
            assert current_invocation is not None
            assert current_invocation.status is ToolInvocationStatus.RUNNING
            assert current_invocation.reserved_budget[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
            reservation = next(
                item
                for item in current_envelope.budget.reservations
                if item.operation_key == operation_key
            )
            assert reservation.status.value == "reserved"
            assert reservation.amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
            assert reservation.amounts[BudgetDimension.CONTEXT_TOKENS] == (prompt_bytes + 3) // 4
            return response

    transport = DurableReservationTransport(response)
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)
    tool._investigations = fixture.investigations
    tool._observations = fixture.observations
    tool._findings = fixture.findings
    tool._investigation_states = fixture.states
    tool._opinions = fixture.opinions
    tool._article_snapshots = fixture.context_builder.article_snapshots
    tool._registry = fixture.registry
    tool._artifact_store.values[observation.blob_id] = observation_raw
    tool._blobs.add(
        observation.blob_id,
        sha256=observation.sha256,
        size=len(observation_raw),
        logical_bucket="investigation-evidence",
    )
    tool._artifact_store.values[blob_id] = sample_bytes
    tool._blobs.add(
        blob_id,
        sha256=sample_sha256,
        size=len(sample_bytes),
        logical_bucket="samples-quarantine",
    )
    tool._blobs.descriptors[
        blob_id
    ].descriptor.mime_type = "application/x-synthetic-sample; fixture=large"

    class LargeSampleAssociations:
        async def list_for_investigation(self, _investigation_id):
            return (
                SimpleNamespace(
                    id=association_id,
                    sample_sha256=sample_sha256,
                    qualification="confirmed",
                ),
            )

    class LargeSamples:
        async def list_for_subject(self, _subject_id):
            return (
                SimpleNamespace(
                    id=sample_id,
                    expected_hash=sample_sha256,
                    source_object_id=None,
                    state="quarantined",
                    blob_id=blob_id,
                    tlp=TLP.GREEN,
                    do_not_submit=False,
                    external_llm_allowed=True,
                    license_restriction=None,
                    original_name="synthetic-large.bin",
                    source_service="fixture",
                    origin_kind="source_seed",
                ),
            )

    tool._sample_associations = LargeSampleAssociations()
    tool._samples = LargeSamples()
    fixture.service._available_capabilities = frozenset({"bridge.advisor"})
    register_investigation_advisor_tool(fixture.registry, adapter=tool)
    investigation = await fixture.investigations.get(investigation_id)
    envelope = await fixture.envelopes.get(investigation_id)
    invocation, result, _refs, uncertain = await fixture.service.execute_tool_call(
        investigation=investigation,
        envelope=envelope,
        budget=envelope.budget,
        state=state,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments={
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id)],
        },
        purpose="Review an authorized large sample placeholder with exact G2 accounting.",
        operation_key=operation_key,
    )

    assert result.status is ToolInvocationStatus.SUCCEEDED, result.output.get("missing_refs")
    assert uncertain is False
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert len(prompt.encode("utf-8")) < 20_000
    assert base64.b64encode(sample_bytes[:4096]).decode("ascii") in prompt
    assert "omitted_range=[4096,3200000)" in prompt
    assert "length=4096" in prompt
    assert "byte_length=3200000" in prompt
    assert base64.b64encode(sample_bytes).decode("ascii") not in prompt
    assert sample_sha256 in prompt
    assert str(sample_id) in prompt
    assert str(association_id) in prompt
    assert str(blob_id) in prompt
    assert "mime_type=application/x-synthetic-sample; fixture=large" in prompt
    assert "length=4096" in prompt
    prompt_bytes = len(prompt.encode("utf-8"))
    prompt_tokens = (prompt_bytes + 3) // 4
    assert invocation.reserved_budget[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert invocation.reserved_budget[BudgetDimension.CONTEXT_TOKENS] == prompt_tokens
    assert invocation.consumed_budget[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert invocation.consumed_budget[BudgetDimension.CONTEXT_TOKENS] == prompt_tokens
    latest_envelope = await fixture.envelopes.get(investigation_id)
    reservation = next(
        item for item in latest_envelope.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert reservation.amounts[BudgetDimension.CONTEXT_TOKENS] == prompt_tokens
    assert reservation.committed_amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert reservation.committed_amounts[BudgetDimension.CONTEXT_TOKENS] == prompt_tokens


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
            TurnSubmission(
                TurnSubmissionStatus.PRE_SUBMISSION_RETRY_EXHAUSTED,
                error_code="bridge_pre_submission_retry_exhausted",
                submission_state="pre_submission",
            ),
            ToolInvocationStatus.UNAVAILABLE,
            "advisor_unavailable",
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
    if submission.failure_stage in {
        "pre_transport_validation",
        "external_effect_authority",
        "binding_missing",
        "binding_invalid",
        "coordinator_validation_refused",
        "coordinator_consume_refused",
        "secret_preflight",
        "sample_disclosure_restricted",
    }:
        assert (
            result.framework_pre_submission_proof
            is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
        )
        assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 0
    else:
        assert result.framework_pre_submission_proof is None
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_advisor_does_not_parse_persisted_output_for_ambiguous_rate_limit() -> None:
    investigation_id = uuid4()
    observation, raw_evidence = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "a" * 64,
            "strings": [{"value": "MeasuredSampleMarker", "offset": 128}],
        },
    )
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
        error_code="bridge_rate_limited",
        submission_state="external_state_unknown",
    )

    transport = Transport(submission)
    tool, _ = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw_evidence,
    )
    result = await tool.execute(
        {
            "consultation_type": "pivot_strategy",
            "evidence_refs": [str(observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.NEEDS_REVIEW
    assert result.output["advisor_status"] == "needs_review"
    assert result.framework_pre_submission_proof is None
    assert "blocks" not in result.output
    assert result.output["bridge_turns_consumed"] == 1
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert len(transport.calls) == 1


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
    assert (
        result.framework_pre_submission_proof
        is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
    )
    assert transport.calls == []


@pytest.mark.asyncio
async def test_general_contradiction_search_reaches_bridge_without_a_refuted_finding() -> None:
    investigation_id = uuid4()
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                "Review contradictions in the loaded passage.", ["article:" + "a" * 64]
            ),
        )
    )
    tool, _sample_blob_id = make_tool(investigation_id=investigation_id, transport=transport)

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "question": (
                "Plan the next investigation step for Blackwood and search for contradictions."
            ),
        },
        make_context(investigation_id),
    )

    assert result.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert "Aucune réfutation connue" in prompt
    assert result.output["bridge_provenance"]["preflight_status"] == "READY"


@pytest.mark.asyncio
async def test_missing_evidence_reference_returns_typed_pre_transport_refusal() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(investigation_id, {"schema": "offline-test.v1"})
    missing_observation_id = uuid4()
    transport = Transport(None)
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=observation,
        raw=raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(observation.id), str(missing_observation_id)],
        },
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["advisor_status"] == "invalid_input"
    assert result.output["input_error_code"] == "advisor_input_rejected"
    assert result.output["failure_stage"] == "pre_transport_validation"
    assert result.output["confirmed_non_execution"] is True
    assert result.output["provider_usage"] == {"status": "not_submitted"}
    assert (
        result.framework_pre_submission_proof
        is FrameworkPreSubmissionProof.EXTERNAL_EFFECT_PRE_SEND_REFUSAL
    )
    assert transport.calls == []


@pytest.mark.asyncio
async def test_cross_investigation_evidence_reference_is_rejected_before_bridge() -> None:
    investigation_id = uuid4()
    foreign_observation, foreign_raw = make_observation(uuid4(), {"content": "foreign evidence"})
    transport = Transport(None)
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=foreign_observation,
        raw=foreign_raw,
    )

    result = await tool.execute(
        {
            "consultation_type": "plan_investigation",
            "evidence_refs": [str(foreign_observation.id)],
        },
        make_context(investigation_id),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_input_invalid"
    assert result.output["failure_stage"] == "pre_transport_validation"
    assert result.output["confirmed_non_execution"] is True
    assert transport.calls == []


def test_retry_refusal_keeps_prior_post_out_of_pre_submission_proof() -> None:
    investigation_id = uuid4()
    tool, _sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=Transport(None),
    )
    submission = TurnSubmission(
        TurnSubmissionStatus.UNAVAILABLE,
        error_code="external_effect_authority_required",
        submission_state="pre_submission",
        failure_stage="external_effect_authority",
        prior_submission_made=True,
    )

    result = tool._submission_result(
        submission,
        prompt="Synthetic bounded consultation prompt.",
        evidence_refs=(),
        source_refs=(),
        input_bytes=256,
        consultation_type="review_text",
        elapsed_seconds=1,
        available_tools={},
        available_tool_schemas={},
        prompt_data={},
    )
    result = tool._finish(make_context(investigation_id, key="advisor:retry-refusal"), result)

    assert result.output["prior_submission_made"] is True
    assert result.output["confirmed_non_execution"] is False
    assert result.output["bridge_turns_consumed"] == 1
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    assert result.framework_pre_submission_proof is None
