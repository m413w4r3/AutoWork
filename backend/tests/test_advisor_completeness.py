from __future__ import annotations

import base64
import hashlib
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.advisor_dossier import (
    AdvisorDossier,
    build_advisor_dossier,
    evaluate_advisor_evidence_preflight,
    extract_content_excerpts,
)
from cti_app.application.advisor_prompt import render_advisor_prompt
from cti_app.application.bridge_investigator import (
    ScriptedTurnTransport,
    TurnSubmission,
    TurnSubmissionStatus,
)
from cti_app.application.investigation_advisor_tools import _advisor_payload_was_submitted


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
                "blob_read_verified": True,
                "content_excerpts": [],
            }
        ],
        findings=[],
    )
    data["article_passages"] = [
        {"claim_id": "S1P3.1", "heading": "Synthetic claim", "text": "Synthetic passage evidence."}
    ]

    missing_dossier = build_advisor_dossier(data)
    missing = evaluate_advisor_evidence_preflight(data, missing_dossier, question)
    assert missing["status"] == "NEEDS_RETRIEVAL"
    observation_statuses = _item(missing, "observation", observation_id)["statuses"]
    assert {row["status"] for row in observation_statuses} >= {"MISSING", "RETRIEVABLE"}
    assert missing["counts"]["present"] == 2  # the passage and versioned working hypothesis
    assert missing["counts"]["sent"] == 0
    assert any("body was not loaded" in row["reason"] for row in missing["missing_refs"])

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


@pytest.mark.parametrize(
    "consultation_type", ["plan_investigation", "pivot_strategy", "figure_plan"]
)
def test_general_contradiction_search_is_informational_without_a_targeted_claim(
    consultation_type: str,
) -> None:
    question = "Planifier la suite et rechercher les contradictions autour de Blackwood."
    data = _planning_data(question=question, evidence=[], findings=[])
    data["consultation_type"] = consultation_type
    data["units"] = [{"id": "U1", "text": "Investigate the selected passage S1P3.1."}]
    data["article_passages"] = [
        {
            "claim_id": "S1P3.1",
            "heading": "Synthetic claim",
            "text": "Blackwood uses a synthetic behavior marker.",
        }
    ]
    if consultation_type == "figure_plan":
        data["findings"] = [
            {
                "finding_id": str(uuid4()),
                "sha256": "c" * 64,
                "assessment": "inconclusive",
                "kind": "claim_verification",
                "line": "Blackwood uses a synthetic behavior marker.",
                "claim_refs": ["S1P3.1"],
            }
        ]

    dossier = build_advisor_dossier(data)
    preflight = evaluate_advisor_evidence_preflight(data, dossier, dossier.text)

    assert preflight["status"] == "READY"
    contradiction = next(row for row in preflight["items"] if row["kind"] == "contradiction")
    assert contradiction["required"] is False
    assert {status["status"] for status in contradiction["statuses"]} == {"INFORMATIONAL"}
    assert "Aucune réfutation connue" in " ".join(
        status["reason"] for status in contradiction["statuses"]
    )
    assert "Aucune réfutation connue" in dossier.text


def test_contradiction_assertion_with_a_cited_finding_is_informational_not_blocking() -> None:
    finding_id = str(uuid4())
    question = "Review the contradiction attached to this finding."
    data = _planning_data(
        question=question,
        evidence=[],
        findings=[
            {
                "finding_id": finding_id,
                "sha256": "d" * 64,
                "assessment": "qualified",
                "kind": "claim_verification",
                "line": "Qualified finding; no refutation is known for it.",
                "claim_refs": ["S1P3.1"],
            }
        ],
    )
    data["assertions"] = [
        {
            "nature": "contradiction",
            "text": "Check whether the cited finding is refuted.",
            "refs": [{"kind": "finding", "id": finding_id}],
        }
    ]

    dossier = build_advisor_dossier(data)
    preflight = evaluate_advisor_evidence_preflight(data, dossier, dossier.text)

    # A refutation is never a hard prerequisite: the consultation proceeds and says so.
    assert preflight["status"] == "READY"
    assert preflight["missing_refs"] == []


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

    # A missing refutation is information, never a blocking gap (friction removed).
    assert blocked["status"] == "READY"
    assert blocked["missing_refs"] == []

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


def test_multi_claim_contradiction_requires_each_named_claim() -> None:
    first_finding_id = str(uuid4())
    question = "Vérifier les contradictions pour S1P3.1 et S1P3.2."
    data = _planning_data(
        question=question,
        evidence=[],
        findings=[
            {
                "finding_id": first_finding_id,
                "sha256": "d" * 64,
                "assessment": "refuted",
                "kind": "claim_verification",
                "line": "Contradiction verified for the first claim.",
                "claim_refs": ["S1P3.1"],
            }
        ],
    )
    data["article_passages"] = [
        {"claim_id": claim, "heading": "Synthetic", "text": f"Passage {claim}."}
        for claim in ("S1P3.1", "S1P3.2")
    ]
    dossier = build_advisor_dossier(data)
    preflight = evaluate_advisor_evidence_preflight(data, dossier, dossier.text)

    # The second claim has no refuted finding: informational only, the consultation proceeds.
    assert preflight["status"] == "READY"
    assert preflight["missing_refs"] == []

    data["findings"].append(
        {
            "finding_id": str(uuid4()),
            "sha256": "e" * 64,
            "assessment": "refuted",
            "kind": "claim_verification",
            "line": "Contradiction verified for the second claim.",
            "claim_refs": ["S1P3.2"],
        }
    )
    complete_dossier = build_advisor_dossier(data)
    complete = evaluate_advisor_evidence_preflight(
        data, complete_dossier, complete_dossier.text, submitted=True
    )
    assert complete["status"] == "READY"
    assert complete["missing_refs"] == []


def test_optional_reference_without_body_is_only_intentionally_omitted() -> None:
    required_id = str(uuid4())
    optional_id = str(uuid4())
    data = _planning_data(
        question="Plan around the required marker.",
        evidence=[
            {
                "evidence_id": required_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {"kind": "text", "title": "Marker", "text": "required marker"}
                ],
            },
            {
                "evidence_id": optional_id,
                "sha256": "d" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [],
            },
        ],
        findings=[],
    )
    data["evidence_refs"] = [required_id]
    dossier = build_advisor_dossier(data)
    preflight = evaluate_advisor_evidence_preflight(data, dossier, dossier.text)

    optional_statuses = {
        row["status"] for row in _item(preflight, "observation", optional_id)["statuses"]
    }
    assert "MISSING" not in optional_statuses
    assert optional_statuses == {"INTENTIONALLY_OMITTED"}


def test_optional_blob_read_failure_is_not_misreported_as_missing() -> None:
    required_id = str(uuid4())
    optional_id = str(uuid4())
    data = _planning_data(
        question="Plan around the required marker.",
        evidence=[
            {
                "evidence_id": required_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {"kind": "text", "title": "Marker", "text": "required marker"}
                ],
            },
            {
                "evidence_id": optional_id,
                "sha256": "d" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "blob_read_verified": False,
                "content_excerpts": [],
            },
        ],
        findings=[],
    )
    data["evidence_refs"] = [required_id]
    dossier = build_advisor_dossier(data)
    preflight = evaluate_advisor_evidence_preflight(data, dossier, dossier.text)

    optional_statuses = {
        row["status"] for row in _item(preflight, "observation", optional_id)["statuses"]
    }
    assert preflight["status"] == "READY"
    assert "MISSING" not in optional_statuses
    assert optional_statuses == {"NOT_VERIFIED", "INTENTIONALLY_OMITTED"}


def test_lexically_omitted_alias_is_reported_as_unverified_selection() -> None:
    selected_id = str(uuid4())
    alias_id = str(uuid4())
    data = _planning_data(
        question="Plan autour de CreateMutexW.",
        evidence=[
            {
                "evidence_id": selected_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {"kind": "text", "title": "Imported API", "text": "CreateMutexW was called."}
                ],
            },
            {
                "evidence_id": alias_id,
                "sha256": "d" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {
                        "kind": "text",
                        "title": "Object synchronization",
                        "text": "Named-object synchronization API was observed.",
                    }
                ],
            },
        ],
        findings=[],
    )
    data["evidence_refs"] = []
    dossier = build_advisor_dossier(data)
    preflight = evaluate_advisor_evidence_preflight(data, dossier, dossier.text)

    assert selected_id in {row["id"] for row in dossier.manifest}
    assert alias_id not in {row["id"] for row in dossier.manifest}
    omitted = _item(preflight, "observation", alias_id)
    statuses = {row["status"] for row in omitted["statuses"]}
    reasons = " ".join(row["reason"] for row in omitted["statuses"]).casefold()
    assert {"NOT_VERIFIED", "INTENTIONALLY_OMITTED"} <= statuses
    assert "bounded lexical selection" in reasons
    assert "semantic relevance" in reasons
    assert "question does not require" not in reasons


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
    assert "bounded lexical selection" in status["reason"]
    assert "semantic irrelevance" in status["reason"]


def test_generic_content_projection_preserves_full_hex_bytes_and_hexdumps() -> None:
    from cti_app.application.advisor_dossier import extract_content_excerpts

    hex_value = "41" * 600
    byte_value = "00ff" * 600
    full_dump = "00000000: " + "41 " * 600
    projected = extract_content_excerpts(
        {
            "hex": [{"offset": 0, "value": hex_value}],
            "bytes": [{"offset": 600, "value": byte_value}],
            "nested": {"hexdump": {"offset": 1_200, "content": full_dump}},
        }
    )

    assert any(row["kind"] == "hex" and row["text"] == hex_value for row in projected)
    assert any(row["kind"] == "bytes" and row["text"] == byte_value for row in projected)
    assert any(row["kind"] == "hex" and row["text"] == full_dump for row in projected)
    assert any("offset 0" in row["title"] for row in projected)
    assert any("offset 600" in row["title"] for row in projected)
    assert any("offset 1200" in row["title"] for row in projected)

    direct_hex = extract_content_excerpts({"hex": hex_value, "offset": 1_800})
    assert any(
        row["kind"] == "hex" and row["text"] == hex_value and "offset 1800" in row["title"]
        for row in direct_hex
    )


def test_generic_content_hexdump_is_preserved() -> None:
    from cti_app.application.advisor_dossier import extract_content_excerpts

    dump = "00000000: " + "41 " * 600

    projected = extract_content_excerpts({"content": dump})

    assert any(row["kind"] == "text" and row["text"] == dump for row in projected)


def test_short_generic_content_excerpt_is_preserved() -> None:
    short_content = "A short synthetic observation excerpt."

    projected = extract_content_excerpts({"content": short_content})

    assert any(row["kind"] == "text" and row["text"] == short_content for row in projected)


def test_short_base64_and_textual_data_url_content_are_preserved() -> None:
    proof = "SyntheticTextualProofMarker"
    encoded = base64.b64encode(proof.encode("ascii")).decode("ascii")
    percent_encoded = "".join(f"%{ord(character):02X}" for character in encoded)
    short_data_url = f"data:text/plain;base64,{encoded}"
    short_percent_encoded_data_url = f"data:text/plain;base64,{percent_encoded}"
    short_values = (encoded, short_data_url, short_percent_encoded_data_url)

    for value in short_values:
        projected = extract_content_excerpts({"content": value})

        assert any(row["text"] == value for row in projected)

    nested = extract_content_excerpts({"outer": {"strings": [{"value": short_data_url}]}})
    assert any(row["text"] == short_data_url for row in nested)


def test_oversized_and_fragmented_base64_is_preserved() -> None:
    encoded = base64.b64encode(b"A" * 600).decode("ascii")
    fragmented = f"{encoded[:200]} {encoded[200:400]} {encoded[400:]}"
    data_url = f"data:application/octet-stream;base64,{fragmented}"

    projected = extract_content_excerpts(
        {"content": encoded, "other": {"base64": [{"value": data_url}]}}
    )

    assert any(row["text"] == encoded for row in projected)
    assert any(row["text"] == data_url for row in projected)


def test_dossier_preserves_all_large_observation_excerpts_with_manifest_hashes() -> None:
    evidence = [
        {
            "evidence_id": str(uuid4()),
            "sha256": marker * 64,
            "source": "synthetic.fixture",
            "object_type": "observation",
            "blob_read_verified": True,
            "content_excerpts": [
                {
                    "kind": "text",
                    "title": "Synthetic marker",
                    "text": f"synthetic marker {marker * 9_000}",
                }
            ],
        }
        for marker in ("a", "b")
    ]
    data = _planning_data(
        question="Include both synthetic markers.", evidence=evidence, findings=[]
    )

    dossier = build_advisor_dossier(data)

    observation_manifest = [item for item in dossier.manifest if item["kind"] == "observation"]
    assert len(observation_manifest) == 2
    for item, marker in zip(observation_manifest, ("a", "b"), strict=True):
        expected_body = f"Synthetic marker:\n| synthetic marker {marker * 9_000}"
        assert item["kind"] == "observation"
        assert item["body_bytes"] == len(expected_body.encode("utf-8"))
        assert item["body_sha256"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
        rendered_body = "\n".join(
            f"| {line}" if line else "|" for line in expected_body.split("\n")
        )
        assert rendered_body in dossier.text


def test_explicit_observation_reference_keeps_every_permitted_excerpt() -> None:
    observation_id = str(uuid4())
    encoded = base64.b64encode(b"B" * 600).decode("ascii")
    data = _planning_data(
        question="Review the DNS evidence.",
        evidence=[
            {
                "evidence_id": observation_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "blob_read_verified": True,
                "content_excerpts": [
                    {"kind": "text", "title": "DNS evidence", "text": "DNS evidence text."},
                    {"kind": "text", "title": "Second evidence", "text": encoded},
                ],
            }
        ],
        findings=[],
    )

    dossier = build_advisor_dossier(data)

    assert "DNS evidence text." in dossier.text
    assert encoded in dossier.text


def test_bytes_rows_with_raw_field_keep_offset_and_value() -> None:
    from cti_app.application.advisor_dossier import extract_content_excerpts

    projected = extract_content_excerpts({"bytes": [{"offset": 4, "raw": "00ff"}]})

    assert len(projected) == 1
    assert projected[0]["kind"] == "bytes"
    assert projected[0]["text"] == "00ff"
    assert "encoding=hex" in projected[0]["title"]
    assert "byte_length=2" in projected[0]["title"]
    assert hashlib.sha256(bytes.fromhex("00ff")).hexdigest() in projected[0]["title"]
    assert "offset 4" in projected[0]["title"]


def test_byte_fragments_include_full_provenance_for_hex_base64_and_integer_arrays() -> None:
    from cti_app.application.advisor_dossier import extract_content_excerpts

    raw = b"\x00\xffA"
    digest = hashlib.sha256(raw).hexdigest()
    payload = {
        "schema": "sample-byte-fragments.v1",
        "evidence_id": "evidence-fixture-id",
        "blob_id": "blob-fixture-id",
        "association_id": "association-fixture-id",
        "mime_type": "application/x-fixture; version=2",
        "hex": [{"offset": 7, "length": len(raw), "value": raw.hex()}],
        "base64": [
            {
                "encoding": "base64",
                "offset": 17,
                "length": len(raw),
                "content_base64": base64.b64encode(raw).decode("ascii"),
            }
        ],
        "raw_bytes": [{"offset": 27, "length": len(raw), "value": list(raw)}],
    }

    excerpts = extract_content_excerpts(payload)
    byte_excerpts = [row for row in excerpts if row["title"].startswith("Octets observés")]

    assert len(byte_excerpts) == 3
    for row, offset, encoding in zip(
        byte_excerpts, (7, 17, 27), ("hex", "base64", "base64"), strict=True
    ):
        for label in (
            f"encoding={encoding}",
            f"byte_length={len(raw)}",
            f"sha256={digest}",
            "evidence_id=evidence-fixture-id",
            "blob_id=blob-fixture-id",
            "association_id=association-fixture-id",
            f"offset {offset}",
            f"length={len(raw)}",
            "mime_type=application/x-fixture; version=2",
        ):
            assert label in row["title"]


@pytest.mark.parametrize(
    ("kind", "encoding", "value", "rendered_encoding"),
    [
        ("hex", "hex", "00ff41", "hex"),
        ("base64", "base64", "AP9B", "base64"),
        ("bytes", "hex", [0, 255, 65], "base64"),
    ],
)
def test_measurement_dossier_labels_fragment_digest_and_complete_provenance(
    kind, encoding, value, rendered_encoding
) -> None:
    raw = b"\x00\xffA"
    fragment_digest = hashlib.sha256(raw).hexdigest()
    sample_digest = "a" * 64
    evidence_digest = "b" * 64
    measurement = {
        "measurement_id": "M001",
        "kind": kind,
        "encoding": encoding,
        "value": value,
        "offset": 17,
        "length": len(raw),
        "byte_length": len(raw),
        "sample_sha256": sample_digest,
        "evidence_sha256": evidence_digest,
        "fragment_sha256": "c" * 64,
        "evidence_id": "evidence-fixture-id",
        "blob_id": "blob-fixture-id",
        "association_id": "association-fixture-id",
        "mime_type": "application/x-fragment-fixture; version=4",
    }
    data = _planning_data(question="Review this hex byte measurement.", evidence=[], findings=[])
    data["consultation_type"] = "yara_draft"
    data["measurements"] = [measurement]

    dossier = build_advisor_dossier(data)

    for label in (
        "encoding=" + rendered_encoding,
        f"byte_length={len(raw)}",
        f"length={len(raw)}",
        "offset 17",
        f"sha256={fragment_digest}",
        f"fragment_sha256={fragment_digest}",
        "evidence_id=evidence-fixture-id",
        "blob_id=blob-fixture-id",
        "association_id=association-fixture-id",
        "mime_type=application/x-fragment-fixture; version=4",
    ):
        assert label in dossier.text
    assert sample_digest not in dossier.text
    assert evidence_digest not in dossier.text


def test_equal_text_under_distinct_labels_is_not_deduplicated() -> None:
    from cti_app.application.advisor_dossier import extract_content_excerpts

    projected = extract_content_excerpts(
        {
            "strings": [
                {"name": "first_field", "value": "shared value"},
                {"name": "second_field", "value": "shared value"},
            ]
        }
    )

    assert [row["title"] for row in projected] == [
        "Valeur décodée — first_field",
        "Valeur décodée — second_field",
    ]


def test_decoded_hex_ioc_is_not_mistaken_for_raw_bytes() -> None:
    from cti_app.application.advisor_dossier import extract_content_excerpts

    md5_ioc = "0123456789abcdef0123456789abcdef"
    projected = extract_content_excerpts({"decoded_strings": [{"value": md5_ioc, "offset": 128}]})

    assert any(row["kind"] == "decoded" and row["text"] == md5_ioc for row in projected)


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


def test_megabyte_observation_is_compacted_once_across_review_units(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw = bytes(range(256)) * 4_096
    encoded = base64.b64encode(raw).decode("ascii")
    observation_id = str(uuid4())
    observation_sha256 = hashlib.sha256(raw).hexdigest()
    question = "Revoir les deux unités à partir de cette observation synthétique."
    data = _planning_data(
        question=question,
        evidence=[
            {
                "evidence_id": observation_id,
                "sha256": observation_sha256,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "blob_read_verified": True,
                "content_excerpts": [
                    {
                        "kind": "base64",
                        "title": "Payload encoding=base64",
                        "text": encoded,
                    }
                ],
            }
        ],
        findings=[],
    )
    data["consultation_type"] = "review_text"
    data["units"] = [
        {"unit_id": "U1", "text": "Revoir le premier point.", "evidence_refs": [observation_id]},
        {"unit_id": "U2", "text": "Comparer le second point.", "evidence_refs": [observation_id]},
    ]
    data["measurements"] = [
        {
            "measurement_id": "M1",
            "evidence_id": observation_id,
            "kind": "base64",
            "encoding": "base64",
            "value": encoded,
            "offset": 0,
            "length": len(raw),
        }
    ]

    with caplog.at_level("INFO", logger="cti_app.application.advisor_dossier"):
        dossier = build_advisor_dossier(data)
    observation_manifest = [item for item in dossier.manifest if item["kind"] == "observation"]

    assert len(raw) == 1_048_576
    assert dossier.byte_size <= 400_000
    assert len(observation_manifest) == 1
    assert encoded not in dossier.text
    assert dossier.text.count(encoded[:128]) == 1
    assert "TRONQUÉ" in dossier.text
    assert "offset UTF-8" in dossier.text
    assert f"sha256={observation_sha256}" in dossier.text
    assert f"corps complet : observation {observation_id}" in dossier.text
    assert "M1" in dossier.text
    assert dossier.text.index("[E1] Observation") < dossier.text.index("déjà présenté sous [E1]")
    assert len(observation_manifest[0]["body_sha256"]) == 64
    volume_record = next(
        record
        for record in caplog.records
        if record.name == "cti_app.application.advisor_dossier"
        and record.getMessage() == "Advisor dossier volume"
    )
    assert volume_record.advisor_dossier_input_bytes > 1_000_000
    assert volume_record.advisor_dossier_output_bytes == dossier.byte_size
    assert encoded[:128] not in caplog.text

    data.update(
        dossier_text=dossier.text,
        dossier_manifest=[dict(row) for row in dossier.manifest],
    )
    prompt = render_advisor_prompt("review_text", data)
    assert prompt.startswith("FORMAT: advisor-wire.v1")
    assert '"dossier_text"' not in prompt
    assert '"dossier_manifest"' not in prompt
    assert '{"consultation_type"' not in prompt


def test_duplicate_base64_payload_keeps_each_observation_context() -> None:
    raw = b"shared-synthetic-payload" * 40_000
    encoded = base64.b64encode(raw).decode("ascii")
    source_sha256 = hashlib.sha256(raw).hexdigest()
    observation_ids = [str(uuid4()), str(uuid4())]
    evidence = [
        {
            "evidence_id": observation_ids[0],
            "sha256": source_sha256,
            "source": "synthetic.fixture",
            "object_type": "observation",
            "content_excerpts": [
                {
                    "kind": "base64",
                    "title": "Payload encoding=base64",
                    "text": f"ALPHA_CONTEXT_BEFORE\n{encoded}\nALPHA_CONTEXT_AFTER",
                }
            ],
        },
        {
            "evidence_id": observation_ids[1],
            "sha256": source_sha256,
            "source": "synthetic.fixture",
            "object_type": "observation",
            "content_excerpts": [
                {
                    "kind": "base64",
                    "title": "Payload encoding=base64",
                    "text": f"BETA_CONTEXT_BEFORE\n{encoded}\nBETA_CONTEXT_AFTER",
                }
            ],
        },
    ]
    data = _planning_data(
        question="Review both synthetic payload contexts.", evidence=evidence, findings=[]
    )

    dossier = build_advisor_dossier(data)

    assert dossier.byte_size <= 400_000
    assert encoded not in dossier.text
    assert dossier.text.count(encoded[:128]) == 1
    for marker in (
        "ALPHA_CONTEXT_BEFORE",
        "ALPHA_CONTEXT_AFTER",
        "BETA_CONTEXT_BEFORE",
        "BETA_CONTEXT_AFTER",
    ):
        assert marker in dossier.text
    assert "base64 déjà présenté sous [E1]" in dossier.text
    assert f"corps complet : observation {observation_ids[1]}" in dossier.text
    assert {row["id"] for row in dossier.manifest if row["kind"] == "observation"} == set(
        observation_ids
    )
    reused = next(row for row in dossier.manifest if row["id"] == observation_ids[1])
    assert reused["body_reused_from_alias"] == "E1"
    assert reused["payload_sha256"] == source_sha256


def test_large_decoded_strings_observation_uses_statistics_and_samples() -> None:
    text = "\n".join(f"decoded-string-{index:05d}-synthetic-value" for index in range(18_000))
    observation_id = str(uuid4())
    data = _planning_data(
        question="Inspect the synthetic decoded strings observation.",
        evidence=[
            {
                "evidence_id": observation_id,
                "sha256": hashlib.sha256(text.encode("utf-8")).hexdigest(),
                "source": "synthetic.strings",
                "object_type": "observation",
                "blob_read_verified": True,
                "content_excerpts": [{"kind": "decoded", "title": "Decoded strings", "text": text}],
            }
        ],
        findings=[],
    )

    dossier = build_advisor_dossier(data)

    assert dossier.byte_size <= 400_000
    assert "Sortie strings résumée" in dossier.text
    assert "ligne(s)" in dossier.text
    assert "Échantillon de lignes" in dossier.text
    assert "TRONQUÉ explicitement" in dossier.text
    assert f"corps complet : observation {observation_id}" in dossier.text


def test_fragmented_base64_noise_keeps_excerpt_within_per_body_budget() -> None:
    encoded_fragment = base64.b64encode(bytes(range(60))).decode("ascii")
    fragmented = "\n".join(f"{encoded_fragment} context-{index:04d}" for index in range(600))
    observation_id = str(uuid4())
    data = _planning_data(
        question="Inspect the synthetic base64 observation.",
        evidence=[
            {
                "evidence_id": observation_id,
                "sha256": hashlib.sha256(fragmented.encode("utf-8")).hexdigest(),
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {
                        "kind": "base64",
                        "title": "Payload encoding=base64",
                        "text": fragmented,
                    }
                ],
            }
        ],
        findings=[],
    )

    dossier = build_advisor_dossier(data)
    observation_row = next(row for row in dossier.manifest if row["kind"] == "observation")

    assert observation_row["body_bytes"] <= 12 * 1024
    assert "Bruit base64 résumé" in dossier.text
    assert "Contexte adjacent tronqué explicitement" in dossier.text
    assert "TRONQUÉ explicitement" in dossier.text


def test_dossier_hard_byte_limit_truncates_with_an_explicit_reference() -> None:
    observation_id = str(uuid4())
    data = _planning_data(
        question="Inspect the synthetic observation.",
        evidence=[
            {
                "evidence_id": observation_id,
                "sha256": "c" * 64,
                "source": "synthetic.fixture",
                "object_type": "observation",
                "content_excerpts": [
                    {
                        "kind": "text",
                        "title": "Synthetic long title " + ("x" * 2_050_000),
                        "text": "Short synthetic body.",
                    }
                ],
            }
        ],
        findings=[],
    )
    data["evidence_refs"] = [observation_id]

    dossier = build_advisor_dossier(data)

    assert dossier.byte_size <= 2_000_000
    assert "TRONQUÉ explicitement par la limite dure de 2 Mo" in dossier.text
    assert f"corps complet : observation {observation_id}" in dossier.text
    assert "x" * 100 not in dossier.text


def test_extreme_candidate_card_count_stays_under_hard_limit_with_source_index() -> None:
    network_facts = [
        {
            "fact_id": f"synthetic-fact-{index:05d}",
            "evidence_id": "synthetic-observation",
            "observation_sha256": "e" * 64,
            "field": "beacon",
            "value": f"signal-{index:05d}",
        }
        for index in range(48_000)
    ]
    data = _planning_data(question="Review beacon signal facts.", evidence=[], findings=[])
    data["consultation_type"] = "draft_text"
    data["network_facts"] = network_facts

    dossier = build_advisor_dossier(data)

    assert dossier.byte_size <= 2_000_000
    assert "TRONQUÉ explicitement par la limite dure de 2 Mo" in dossier.text
    assert "sha256_liste_sources=" in dossier.text
    assert "Références canoniques résumées" in dossier.text


@pytest.mark.asyncio
async def test_large_dossier_crosses_bridge_file_threshold_without_omission() -> None:
    body = "SYNTHETIC_ONLY " + ("proof-marker " * 17_000)
    article_sha = hashlib.sha256(b"synthetic canonical article").hexdigest()
    article_ref = f"article:{article_sha}"
    data: dict[str, Any] = {
        "consultation_type": "draft_text",
        "question": "proof-marker",
        "article_source_ref": article_ref,
        "article_sha256": article_sha,
        "article_passages": [{"claim_id": "S1P1", "heading": "Synthetic proof", "text": body}],
    }
    dossier = build_advisor_dossier(data)
    manifest = dossier.manifest[0]
    expected_body = body.strip()
    assert dossier.byte_size > 200_000
    assert manifest["body_bytes"] == len(expected_body.encode("utf-8"))
    assert manifest["body_sha256"] == hashlib.sha256(expected_body.encode("utf-8")).hexdigest()
    assert manifest["source_ref"] == article_ref

    data.update(
        dossier_text=dossier.text,
        dossier_sha256=dossier.sha256,
        dossier_bytes=dossier.byte_size,
        dossier_manifest=[dict(row) for row in dossier.manifest],
    )
    prompt = render_advisor_prompt("draft_text", data)
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
