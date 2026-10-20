from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.investigation_advisor_tools import (
    ADVISOR_RESPONSE_SCHEMA,
    MAX_ADVISOR_INPUT_BYTES,
    InvestigationAdvisorTool,
    _safe_text,
)
from cti_app.application.investigation_tools import ToolExecutionContext
from cti_app.domain.analyst_opinion import OpinionBlockType
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    InvestigationBudget,
    ToolInvocationStatus,
)


class Transport:
    def __init__(self, submission):
        self.submission = submission
        self.calls = []

    async def submit(self, **kwargs):
        self.calls.append(kwargs)
        return self.submission


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
    subject_id = uuid4()
    sample_blob_id = uuid4()
    snapshot = SimpleNamespace(
        external_llm_allowed=allowed,
        do_not_submit=not allowed,
        tlp="clear",
        synthesis_sha256="a" * 64,
        article=SimpleNamespace(short_summary="A bounded summary.", open_questions=("Q1",)),
    )
    tool = InvestigationAdvisorTool(
        transport=transport,
        settings=SimpleNamespace(
            openai_bridge_min_submission_interval_seconds=0,
            openai_bridge_submission_jitter_fraction=0,
            operator_advisor_min_consultation_interval_seconds=0,
            operator_advisor_max_turns_per_investigation=4,
            operator_advisor_checkpoint_tool_threshold=12,
        ),
        investigations=SimpleNamespace(get=lambda _id: None),
        observations=ObservationRepository((observation,) if observation else ()),
        findings=SimpleNamespace(list_for_investigation=async_get(())),
        opinions=SimpleNamespace(),
        sample_associations=SimpleNamespace(),
        samples=SimpleNamespace(),
        blobs=SimpleNamespace(),
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
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "c" * 64, "strings": []},
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

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert len(transport.calls) == 1
    prompt = transport.calls[0]["prompt"]
    assert "args_hint must be a JSON object" in prompt
    assert '"args_hint":{}' in prompt
    assert '"required_arguments":["sample_sha256"]' in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
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
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "c" * 64, "strings": []},
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

    assert result.status is ToolInvocationStatus.SUCCEEDED
    prompt = transport.calls[0]["prompt"]
    assert '"required_arguments":["object_type","object_id"]' in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
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
    }
    observation, raw = make_observation(investigation_id, measured)
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response("rule candidate", [str(observation.id)]),
        )
    )
    tool, sample_blob_id = make_tool(
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
    tool._sample_associations.list_for_investigation = async_get(
        (SimpleNamespace(sample_sha256=sample_sha256),)
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
    tool._blobs.get = async_get(
        SimpleNamespace(
            descriptor=SimpleNamespace(
                sha256=sample_sha256,
                logical_bucket="samples-quarantine",
            )
        )
    )

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
                }
            ],
        },
        make_context(investigation_id),
    )

    prompt = transport.calls[0]["prompt"]
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert str(sample_blob_id) in prompt
    assert '"offset":128' in prompt
    assert "ignored-without-offset" not in prompt
    assert "UNTRUSTED ADVISORY" in result.output["advisory_text"]
    assert result.output["untrusted"] is True
    assert result.budget_consumed[BudgetDimension.BRIDGE_TURNS] == 1
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["schema"] == "operator-advisor-call.v1"
    assert persisted["input_observation_refs"] == [str(observation.id)]
    assert persisted["output"]["schema"] == ADVISOR_RESPONSE_SCHEMA
    assert persisted["output"]["alternative_hypotheses"][0]["basis_refs"] == [str(observation.id)]
    assert persisted["output"]["discriminating_experiment"]["basis_refs"] == [str(observation.id)]
    assert persisted["output"]["discarded_lead"]["basis_refs"] == [str(observation.id)]
    assert persisted["cost"]["bridge_turns"] == 1
    assert persisted["cost"]["context_bytes"] > 0
    assert persisted["cost"]["context_units"] > 0


@pytest.mark.asyncio
async def test_plan_investigation_compacts_six_refs_to_actual_prompt_budget() -> None:
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

    async def get_sample_blob(blob_id):
        sample_hash = next(value for value, key in sample_blobs.items() if key == blob_id)
        return SimpleNamespace(
            descriptor=SimpleNamespace(
                sha256=sample_hash,
                size=4096,
                mime_type="application/octet-stream",
                logical_bucket="samples-quarantine",
            )
        )

    tool._blobs.get = get_sample_blob
    tool._findings = SimpleNamespace(
        list_for_investigation=async_get(
            tuple(
                SimpleNamespace(
                    id=uuid4(),
                    kind=SimpleNamespace(value="network_behaviour"),
                    assessment=SimpleNamespace(value="inconclusive"),
                    statement=(f"Measured finding {index}: " + "x" * 900),
                    observation_refs=(observations[index].id,),
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
    assert len(prompt.encode("utf-8")) <= MAX_ADVISOR_INPUT_BYTES
    assert all(str(item.id) in prompt for item in observations)
    assert "CandidateLiteral0" in prompt
    assert "x" * 500 not in prompt


@pytest.mark.asyncio
async def test_minimal_plan_assembles_evidence_and_parses_structured_output() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": "c" * 64,
            "strings": [{"value": "CreateMutexW", "offset": 128}],
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
    tool._blobs.get = async_get(
        SimpleNamespace(
            descriptor=SimpleNamespace(
                sha256="c" * 64,
                size=4096,
                mime_type="application/vnd.microsoft.portable-executable",
                logical_bucket="samples-quarantine",
            )
        )
    )

    result = await tool.execute(
        {"consultation_type": "plan_investigation", "question": "Quel pivot ensuite ?"},
        make_context(
            investigation_id,
            key="operator:advisor:minimal-plan",
            capabilities=("local.sample_analysis",),
        ),
    )

    prompt = transport.calls[0]["prompt"]
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert str(observation.id) in prompt
    assert str(sample_blob_id) in prompt
    assert '"status":"evidence_recorded"' in prompt
    assert "Quel pivot ensuite ?" in prompt
    assert result.output["summary"].startswith("Comparer")
    assert result.output["recommended_next_tools"][0]["tool"] == "sample.analyze"
    assert result.output["recommended_next_tools"][0]["schema_version"] == "1"
    assert result.output["recommended_next_tools"][0]["basis_observation_refs"] == (
        str(observation.id),
    )
    assert result.output["untrusted"] is True


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
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "f" * 64, "strings": []},
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

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_output_invalid"
    assert result.output["advisor_status"] == "output_invalid"
    rejection = result.output["validation_rejection"]
    assert rejection["code"] == "invalid_reference_list"
    assert rejection["path"] == "$.alternative_hypotheses[0].basis_refs"
    assert rejection["normalization_status"] == "partially_normalized"
    assert rejection["normalizations"][0].endswith(unknown_ref)
    assert len(transport.calls) == 1


@pytest.mark.asyncio
async def test_unknown_basis_refs_are_removed_and_traced_when_valid_refs_remain() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "a" * 64, "strings": []},
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

    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["normalization_status"] == "partially_normalized"
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
    assert f'Complete allowed observation UUID references: ["{valid_ref}"]' in prompt
    assert all(unknown_ref not in prompt for unknown_ref in unknown_refs)
    example_prefix = (
        "Complete valid advisor-response.v2 example using an allowed basis reference "
        "and, when an observation and tool are available, the first listed tool/version: "
    )
    example_line = next(line for line in prompt.splitlines() if line.startswith(example_prefix))
    example = json.loads(example_line.removeprefix(example_prefix).removesuffix("."))
    assert example["schema"] == ADVISOR_RESPONSE_SCHEMA
    assert example["alternative_hypotheses"][0]["basis_refs"] == [valid_ref]
    assert example["recommended_next_tools"][0]["tool"] == "sample.analyze"
    assert example["recommended_next_tools"][0]["schema_version"] == "1"

    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["output"]["normalization_status"] == "partially_normalized"
    assert persisted["output"]["normalizations"] == list(normalizations)


@pytest.mark.asyncio
async def test_recommendation_without_any_valid_observation_ref_stays_rejected_and_traced() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "b" * 64, "strings": []},
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

    rejection = result.output["validation_rejection"]
    assert result.status is ToolInvocationStatus.FAILED
    assert rejection["code"] == "observation_reference_required"
    assert rejection["path"] == "$.recommended_next_tools[0].basis_refs"
    assert rejection["normalization_status"] == "partially_normalized"
    assert rejection["normalizations"][0].endswith(unknown_ref)
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    persisted_rejection = persisted["output"]["validation_rejection"]
    assert persisted_rejection["code"] == rejection["code"]
    assert persisted_rejection["path"] == rejection["path"]
    assert persisted_rejection["normalization_status"] == rejection["normalization_status"]
    assert persisted_rejection["normalizations"] == list(rejection["normalizations"])


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
            input_block = prompt.split("<UNTRUSTED_ADVISORY_INPUT>\n", 1)[1].split(
                "\n</UNTRUSTED_ADVISORY_INPUT>", 1
            )[0]
            article_ref = json.loads(input_block)["article_source_ref"]
            return TurnSubmission(
                TurnSubmissionStatus.SUCCEEDED,
                output_text=advisor_response(
                    "L'article fournit un contexte de départ.", [article_ref]
                ),
            )

    transport = ArticleBasisTransport()
    tool, _sample_blob = make_tool(investigation_id=investigation_id, transport=transport)

    result = await tool.execute(
        {"consultation_type": "plan_investigation"},
        make_context(investigation_id),
    )

    prompt = transport.calls[0]["prompt"]
    input_block = prompt.split("<UNTRUSTED_ADVISORY_INPUT>\n", 1)[1].split(
        "\n</UNTRUSTED_ADVISORY_INPUT>", 1
    )[0]
    article_ref = json.loads(input_block)["article_source_ref"]
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["alternative_hypotheses"][0]["basis_refs"] == (article_ref,)
    assert result.output["discriminating_experiment"]["basis_refs"] == (article_ref,)
    assert result.output["discarded_lead"]["basis_refs"] == (article_ref,)
    assert result.output["recommended_next_tools"] == ()
    allowed_refs_line = (
        f"Complete allowed references (observation UUIDs and article_source_ref when sent): "
        f'["{article_ref}"]'
    )
    assert allowed_refs_line in prompt
    assert "Never cite an article" not in prompt

    example_prefix = (
        "Complete valid advisor-response.v2 example using an allowed basis reference "
        "and, when an observation and tool are available, the first listed tool/version: "
    )
    example_line = next(line for line in prompt.splitlines() if line.startswith(example_prefix))
    example = json.loads(example_line.removeprefix(example_prefix).removesuffix("."))
    assert example["alternative_hypotheses"][0]["basis_refs"] == [article_ref]
    assert example["discriminating_experiment"]["basis_refs"] == [article_ref]
    assert example["discarded_lead"]["basis_refs"] == [article_ref]
    assert example["recommended_next_tools"] == []


@pytest.mark.asyncio
async def test_article_source_ref_cannot_support_a_tool_recommendation() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "e" * 64, "strings": []},
    )

    class ArticleRecommendationTransport:
        def __init__(self):
            self.calls = []

        async def submit(self, **kwargs):
            self.calls.append(kwargs)
            prompt = kwargs["prompt"]
            input_block = prompt.split("<UNTRUSTED_ADVISORY_INPUT>\n", 1)[1].split(
                "\n</UNTRUSTED_ADVISORY_INPUT>", 1
            )[0]
            article_ref = json.loads(input_block)["article_source_ref"]
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
        },
        make_context(investigation_id, capabilities=("local.sample_analysis",)),
    )

    article_ref = json.loads(
        transport.calls[0]["prompt"]
        .split("<UNTRUSTED_ADVISORY_INPUT>\n", 1)[1]
        .split("\n</UNTRUSTED_ADVISORY_INPUT>", 1)[0]
    )["article_source_ref"]
    assert result.status is ToolInvocationStatus.FAILED
    rejection = result.output["validation_rejection"]
    assert rejection["code"] == "observation_reference_required"
    assert rejection["path"] == "$.recommended_next_tools[0].basis_refs"
    assert rejection["normalization_status"] == "partially_normalized"
    assert rejection["normalizations"][0].endswith(article_ref)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("basis_refs", "expected_code"),
    [
        ("not-a-list", "invalid_reference_list"),
        (["duplicate-placeholder", "duplicate-placeholder"], "invalid_reference_list"),
        (["too-many-placeholder"] * 9, "invalid_reference_list"),
    ],
)
async def test_malformed_basis_ref_lists_remain_rejected(basis_refs, expected_code) -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "c" * 64, "strings": []},
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
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=advisor_response(
                'rule Sample { strings: $a = "CreateMutexW" condition: $a }',
                [str(observation.id)],
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
    tool._blobs.get = async_get(
        SimpleNamespace(
            descriptor=SimpleNamespace(
                sha256=sample_sha256,
                logical_bucket="samples-quarantine",
            )
        )
    )

    result = await tool.execute(
        {"consultation_type": "yara_draft"},
        make_context(investigation_id, key="operator:advisor:minimal-yara"),
    )

    prompt = transport.calls[0]["prompt"]
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert str(sample_blob_id) in prompt
    assert '"offset":128' in prompt
    assert '"offset":384' in prompt
    assert "CreateMutexW" in result.output["summary"]


@pytest.mark.asyncio
async def test_invalid_structured_advisor_output_is_explicit_and_not_retried() -> None:
    investigation_id = uuid4()
    raw_output = "{"
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=raw_output))
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)

    result = await tool.execute(
        {"consultation_type": "plan_investigation"},
        make_context(investigation_id, key="operator:advisor:invalid-output"),
    )

    assert result.status is ToolInvocationStatus.FAILED
    assert result.error_code == "advisor_output_invalid"
    assert result.output["advisor_status"] == "output_invalid"
    assert result.output["validation_rejection"] == {
        "code": "json_object_required",
        "path": "$",
    }
    assert len(result.output["response_excerpt"]) <= 512
    assert json.loads(result.output["response_excerpt"]) == {
        "characters": 1,
        "format": "json_like",
    }
    assert result.output["response_sha256"] == hashlib.sha256(raw_output.encode()).hexdigest()
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["output"]["validation_rejection"] == result.output["validation_rejection"]
    assert persisted["output"]["response_sha256"] == result.output["response_sha256"]
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
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=raw_output))
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)

    result = await tool.execute(
        {"consultation_type": "plan_investigation"},
        make_context(investigation_id, key="operator:advisor:schema-diagnostic"),
    )

    assert result.output["validation_rejection"] == {
        "code": "schema_mismatch",
        "path": "$.schema",
    }
    assert "PRIVATE-RESPONSE-MARKER" not in result.output["response_excerpt"]
    assert result.output["response_sha256"] == hashlib.sha256(raw_output.encode()).hexdigest()


@pytest.mark.asyncio
async def test_oversized_advisor_output_is_rejected_with_bounded_diagnostic() -> None:
    investigation_id = uuid4()
    raw_output = "private-response-marker " * 1_000
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=raw_output))
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)

    result = await tool.execute(
        {"consultation_type": "plan_investigation"},
        make_context(investigation_id, key="operator:advisor:oversized-output"),
    )

    assert result.output["validation_rejection"] == {
        "code": "response_too_large",
        "path": "$",
    }
    assert len(result.output["response_excerpt"]) <= 512
    assert "private-response-marker" not in result.output["response_excerpt"]
    assert result.output["response_bytes"] == len(raw_output.encode())
    assert result.output["response_sha256"] == hashlib.sha256(raw_output.encode()).hexdigest()


@pytest.mark.asyncio
async def test_review_text_passes_cited_block_refs_without_expanding_evidence_payload() -> None:
    investigation_id = uuid4()
    observation, raw = make_observation(
        investigation_id,
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "7" * 64, "strings": []},
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

    assert result.status is ToolInvocationStatus.SUCCEEDED
    prompt = transport.calls[0]["prompt"]
    assert "Le paragraphe cite une observation existante." in prompt
    assert f'"evidence_refs":["{observation.id}"]' in prompt
    assert f'"opinion_id":"{opinion_id}"' in prompt
    assert '"opinion_version":14' in prompt
    assert f'"opinion_sha256":"{opinion_sha256}"' in prompt
    assert source_ref not in prompt
    assert '"schema":"sample-obfuscated-strings.v1"' not in prompt
    advisor_observation = next(
        item
        for item in tool._observations.observations.values()
        if item.source == "advisor.consult"
    )
    persisted = json.loads(
        await tool._artifact_store.read_bytes(advisor_observation.blob_id, max_bytes=16 * 1024)
    )
    assert persisted["input_observation_refs"] == [str(observation.id)]
    assert persisted["input_source_refs"] == [f"article:{'a' * 64}", source_ref]


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


def test_advisor_brief_redacts_credentials_embedded_in_urls() -> None:
    assert _safe_text("fetch https://analyst:secret@example.test/path", 200) == (
        "fetch https://[REDACTED]@example.test/path"
    )


@pytest.mark.asyncio
async def test_advisor_server_enforces_per_investigation_pacing_without_bridge_retry() -> None:
    investigation_id = uuid4()
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    prior_call = SimpleNamespace(
        id=uuid4(),
        investigation_id=investigation_id,
        source="advisor.consult",
        object_type="advisor_call",
        observed_at=datetime.now(UTC),
    )
    tool, _sample_blob_id = make_tool(investigation_id=investigation_id, transport=transport)
    tool._settings.operator_advisor_min_consultation_interval_seconds = 30
    tool._observations = ObservationRepository((prior_call,))

    result = await tool.execute(
        {"consultation_type": "pivot_strategy"}, make_context(investigation_id)
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.error_code == "advisor_pacing_wait"
    assert result.output["retry"] is False
    assert result.output["retry_after_seconds"] > 0
    assert transport.calls == []


@pytest.mark.asyncio
async def test_advisor_server_enforces_configured_bridge_turn_ceiling() -> None:
    investigation_id = uuid4()
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
    tool, _sample_blob_id = make_tool(investigation_id=investigation_id, transport=transport)
    tool._observations = ObservationRepository(prior_calls)

    result = await tool.execute(
        {"consultation_type": "pivot_strategy"}, make_context(investigation_id)
    )

    assert result.status is ToolInvocationStatus.UNAVAILABLE
    assert result.error_code == "advisor_budget_exhausted"
    assert result.output["retry"] is False
    assert transport.calls == []


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
        {"schema": "sample-obfuscated-strings.v1", "sample_sha256": "d" * 64, "strings": []},
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
    tool._settings.operator_advisor_max_turns_per_investigation = 1

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
            ToolInvocationStatus.FAILED,
            "advisor_output_invalid",
        ),
    ],
)
async def test_advisor_transport_failures_are_explicit_and_never_retried(
    submission, expected_status, expected_code
) -> None:
    investigation_id = uuid4()
    transport = Transport(submission)
    tool, _subject = make_tool(investigation_id=investigation_id, transport=transport)
    result = await tool.execute(
        {"consultation_type": "plan_investigation", "evidence_refs": []},
        make_context(investigation_id),
    )

    assert result.status is expected_status
    assert result.error_code == expected_code
    assert len(transport.calls) == 1
