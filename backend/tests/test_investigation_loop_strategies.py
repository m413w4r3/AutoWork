import json
from dataclasses import replace
from uuid import UUID, uuid4, uuid5

import pytest

from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.application.investigation_contracts import (
    FindingProposalV1,
    parse_agent_turn_decision,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.domain.analyst_opinion import OpinionBlockType
from cti_app.domain.investigation import FindingAssessment, FindingKind, InvestigationFinding
from cti_app.domain.investigation_state import Hypothesis, HypothesisStatus, StopReason
from tests.test_investigation_service import (
    _decision,
    _finding_id,
    _Fixture,
    _operation_and_observation_id,
    _opinion_delta,
    _proposal,
    _tool_call,
)


async def test_two_agent_strategies_choose_distinct_tool_orders_and_draft_findings() -> None:
    # Strategy A confirms the article's chain first, then runs a VT-like search.
    a_chain = _tool_call("chain.confirm", "sample:seed-a", "confirm the reported infection chain")
    a_vt = _tool_call("vt.search", "content:loader-marker", "look for a related payload")
    fixture_a = _Fixture(
        responses=[],
        tools=(("chain.confirm", "vt"), ("vt.search", "vt")),
    )
    a_state = await fixture_a.start()
    fixture_a.tools["chain.confirm"].observation_object_type = "sample"
    fixture_a.tools["chain.confirm"].observation_object_id = "sample:seed-a"
    a_candidate_hash = "d" * 64
    fixture_a.tools["vt.search"].observation_object_type = "sample"
    fixture_a.tools["vt.search"].observation_object_id = a_candidate_hash
    _, a_chain_observation = _operation_and_observation_id(a_state.investigation_id, a_chain)
    _, a_vt_observation = _operation_and_observation_id(a_state.investigation_id, a_vt)
    a_chain_proposal = _proposal(
        FindingKind.INFECTION_CHAIN.value,
        FindingAssessment.SUPPORTED.value,
        "The seed retrieves a second-stage payload.",
        a_chain_observation,
        ("C1",),
        subject_ref="sample:seed-a",
    )
    a_chain_finding = _finding_id(a_state.investigation_id, a_chain_proposal)
    a_vt_proposal = _proposal(
        FindingKind.NEW_SAMPLE.value,
        FindingAssessment.SUPPORTED.value,
        "The passive search returned a related candidate hash.",
        a_vt_observation,
        subject_ref=a_candidate_hash,
    )
    a_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="confirm infection chain",
                next_action="continue",
                tools=(a_chain,),
                findings=(a_chain_proposal,),
            ),
            _decision(
                goal="search for a payload variant",
                next_action="draft_opinion",
                tools=(a_vt,),
                findings=(a_vt_proposal,),
                opinion=_opinion_delta(
                    a_chain_finding,
                    a_chain_observation,
                    text="The article's chain is supported by the observed retrieval.",
                ),
            ),
            _decision(goal="finish after the useful evidence", next_action="finish"),
        ]
    )
    fixture_a.bridge_transport = a_transport
    fixture_a.bridge = BridgeInvestigator(a_transport)
    fixture_a.service._bridge = fixture_a.bridge
    result_a = await fixture_a.service.run(a_state.investigation_id)

    # Strategy B starts with network evidence, changes its attribution hypothesis,
    # verifies a claim, and finishes without calling a sample tool.
    b_network = _tool_call("network.lookup", "203.0.113.8", "inspect the reported address")
    b_claim = _tool_call("claim.verify", "C1", "reassess the C2 attribution")
    fixture_b = _Fixture(
        responses=[],
        tools=(("network.lookup", "network"), ("claim.verify", "network")),
    )
    hypothesis = Hypothesis(id="ip-attribution", statement="The address is campaign C2.")
    b_state = await fixture_b.start(hypotheses=(hypothesis,))
    fixture_b.tools["network.lookup"].observation_object_type = "ip"
    fixture_b.tools["network.lookup"].observation_object_id = "203.0.113.8"
    _, b_network_observation = _operation_and_observation_id(b_state.investigation_id, b_network)
    _, b_claim_observation = _operation_and_observation_id(b_state.investigation_id, b_claim)
    b_network_proposal = _proposal(
        FindingKind.NETWORK_BEHAVIOUR.value,
        FindingAssessment.SUPPORTED.value,
        "A passive observation associates the address with a shared service.",
        b_network_observation,
        ("C1",),
        subject_ref="203.0.113.8",
    )
    b_network_finding = _finding_id(b_state.investigation_id, b_network_proposal)
    b_claim_proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.REFUTED.value,
        "Available timestamps do not support the article's campaign attribution.",
        b_claim_observation,
        ("C1",),
    )
    b_claim_proposal["hypothesis_refs"] = ["ip-attribution"]
    b_transport = ScriptedTurnTransport(
        [
            _decision(
                goal="inspect the address first",
                next_action="switch_hypothesis",
                tools=(b_network,),
                findings=(b_network_proposal,),
                hypothesis_refs=("ip-attribution",),
            ),
            _decision(
                goal="reassess the IP attribution",
                next_action="finish",
                tools=(b_claim,),
                findings=(b_claim_proposal,),
                opinion=_opinion_delta(
                    b_network_finding,
                    b_network_observation,
                    text="The observed service is shared, so attribution remains limited.",
                ),
                hypothesis_refs=("ip-attribution",),
            ),
        ]
    )
    fixture_b.bridge_transport = b_transport
    fixture_b.bridge = BridgeInvestigator(b_transport)
    fixture_b.service._bridge = fixture_b.bridge
    result_b = await fixture_b.service.run(b_state.investigation_id)

    a_order = fixture_a.execution_order
    b_order = fixture_b.execution_order
    assert result_a.stop_reason is StopReason.AGENT_FINISHED
    assert result_b.stop_reason is StopReason.AGENT_FINISHED
    assert a_order == ["chain.confirm", "vt.search"]
    assert b_order == ["network.lookup", "claim.verify"]
    assert a_order != b_order
    assert "sample.get_feature_pack" not in b_order
    assert all(
        "current_stage" not in prompt for prompt in (*a_transport.prompts, *b_transport.prompts)
    )
    # Each Bridge turn carries the bounded canonical context as readable text,
    # including the article and current state, without a second JSON snapshot.
    assert all(
        "CONTEXTE FACTUEL CANONIQUE — TEXTE" in prompt
        and "A verified campaign narrative." in prompt
        and "The article describes a loader chain." in prompt
        and '"view":"full"' not in prompt
        for prompt in (*a_transport.prompts, *b_transport.prompts)
    )
    assert fixture_a.handoff.modified_artifacts == []
    assert fixture_b.handoff.modified_artifacts == []
    assert all("yara" not in name.lower() for name in (*a_order, *b_order))
    a_opinion = await fixture_a.opinions.get(
        uuid5(UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d"), str(a_state.investigation_id))
    )
    assert a_opinion is not None
    b_opinion = await fixture_b.opinions.get(
        uuid5(UUID("f141ae8e-999c-4a7f-b07d-5ffb0bd7b86d"), str(b_state.investigation_id))
    )
    assert b_opinion is not None
    assert b_opinion.renderable_blocks[0].block_type is OpinionBlockType.KEY_FINDINGS
    final_hypothesis = (await fixture_b.states.get(b_state.investigation_id)).hypotheses[0]
    assert final_hypothesis.status is HypothesisStatus.REFUTED


def test_hypothesis_findings_are_explicitly_linked_not_shared_across_a_turn() -> None:
    observation_a, observation_b = uuid4(), uuid4()
    investigation_id = uuid4()
    first = _proposal(
        FindingKind.NETWORK_BEHAVIOUR.value,
        FindingAssessment.SUPPORTED.value,
        "First hypothesis is supported.",
        observation_a,
    )
    first["hypothesis_refs"] = ["a"]
    second = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.REFUTED.value,
        "Second hypothesis is refuted.",
        observation_b,
    )
    second["hypothesis_refs"] = ["b"]
    decision = parse_agent_turn_decision(
        _decision(
            goal="check two unrelated hypotheses",
            next_action="continue",
            findings=(first, second),
            hypothesis_refs=("a", "b", "c"),
        )
    )
    findings = (
        InvestigationFinding(
            investigation_id=investigation_id,
            kind=FindingKind.NETWORK_BEHAVIOUR,
            assessment=FindingAssessment.SUPPORTED,
            statement="First hypothesis is supported.",
            observation_refs=(observation_a,),
        ),
        InvestigationFinding(
            investigation_id=investigation_id,
            kind=FindingKind.CLAIM_VERIFICATION,
            assessment=FindingAssessment.REFUTED,
            statement="Second hypothesis is refuted.",
            observation_refs=(observation_b,),
        ),
    )
    updated = InvestigationService._update_hypotheses(
        tuple(Hypothesis(id=name, statement=f"Hypothesis {name}") for name in ("a", "b", "c")),
        decision=decision,
        findings=findings,
    )
    assert [item.status for item in updated] == [
        HypothesisStatus.SUPPORTED,
        HypothesisStatus.REFUTED,
        HypothesisStatus.OPEN,
    ]
    assert updated[0].evidence_refs == (observation_a,)
    assert updated[1].evidence_refs == (observation_b,)
    assert updated[2].evidence_refs == ()
    assert updated[0].finding_refs == (findings[0].id,)
    assert updated[1].finding_refs == (findings[1].id,)
    assert updated[2].finding_refs == ()


def test_finding_link_alone_targets_and_refutes_one_hypothesis() -> None:
    observation = uuid4()
    investigation_id = uuid4()
    proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.REFUTED.value,
        "The observation contradicts the attribution.",
        observation,
    )
    proposal["hypothesis_refs"] = ["a"]
    decision = parse_agent_turn_decision(
        _decision(
            goal="evaluate the linked attribution",
            next_action="continue",
            findings=(proposal,),
        )
    )
    finding = InvestigationFinding(
        investigation_id=investigation_id,
        kind=FindingKind.CLAIM_VERIFICATION,
        assessment=FindingAssessment.REFUTED,
        statement="The observation contradicts the attribution.",
        observation_refs=(observation,),
    )

    updated = InvestigationService._update_hypotheses(
        (Hypothesis(id="a", statement="The attribution is correct."),),
        decision=decision,
        findings=(finding,),
    )

    assert updated[0].status is HypothesisStatus.REFUTED
    assert updated[0].evidence_refs == (observation,)
    assert updated[0].finding_refs == (finding.id,)


def test_hypothesis_conflicting_or_unlinked_findings_cannot_be_promoted() -> None:
    observation_a, observation_b = uuid4(), uuid4()
    investigation_id = uuid4()
    proposals = []
    findings = []
    for assessment, observation in (
        (FindingAssessment.SUPPORTED, observation_a),
        (FindingAssessment.REFUTED, observation_b),
    ):
        proposal = _proposal(
            FindingKind.CLAIM_VERIFICATION.value, assessment.value, "Check", observation
        )
        proposal["hypothesis_refs"] = ["a"]
        proposals.append(proposal)
        findings.append(
            InvestigationFinding(
                investigation_id=investigation_id,
                kind=FindingKind.CLAIM_VERIFICATION,
                assessment=assessment,
                statement="Check",
                observation_refs=(observation,),
            )
        )
    decision = parse_agent_turn_decision(
        _decision(
            goal="reconcile contradiction",
            next_action="continue",
            findings=tuple(proposals),
            hypothesis_refs=("a",),
        )
    )
    updated = InvestigationService._update_hypotheses(
        (Hypothesis(id="a", statement="Uncertain"),),
        decision=decision,
        findings=tuple(findings),
    )
    assert updated[0].status is HypothesisStatus.OPEN
    assert set(updated[0].evidence_refs) == {observation_a, observation_b}
    unlinked = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.SUPPORTED.value,
        "Unrelated",
        observation_a,
    )
    decision_unlinked = parse_agent_turn_decision(
        _decision(
            goal="do not infer a link",
            next_action="switch_hypothesis",
            findings=(unlinked,),
            hypothesis_refs=("a",),
        )
    )
    unchanged = InvestigationService._update_hypotheses(
        (Hypothesis(id="a", statement="Uncertain"),),
        decision=decision_unlinked,
        findings=(findings[0],),
    )
    assert unchanged[0].status is HypothesisStatus.OPEN
    assert unchanged[0].evidence_refs == ()


@pytest.mark.asyncio
async def test_unknown_hypothesis_reference_is_rejected_before_tool_or_finding_effects() -> None:
    call = _tool_call("vt.search", "content:loader", "check a passive indicator")
    proposal = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.SUPPORTED.value,
        "The observation supports an unknown hypothesis.",
        uuid4(),
    )
    proposal["hypothesis_refs"] = ["missing-hypothesis"]
    fixture = _Fixture(
        responses=[
            _decision(
                goal="check the proposed attribution",
                next_action="finish",
                tools=(call,),
                findings=(proposal,),
                hypothesis_refs=("known-hypothesis",),
            )
        ]
        * 3,
        tools=(("vt.search", "vt"),),
    )
    initial = await fixture.start(
        hypotheses=(Hypothesis(id="known-hypothesis", statement="Known attribution."),)
    )

    result = await fixture.service.run(initial.investigation_id)

    assert result.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert result.hypotheses == initial.hypotheses
    assert fixture.execution_order == []
    assert await fixture.findings.list_for_investigation(initial.investigation_id) == ()
    assert any("unknown_hypothesis_reference" in action for action in result.recent_actions)
    assert (
        fixture.bridge_transport.submission_count == BridgeInvestigator.MAX_CORRECTIVE_ATTEMPTS + 1
    )


@pytest.mark.asyncio
async def test_unknown_hypothesis_reference_is_corrected_before_tool_execution() -> None:
    call = _tool_call("vt.search", "synthetic-indicator", "check one passive indicator")
    invalid = _decision(
        goal="check a proposed hypothesis",
        next_action="finish",
        tools=(call,),
        hypothesis_refs=("new-hypothesis-label",),
    )
    corrected = _decision(
        goal="check the indicator without inventing a hypothesis",
        next_action="finish",
        tools=(call,),
    )
    fixture = _Fixture(responses=[invalid, corrected], tools=(("vt.search", "vt"),))
    initial = await fixture.start()

    result = await fixture.service.run(initial.investigation_id)

    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert fixture.bridge_transport.submission_count == 2
    assert fixture.execution_order == ["vt.search"]
    assert "decision_rejected:unknown_hypothesis_reference" in fixture.bridge_transport.prompts[1]
    assert "known hypothesis ids: none" in fixture.bridge_transport.prompts[1].lower()


@pytest.mark.asyncio
async def test_only_explicitly_addressed_operator_requests_are_consumed() -> None:
    fixture = _Fixture(responses=[], tools=())
    initial = await fixture.start()
    first_request_id = uuid4()
    second_request_id = uuid4()
    first_action = "user-request:" + json.dumps(
        {"request_id": str(first_request_id), "text": "Check the synthetic host.", "target": None},
        sort_keys=True,
    )
    second_action = "user-request:" + json.dumps(
        {
            "request_id": str(second_request_id),
            "text": "Compare the synthetic domain.",
            "target": None,
        },
        sort_keys=True,
    )
    with_requests = replace(
        initial,
        version=initial.version + 1,
        recent_actions=(first_action, second_action),
    )
    await fixture.states.save_if_version(with_requests, expected_version=initial.version)
    fixture.bridge_transport = ScriptedTurnTransport(
        [
            "@@DECISION@@\nGOAL: Address one operator request.\n"
            "SUMMARY: The first request was handled.\nNEXT: finish\n"
            f"ADDRESSED_REQUESTS: {first_request_id}\n@@END@@"
        ]
    )
    fixture.bridge = BridgeInvestigator(fixture.bridge_transport)
    fixture.service._bridge = fixture.bridge

    result = await fixture.service.run(initial.investigation_id)

    assert first_action not in result.recent_actions
    assert second_action in result.recent_actions


@pytest.mark.asyncio
async def test_duplicate_finding_identity_cannot_promote_two_hypotheses() -> None:
    call = _tool_call("vt.search", "content:loader", "collect one passive observation")
    observation = uuid4()
    proposal_a = _proposal(
        FindingKind.CLAIM_VERIFICATION.value,
        FindingAssessment.SUPPORTED.value,
        "The same finding content.",
        observation,
    )
    proposal_a["hypothesis_refs"] = ["a"]
    proposal_b = {**proposal_a, "hypothesis_refs": ["b"]}
    fixture = _Fixture(
        responses=[
            _decision(
                goal="keep two hypotheses separate",
                next_action="finish",
                tools=(call,),
                findings=(proposal_a, proposal_b),
                hypothesis_refs=("a", "b"),
            )
        ],
        tools=(("vt.search", "vt"),),
    )
    initial = await fixture.start(
        hypotheses=(
            Hypothesis(id="a", statement="Hypothesis A."),
            Hypothesis(id="b", statement="Hypothesis B."),
        )
    )

    result = await fixture.service.run(initial.investigation_id)

    assert result.stop_reason is StopReason.SUBMISSION_NEEDS_REVIEW
    assert result.hypotheses == initial.hypotheses
    assert fixture.execution_order == []
    assert await fixture.findings.list_for_investigation(initial.investigation_id) == ()
    assert any(
        "bridge_invalid_output:decision_rejected:duplicate_finding_identity_conflicting_links"
        in action
        for action in result.recent_actions
    )


@pytest.mark.asyncio
async def test_frozen_legacy_decision_replays_without_hypothesis_annotation() -> None:
    legacy_json = (
        '{"schema":"agent-turn-decision.v1","reasoning_summary":"Keep investigating.",'
        '"goal":"Check one claim.","proposed_tool_calls":[],"finding_proposals":['
        '{"kind":"claim_verification","assessment":"inconclusive",'
        '"statement":"The available evidence is incomplete.","subject_ref":null,'
        '"observation_refs":[],"claim_refs":[],"novelty":null,"scope":"",'
        '"limitations":[]}],"opinion_delta":null,"next_action":"finish"}'
    )
    fixture = _Fixture(responses=[legacy_json], tools=())
    initial = await fixture.start(
        hypotheses=(Hypothesis(id="still-open", statement="An unresolved hypothesis."),)
    )

    decision = parse_agent_turn_decision(legacy_json)
    first = await fixture.service._persist_findings(
        investigation_id=initial.investigation_id,
        proposals=decision.finding_proposals,
        decision_trace=decision.reasoning_summary,
    )
    replay = parse_agent_turn_decision(legacy_json)
    replayed = await fixture.service._persist_findings(
        investigation_id=initial.investigation_id,
        proposals=replay.finding_proposals,
        decision_trace=replay.reasoning_summary,
    )
    result = await fixture.service.run(initial.investigation_id)

    assert decision.hypothesis_refs == ()
    assert decision.finding_proposals[0].hypothesis_refs == ()
    assert first[0].id == replayed[0].id
    assert result.stop_reason is StopReason.AGENT_FINISHED
    assert result.hypotheses == initial.hypotheses
    assert len(await fixture.findings.list_for_investigation(initial.investigation_id)) == 1


@pytest.mark.asyncio
async def test_finding_hypothesis_annotation_does_not_change_persisted_identity() -> None:
    fixture = _Fixture(responses=[], tools=())
    initial = await fixture.start()
    raw_proposal = {
        "kind": FindingKind.CLAIM_VERIFICATION,
        "assessment": FindingAssessment.INCONCLUSIVE,
        "statement": "A stable finding identity.",
        "observation_refs": (),
        "claim_refs": (),
    }
    plain = FindingProposalV1.model_validate(raw_proposal)
    annotated = plain.model_copy(update={"hypothesis_refs": ("hypothesis-a",)})

    persisted_plain = await fixture.service._persist_findings(
        investigation_id=initial.investigation_id,
        proposals=(plain,),
        decision_trace="first replay",
    )
    persisted_annotated = await fixture.service._persist_findings(
        investigation_id=initial.investigation_id,
        proposals=(annotated,),
        decision_trace="annotated replay",
    )

    assert persisted_plain[0].id == persisted_annotated[0].id
    assert len(await fixture.findings.list_for_investigation(initial.investigation_id)) == 1
