from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from uuid import UUID, uuid4

import pytest

from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.content_pivot_candidates import verify_content_pattern_candidates
from cti_app.domain.investigation import DetectionCandidateStatus
from tests.test_investigation_advisor_tools import (
    Transport,
    _advisor_response_link_payloads,
    _attach_sample_policy,
    make_context,
    make_observation,
    make_tool,
)
from tests.test_investigation_rule_tools import (
    SampleOrigin,
    _associate,
    _call,
    _harness,
)


@pytest.mark.asyncio
async def test_bridge_yara_prose_reaches_an_untested_rule_candidate() -> None:
    investigation_id = uuid4()
    sample_bytes = b"\x00" * 0x80 + b"CreateMutexW" + b"\x00"
    sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()
    evidence, evidence_bytes = make_observation(
        investigation_id,
        {
            "schema": "sample-obfuscated-strings.v1",
            "sample_sha256": sample_sha256,
            "strings": [
                {
                    "value": "CreateMutexW",
                    "kind": "static",
                    "encoding": "ascii",
                    "offset": 0x80,
                    "truncated_fields": [],
                }
            ],
        },
    )
    evidence = replace(evidence, source="autowork.floss")
    source = """rule BridgeProseCandidate {
    strings:
        $mutex = "CreateMutexW" ascii
        $unmeasured = "NoMeasurementForThisMarker" ascii
    condition:
        $mutex and $unmeasured
}
"""
    raw_response = (
        "The first literal is measured in the sample. The second literal still needs evidence.\n\n"
        "```yara\n" + source + "```\n"
        "This is a consultative candidate and has not been tested."
    )
    invocation_id = str(uuid4())
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text=raw_response,
            bridge_invocation_id=invocation_id,
        )
    )
    advisor, sample_blob_id = make_tool(
        investigation_id=investigation_id,
        transport=transport,
        observation=evidence,
        raw=evidence_bytes,
    )
    _attach_sample_policy(advisor, sample_blob_id, sample_sha256)
    content_pattern = verify_content_pattern_candidates(
        (evidence,), {str(evidence.id): json.loads(evidence_bytes)}
    )[0]

    consulted = await advisor.execute(
        {
            "consultation_type": "yara_draft",
            "evidence_refs": [str(evidence.id)],
            "measurements": [{"content_pattern_id": content_pattern.id}],
        },
        make_context(investigation_id, key="bridge-prose-yara-e2e"),
    )

    assert consulted.output["usable"] is True
    assert consulted.output["structured_usable"] is False
    assert consulted.output["canonicalization_eligible"] is False
    assert consulted.output["consult_status"] == "response_persisted"
    assert consulted.output["rule_source"] == source
    source_sha256 = hashlib.sha256(source.encode("utf-8")).hexdigest()
    assert consulted.output["rule_source_sha256"] == source_sha256

    response_links = await _advisor_response_link_payloads(advisor, investigation_id)
    assert len(response_links) == 1
    _response_observation, response_link = response_links[0]
    assert response_link["bridge_invocation_id"] == invocation_id
    assert (
        response_link["raw_response_sha256"]
        == hashlib.sha256(raw_response.encode("utf-8")).hexdigest()
    )
    assert advisor._artifact_store.values[UUID(response_link["raw_response_blob_id"])] == (
        raw_response.encode("utf-8")
    )

    advisor_call = next(
        observation
        for observation in advisor._observations.observations.values()
        if observation.source == "advisor.consult" and observation.object_type == "advisor_call"
    )
    persisted_call = json.loads(
        await advisor._artifact_store.read_bytes(advisor_call.blob_id, max_bytes=1_000_000)
    )
    assert persisted_call["output"]["raw_output"] == raw_response
    assert persisted_call["output"]["raw_output_sha256"] == response_link["raw_response_sha256"]
    assert persisted_call["output"]["rule_source"] == source
    assert persisted_call["output"]["rule_source_sha256"] == source_sha256

    rules = _harness(samples={sample_sha256: sample_bytes})
    rules.investigation_id = investigation_id
    rules.context = replace(
        rules.context,
        investigation_id=investigation_id,
        envelope=replace(rules.context.envelope, investigation_id=investigation_id),
    )
    await _associate(rules, sample_sha256, origin=SampleOrigin.SEED)
    for observation in advisor._observations.observations.values():
        await rules.evidence.repository.append_if_absent(observation)
    rules.evidence.payloads[advisor_call.blob_id] = persisted_call

    proposed = await _call(
        rules,
        "rule.propose_bridge",
        {
            "bridge_observation_id": str(advisor_call.id),
            "bridge_invocation_id": invocation_id,
            "rule_source_sha256": source_sha256,
        },
    )

    assert proposed.status.value == "succeeded", dict(proposed.output)
    assert proposed.output["source_author"] == "bridge"
    assert proposed.output["compile_status"] == "ok"
    assert tuple(proposed.output["justification_gaps"]) == ("$unmeasured",)
    versions = await rules.candidates.list_for_candidate(UUID(proposed.output["candidate_id"]))
    assert versions[0].status is DetectionCandidateStatus.CANDIDATE
    assert versions[0].source == source
    assert versions[0].source_sha256 == source_sha256

    tested = await _call(
        rules,
        "rule.test",
        {"candidate_id": proposed.output["candidate_id"], "version": 1},
    )
    assert "report" in tested.output
    assert tested.output["report"]["rule_source_sha256"] == source_sha256
    tested_versions = await rules.candidates.list_for_candidate(
        UUID(proposed.output["candidate_id"])
    )
    assert all(
        version.status is not DetectionCandidateStatus.APPROVED for version in tested_versions
    )


@pytest.mark.asyncio
async def test_bridge_suricata_prose_is_persisted_as_untested_without_capture() -> None:
    investigation_id = uuid4()
    source = (
        'alert http any any -> any any (msg:"Bridge prose candidate"; '
        'content:"example.invalid"; sid:9901234; rev:1;)\n'
    )
    raw_response = (
        "This is an untested Suricata candidate because no capture is available.\n\n"
        "```suricata\n" + source + "```"
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=raw_response))
    advisor, _sample_blob_id = make_tool(investigation_id=investigation_id, transport=transport)

    result = await advisor.execute(
        {"consultation_type": "suricata_draft"},
        make_context(investigation_id, key="bridge-prose-suricata-e2e"),
    )

    assert result.output["usable"] is True
    assert result.output["structured_usable"] is False
    assert result.output["canonicalization_eligible"] is False
    assert result.output["consult_status"] == "response_persisted"
    observation = next(
        item
        for item in advisor._observations.observations.values()
        if item.source == "advisor.consult" and item.object_type == "advisor_call"
    )
    persisted = json.loads(
        await advisor._artifact_store.read_bytes(observation.blob_id, max_bytes=1_000_000)
    )
    assert persisted["output"]["raw_output"] == raw_response
    assert persisted["output"]["rule_source"] == source
    assert (
        persisted["output"]["rule_source_sha256"]
        == hashlib.sha256(source.encode("utf-8")).hexdigest()
    )
    assert any("untested" in item.casefold() for item in persisted["output"]["limitations"])
    assert persisted["output"]["fact_refs"] == []
