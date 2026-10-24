from __future__ import annotations

import hashlib
import json
import socket
import subprocess
from pathlib import Path

import pytest

from cti_app.application.advisor_semantic_parser import (
    ADVISOR_RESPONSE_SCHEMA_V4,
    ADVISOR_SOURCE_PARSER_VERSION_V4,
    parse_advisor_semantic_response,
)
from cti_app.application.investigation_advisor_tools import (
    _advisor_output_result_status,
    _parse_advisor_output,
)
from cti_app.domain.investigation import ToolInvocationStatus


def _prompt_data() -> dict:
    identifier = "observation:synthetic-1"
    digest = "a" * 64
    return {
        "consultation_type": "plan_investigation",
        "question": "Review the observed JSON configuration.",
        "transmitted_refs": [{"kind": "observation", "id": identifier, "sha256": digest}],
        "evidence": [
            {
                "evidence_id": identifier,
                "sha256": digest,
                "object_type": "observation",
                "content_excerpts": [
                    {
                        "kind": "json",
                        "title": "Observed JSON configuration",
                        "text": '{"mode":"synthetic","label":"évidence 🧪"}',
                    }
                ],
            }
        ],
    }


def _response(
    *,
    status: str = "succeeded",
    opinions: list[dict] | None = None,
    incomplete_units: list[str] | None = None,
) -> str:
    return json.dumps(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA_V4,
            "consultation_type": "plan_investigation",
            "status": status,
            "opinions": opinions
            if opinions is not None
            else [
                {
                    "unit_id": "U1",
                    "text": "Inspect the observed configuration.",
                    "source_aliases": ["E1"],
                }
            ],
            "incomplete_units": incomplete_units or [],
        },
        ensure_ascii=False,
    )


def test_refused_is_terminal_and_preserves_raw_bytes_and_hash() -> None:
    raw = _response(status="refused", opinions=[{"unit_id": "U1", "text": "must be dropped"}])

    result = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] == "refused"
    assert result["opinions"] == []
    assert result["candidates"] == []
    assert result["raw_output"] == raw
    assert result["raw_output_bytes"] == len(raw.encode("utf-8"))
    assert result["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert result["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4


def test_partial_requires_and_reports_explicit_incomplete_units() -> None:
    raw = _response(status="partial", incomplete_units=["U2"])

    result = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] == "partial"
    assert result["incomplete_units"] == ["U2"]
    assert result["opinions"][0]["unit_id"] == "U1"

    missing_units = parse_advisor_semantic_response(
        _response(status="partial"),
        consultation_type="plan_investigation",
        prompt_data=_prompt_data(),
    )
    assert missing_units["status"] == "invalid"
    assert missing_units["opinions"] == []


@pytest.mark.parametrize(
    ("status", "expected"), [("invalid", "invalid"), ("ambiguous", "ambiguous")]
)
def test_terminal_invalid_and_ambiguous_statuses_drop_all_opinions(
    status: str, expected: str
) -> None:
    result = parse_advisor_semantic_response(
        _response(status=status),
        consultation_type="plan_investigation",
        prompt_data=_prompt_data(),
    )

    assert result["status"] == expected
    assert result["opinions"] == []
    assert result["candidates"] == []


@pytest.mark.parametrize(
    "raw",
    [
        "not a v4 semantic response",
        '{"schema":"advisor-response.v3","status":"succeeded"}',
        '{"schema":"advisor-response.v4","schema":"advisor-response.v4"}',
    ],
)
def test_invalid_or_ambiguous_envelopes_fail_closed(raw: str) -> None:
    result = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] in {"invalid", "ambiguous"}
    assert result["opinions"] == []
    assert result["candidates"] == []


def test_ambiguous_source_alias_is_not_resolved() -> None:
    data = _prompt_data()
    data["dossier_manifest"] = [
        {"alias": "E1", "kind": "observation", "id": "one", "sha256": "b" * 64},
        {"alias": "E1", "kind": "observation", "id": "two", "sha256": "c" * 64},
    ]

    result = parse_advisor_semantic_response(
        _response(), consultation_type="plan_investigation", prompt_data=data
    )

    assert result["status"] == "ambiguous"
    assert result["opinions"] == []
    assert result["unresolved_refs"][0]["reason"] == "ambiguous_alias"


@pytest.mark.parametrize(
    ("prompt_mutation", "reason"),
    [
        (
            lambda data: data.update(disclosure_restricted_refs=["observation:synthetic-1"]),
            "restricted",
        ),
        (
            lambda data: data.update(
                dossier_manifest=[
                    {
                        "alias": "E1",
                        "kind": "observation",
                        "id": "observation:stale",
                        "sha256": "b" * 64,
                    }
                ],
            ),
            "stale_alias",
        ),
    ],
)
def test_restricted_and_stale_aliases_remain_unresolved(prompt_mutation, reason: str) -> None:
    data = _prompt_data()
    prompt_mutation(data)

    result = parse_advisor_semantic_response(
        _response(), consultation_type="plan_investigation", prompt_data=data
    )

    assert result["status"] == "partial"
    assert result["opinions"][0]["source_refs"] == []
    assert result["unresolved_refs"][0]["reason"] == reason
    assert result["validated_facts"] == []
    assert result["incomplete_units"] == ["U1"]


def test_absent_alias_is_reported_as_missing_not_a_fact() -> None:
    raw = _response(
        opinions=[{"unit_id": "U1", "text": "Unverified proposal.", "source_aliases": ["E99"]}]
    )

    result = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] == "partial"
    assert result["unresolved_refs"] == [
        {"alias": "E99", "reason": "missing_alias", "unit_id": "U1"}
    ]
    assert result["validated_facts"] == []


def test_conflicting_kind_id_sha_refuses_without_candidates() -> None:
    data = _prompt_data()
    data["transmitted_refs"].append(
        {"kind": "observation", "id": "observation:synthetic-1", "sha256": "b" * 64}
    )

    result = parse_advisor_semantic_response(
        _response(), consultation_type="plan_investigation", prompt_data=data
    )

    assert result["status"] == "refused"
    assert result["refusal_reason"] == "source_identity_conflict"
    assert result["opinions"] == []
    assert result["candidates"] == []


def test_versioned_opinion_source_ref_is_exact() -> None:
    opinion_id = "75f0accb-98f3-4606-afe8-03c0fa20d7ee"
    digest = "d" * 64
    source_ref = f"opinion:{opinion_id}:v7:{digest}:key_findings"
    data = {
        "consultation_type": "review_text",
        "question": "Review the exact opinion unit.",
        "units": [
            {
                "unit_id": "U1",
                "opinion_id": opinion_id,
                "opinion_version": 7,
                "opinion_sha256": digest,
                "source_ref": source_ref,
                "block_type": "key_findings",
                "text": "An immutable opinion unit.",
            }
        ],
    }
    raw = json.dumps(
        {
            "schema": ADVISOR_RESPONSE_SCHEMA_V4,
            "consultation_type": "review_text",
            "status": "succeeded",
            "opinions": [{"unit_id": "U1", "text": "Review conclusion.", "source_aliases": ["T1"]}],
            "incomplete_units": [],
        }
    )

    result = parse_advisor_semantic_response(raw, consultation_type="review_text", prompt_data=data)

    assert result["status"] == "succeeded"
    assert result["opinions"][0]["source_refs"] == [
        {
            "alias": "T1",
            "kind": "opinion",
            "id": source_ref,
            "sha256": digest,
            "source_ref": source_ref,
            "validated": False,
        }
    ]


def test_wire_handles_unicode_crlf_whitespace_and_json_content_as_evidence() -> None:
    raw = (
        "\ufeff  advisor-wire.v1  \r\n"
        "  SCHEMA : advisor-response.v4  \r\n"
        "TYPE: plan_investigation\r\n"
        "STATUS: succeeded\r\n"
        'OPINION: {"observation":"évidence 🧪"}\r\n'
        "REF: E1\r\n"
    )

    result = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] == "succeeded"
    assert result["opinions"][0]["text"] == '{"observation":"évidence 🧪"}'
    assert result["raw_output"] == raw
    assert result["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_wire_blank_incomplete_unit_is_invalid_before_partial_result() -> None:
    raw = (
        "advisor-wire.v1\nSCHEMA: advisor-response.v4\n"
        "TYPE: plan_investigation\nSTATUS: partial\n"
        "UNIT: U1\nUNIT_TEXT: An incomplete opinion.\n"
        "UNIT_REF: E1\nINCOMPLETE_UNIT:\n"
    )

    result = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] == "invalid"
    assert result["opinions"] == []
    assert result["candidates"] == []


def test_parser_runs_without_egress_or_bridge_capabilities(monkeypatch) -> None:
    def forbidden(*_args, **_kwargs):
        raise AssertionError("semantic parser attempted egress")

    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "run", forbidden)

    result = parse_advisor_semantic_response(
        _response(), consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] == "succeeded"


def test_operational_secret_refusal_keeps_only_immutable_raw_provenance() -> None:
    raw = (
        "advisor-wire.v1\nSCHEMA: advisor-response.v4\n"
        "TYPE: plan_investigation\nSTATUS: succeeded\n"
        "OPINION: OPENAI_BRIDGE_API_KEY=synthetic-secret-value\nREF: E1\n"
    )

    result = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert result["status"] == "refused"
    assert result["refusal_reason"] == "operational_secret_detected"
    assert result["opinions"] == []
    assert result["raw_output"] is None
    assert result["raw_output_retained"] is False
    assert result["raw_output_bytes"] == len(raw.encode("utf-8"))
    assert result["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_explicitly_configured_credential_in_output_is_refused() -> None:
    credential = "configured-only-synthetic-token-75b2ac4f"
    raw = _response(
        opinions=[
            {
                "unit_id": "U1",
                "text": f"The configured credential is {credential}.",
                "source_aliases": ["E1"],
            }
        ]
    )

    result = parse_advisor_semantic_response(
        raw,
        consultation_type="plan_investigation",
        prompt_data=_prompt_data(),
        operational_secret_values=(credential,),
    )

    assert result["status"] == "refused"
    assert result["refusal_reason"] == "operational_secret_detected"
    assert result["opinions"] == []
    assert result["raw_output"] is None
    assert result["raw_output_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()


def test_benign_cwd_and_home_paths_do_not_change_parser_result(monkeypatch) -> None:
    cwd_values = ["/synthetic/one/worktree", "/synthetic/two/worktree"]
    home_values = ["/synthetic/one/home", "/synthetic/two/home"]
    raw = _response(
        opinions=[
            {
                "unit_id": "U1",
                "text": "Benign paths: " + " ".join((*cwd_values, *home_values)),
                "source_aliases": ["E1"],
            }
        ]
    )
    current = {"cwd": cwd_values[0], "home": home_values[0]}
    monkeypatch.setattr(Path, "cwd", lambda: Path(current["cwd"]))
    monkeypatch.setattr(Path, "home", lambda: Path(current["home"]))
    monkeypatch.setenv("HOME", home_values[0])

    first = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )
    current.update(cwd=cwd_values[1], home=home_values[1])
    monkeypatch.setenv("HOME", home_values[1])
    second = parse_advisor_semantic_response(
        raw, consultation_type="plan_investigation", prompt_data=_prompt_data()
    )

    assert first["status"] == second["status"] == "succeeded"
    assert first == second


def test_existing_parse_caller_keeps_v3_default_and_requires_explicit_v4_selection() -> None:
    raw = _response()
    prompt_data = _prompt_data()
    prompt_data["consultation_type"] = "draft_text"
    response = json.loads(raw)
    response["consultation_type"] = "draft_text"
    raw = json.dumps(response, ensure_ascii=False)
    caller_args = {
        "raw": raw,
        "prompt": "",
        "available_tools": {},
        "available_tool_schemas": {},
        "evidence_refs": (),
        "source_refs": (),
        "consultation_type": "draft_text",
        "prompt_data": prompt_data,
    }

    default_result = _parse_advisor_output(**caller_args)
    selected_result = _parse_advisor_output(
        **caller_args, parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4
    )

    assert default_result is None
    assert selected_result is not None
    assert selected_result["parser_version"] == ADVISOR_SOURCE_PARSER_VERSION_V4
    assert selected_result["status"] == "succeeded"


@pytest.mark.parametrize("status", ["invalid", "ambiguous"])
def test_explicit_v4_failures_map_to_needs_review_without_changing_v3_default(status: str) -> None:
    caller_args = {
        "raw": _response(status=status),
        "prompt": "",
        "available_tools": {},
        "available_tool_schemas": {},
        "evidence_refs": (),
        "source_refs": (),
        "consultation_type": "plan_investigation",
        "prompt_data": _prompt_data(),
    }

    parsed_v4 = _parse_advisor_output(
        **caller_args, parser_version=ADVISOR_SOURCE_PARSER_VERSION_V4
    )
    assert parsed_v4 is not None
    advisor_status, invocation_status = _advisor_output_result_status(parsed_v4)

    assert parsed_v4["status"] == status
    assert advisor_status == status
    assert invocation_status is ToolInvocationStatus.NEEDS_REVIEW
    assert _advisor_output_result_status({"status": "invalid"}) == (
        "succeeded",
        ToolInvocationStatus.SUCCEEDED,
    )
