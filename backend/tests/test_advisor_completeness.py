from __future__ import annotations

import hashlib
from uuid import uuid4

import pytest

from cti_app.application.advisor_dossier import (
    AdvisorDossier,
    build_advisor_dossier,
    evaluate_advisor_evidence_preflight,
)
from cti_app.application.bridge_investigator import (
    ScriptedTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.investigation_advisor_tools import (
    InvestigationAdvisorTool,
    _advisor_payload_was_submitted,
)


def _planning_data(*, question: str, evidence: list[dict], findings: list[dict]) -> dict:
    return {
        "consultation_type": "plan_investigation",
        "question": question,
        "article_source_ref": "article:" + "a" * 64,
        "article_sha256": "a" * 64,
        "article_passages": [],
        "hypotheses": [{"id": "H1", "status": "open", "statement": "H1 synthetic hypothesis"}],
        "working_state_version": 4,
        "working_state_sha256": "b" * 64,
        "working_state_ref": "investigation-state:synthetic:v4",
        "evidence_refs": [item["evidence_id"] for item in evidence],
        "evidence": evidence,
        "findings": findings,
    }


def _item(preflight: dict, kind: str, identifier: str) -> dict:
    return next(
        row for row in preflight["items"] if row["kind"] == kind and row["id"] == identifier
    )


def test_decisive_proof_moves_from_retrieval_needed_to_sent() -> None:
    observation_id = str(uuid4())
    question = "Vérifier le passage S1P3.1 et les observations associées."
    data = _planning_data(
        question=question,
        evidence=[
            {
                "evidence_id": observation_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [],
            }
        ],
        findings=[],
    )

    missing_dossier = build_advisor_dossier(data)
    missing = evaluate_advisor_evidence_preflight(data, missing_dossier, question)
    assert missing["status"] == "NEEDS_RETRIEVAL"
    observation_statuses = _item(missing, "observation", observation_id)["statuses"]
    assert {row["status"] for row in observation_statuses} >= {"MISSING", "RETRIEVABLE"}
    assert missing["counts"]["present"] == 1  # the versioned working hypothesis
    assert missing["counts"]["sent"] == 0
    assert any("body was not loaded" in row["reason"] for row in missing["missing_refs"])

    data["article_passages"] = [
        {"claim_id": "S1P3.1", "heading": "Synthetic claim", "text": "Synthetic passage evidence."}
    ]
    data["evidence"][0]["content_excerpts"] = [
        {
            "kind": "text",
            "title": "Synthetic decisive observation",
            "text": "Observed proof marker.",
        }
    ]
    dossier = build_advisor_dossier(data)
    prompt = f"TASK: {question}\n{dossier.text}"
    ready = evaluate_advisor_evidence_preflight(data, dossier, prompt, submitted=True)

    assert ready["status"] == "READY"
    assert ready["missing_refs"] == []
    assert ready["counts"]["sent"] >= 2
    assert any(
        status["status"] == "SENT_TO_BRIDGE"
        for status in _item(ready, "observation", observation_id)["statuses"]
    )


def test_canonical_contradiction_omitted_from_dossier_requests_retrieval() -> None:
    finding_id = str(uuid4())
    question = "Expose la contradiction dans les constatations canoniques."
    data = _planning_data(
        question=question,
        evidence=[],
        findings=[
            {
                "finding_id": finding_id,
                "sha256": "d" * 64,
                "assessment": "refuted",
                "kind": "claim_verification",
                "line": "Contradiction observed in this synthetic finding.",
                "claim_refs": ["S1P3.1"],
            }
        ],
    )
    omitted = AdvisorDossier(text="DOSSIER DE FAITS\nAucune preuve sélectionnée.", manifest=())
    preflight = evaluate_advisor_evidence_preflight(data, omitted, f"TASK: {question}")

    assert preflight["status"] == "NEEDS_RETRIEVAL"
    assert "RETRIEVABLE" in {
        row["status"] for row in _item(preflight, "finding", finding_id)["statuses"]
    }

    included = build_advisor_dossier(data)
    recovered = evaluate_advisor_evidence_preflight(
        data, included, f"TASK: {question}\n{included.text}", submitted=True
    )
    assert recovered["status"] == "READY"
    assert "SENT_TO_BRIDGE" in {
        row["status"] for row in _item(recovered, "finding", finding_id)["statuses"]
    }


def test_contradiction_must_match_the_claim_named_in_the_question() -> None:
    unrelated_finding_id = str(uuid4())
    requested_finding_id = str(uuid4())
    question = "Expose la contradiction associée au claim S1P3.1."
    data = _planning_data(
        question=question,
        evidence=[],
        findings=[
            {
                "finding_id": unrelated_finding_id,
                "sha256": "d" * 64,
                "assessment": "refuted",
                "kind": "claim_verification",
                "line": "Contradiction observed for another claim.",
                "claim_refs": ["S2P1.1"],
            }
        ],
    )
    data["article_passages"] = [
        {
            "claim_id": "S1P3.1",
            "heading": "Synthetic requested claim",
            "text": "Synthetic passage for the requested claim.",
        }
    ]

    wrong_claim_dossier = build_advisor_dossier(data)
    blocked = evaluate_advisor_evidence_preflight(
        data, wrong_claim_dossier, f"TASK: {question}\n{wrong_claim_dossier.text}"
    )

    assert blocked["status"] == "INSUFFICIENT_EVIDENCE"
    assert any(
        row["ref"] == "required-contradiction" and "s1p3.1" in row["reason"].casefold()
        for row in blocked["missing_refs"]
    )

    data["findings"].append(
        {
            "finding_id": requested_finding_id,
            "sha256": "e" * 64,
            "assessment": "refuted",
            "kind": "claim_verification",
            "line": "Contradiction verified for the requested claim.",
            "claim_refs": ["S1P3.1"],
        }
    )
    matching_dossier = build_advisor_dossier(data)
    ready = evaluate_advisor_evidence_preflight(
        data,
        matching_dossier,
        f"TASK: {question}\n{matching_dossier.text}",
        submitted=True,
    )

    assert ready["status"] == "READY"
    assert ready["missing_refs"] == []
    assert "SENT_TO_BRIDGE" in {
        row["status"] for row in _item(ready, "finding", requested_finding_id)["statuses"]
    }


def test_unrequired_canonical_proof_omission_has_count_and_reason() -> None:
    required_id = str(uuid4())
    optional_id = str(uuid4())
    data = _planning_data(
        question="Plan around the synthetic marker.",
        evidence=[
            {
                "evidence_id": required_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {"kind": "text", "title": "Required marker", "text": "marker observed"}
                ],
            },
            {
                "evidence_id": optional_id,
                "sha256": "d" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {"kind": "text", "title": "Unrelated detail", "text": "unrelated detail"}
                ],
            },
        ],
        findings=[],
    )
    data["evidence_refs"] = [required_id]
    dossier = build_advisor_dossier(data)
    preflight = evaluate_advisor_evidence_preflight(data, dossier, dossier.text, submitted=True)

    assert preflight["status"] == "READY"
    assert preflight["counts"]["required"] > 0
    assert preflight["counts"]["present"] > 0
    assert preflight["counts"]["sent"] > 0
    assert preflight["counts"]["omitted"] == 1
    omitted = _item(preflight, "observation", optional_id)
    status = next(row for row in omitted["statuses"] if row["status"] == "INTENTIONALLY_OMITTED")
    assert "question does not require" in status["reason"]


def test_pre_submission_failure_does_not_mark_proof_as_sent() -> None:
    observation_id = str(uuid4())
    question = "Vérifier le marqueur synthétique."
    data = _planning_data(
        question=question,
        evidence=[
            {
                "evidence_id": observation_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {"kind": "text", "title": "Marker", "text": "marqueur synthétique"}
                ],
            }
        ],
        findings=[],
    )
    dossier = build_advisor_dossier(data)
    pre_submission = TurnSubmission(
        TurnSubmissionStatus.UNAVAILABLE,
        submission_state="pre_submission",
    )
    preflight = evaluate_advisor_evidence_preflight(
        data,
        dossier,
        dossier.text,
        submitted=_advisor_payload_was_submitted(pre_submission),
    )

    assert preflight["status"] == "READY"
    assert _advisor_payload_was_submitted(pre_submission) is False
    assert "SENT_TO_BRIDGE" not in {
        row["status"] for row in _item(preflight, "observation", observation_id)["statuses"]
    }


@pytest.mark.asyncio
async def test_large_dossier_crosses_bridge_file_threshold_without_omission() -> None:
    body = "SYNTHETIC_ONLY " + ("proof-marker " * 17_000)
    evidence_id = str(uuid4())
    evidence_sha = hashlib.sha256(b"synthetic canonical observation").hexdigest()
    data = {
        "consultation_type": "draft_text",
        "question": "proof-marker",
        "evidence": [
            {
                "evidence_id": evidence_id,
                "sha256": evidence_sha,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [{"kind": "text", "title": "Synthetic proof", "text": body}],
            }
        ],
    }
    dossier = build_advisor_dossier(data)
    manifest = dossier.manifest[0]
    expected_body = "Synthetic proof:\n" + "\n".join(
        f"| {line}" for line in body.strip().splitlines()
    )
    assert dossier.byte_size > 200_000
    assert manifest["body_bytes"] == len(expected_body.encode("utf-8"))
    assert manifest["body_sha256"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
    assert manifest["source_ref"] == {
        "kind": "observation",
        "id": evidence_id,
        "sha256": evidence_sha,
    }

    data.update(
        dossier_text=dossier.text,
        dossier_sha256=dossier.sha256,
        dossier_bytes=dossier.byte_size,
        dossier_manifest=[dict(row) for row in dossier.manifest],
    )
    prompt = InvestigationAdvisorTool._prompt("draft_text", data)
    payload = prompt.encode("utf-8")
    simulated_bridge_mode = "text_file" if len(payload) > 200_000 else "inline"
    assert simulated_bridge_mode == "text_file"

    transport = ScriptedTurnTransport(["synthetic response"])
    result = await transport.submit(
        investigation_id=uuid4(),
        subject_id=uuid4(),
        turn_index=0,
        context_sha256=hashlib.sha256(payload).hexdigest(),
        prompt=prompt,
        external_llm_allowed=True,
    )
    assert result.status is TurnSubmissionStatus.SUCCEEDED
    assert transport.prompts == [prompt]
    assert (
        hashlib.sha256(transport.prompts[0].encode("utf-8")).hexdigest()
        == hashlib.sha256(payload).hexdigest()
    )
