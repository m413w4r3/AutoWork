from uuid import UUID, uuid5

from cti_app.application.bridge_investigator import BridgeInvestigator, ScriptedTurnTransport
from cti_app.domain.analyst_opinion import OpinionBlockType
from cti_app.domain.investigation import FindingAssessment, FindingKind
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
    assert any(
        '"view":"delta"' in prompt
        for prompt in (*a_transport.prompts[1:], *b_transport.prompts[1:])
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
