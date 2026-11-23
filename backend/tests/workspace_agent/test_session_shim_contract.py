"""M24-05 H2 red oracles: closed MCP catalogue, route mapping, D7 signing.

Normative source: ``docs/design/investigation-workshop/SESSION-RUNNER.md`` §7
and ``docs/adr/ADR-0011-investigation-workshop-coordination.md`` D7. Everything
here is offline: the transport is in-process and counts calls, so any rejected
tool proves the absence of a network effect.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest
from sidecars.workspace_agent.sessions import catalog, shim

from tests.workspace_agent import session_shim_support as support

FIXTURE_PATH = Path(__file__).with_name("fixtures") / "session_signing_v1.json"


def _tools_call(
    name: str, arguments: Mapping[str, object], *, request_id: int = 7
) -> dict[str, object]:
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": "tools/call",
        "params": {"name": name, "arguments": dict(arguments)},
    }


class RecordingTransport:
    """In-process transport that records exactly what the shim would send."""

    def __init__(self, response: shim.HttpResponse | None = None) -> None:
        self.calls: list[dict[str, object]] = []
        self.response = response or shim.HttpResponse(
            status=200, content_type="application/json", body=b'{"ok":true}'
        )

    def send(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        body: bytes,
        timeout: float,
    ) -> shim.HttpResponse:
        self.calls.append(
            {
                "method": method,
                "path": path,
                "headers": dict(headers),
                "body": body,
                "timeout": timeout,
            }
        )
        return self.response


def _server(
    transport: RecordingTransport, *, clock_value: int = support.FIXED_TIMESTAMP
) -> shim.ShimServer:
    return shim.ShimServer(
        session_id=support.SESSION_ID,
        secret=support.SECRET,
        backend_url="http://127.0.0.1:1",
        clock=lambda: clock_value,
        transport=transport,
    )


def _result_envelope(response: Mapping[str, object]) -> dict[str, object]:
    assert "result" in response, response
    result = cast(dict[str, object], response["result"])
    content = cast(list[dict[str, object]], result["content"])
    assert len(content) == 1
    text = cast(str, content[0]["text"])
    envelope = json.loads(text)
    assert isinstance(envelope, dict)
    return cast(dict[str, object], envelope)


def test_unknown_tool_rejected_without_network() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    for name in ("autowork_shell", "autowork_http_proxy", "advisor.consult", "local.run_command"):
        response = server.handle_message(_tools_call(name, {}))
        assert response is not None
        error = cast(dict[str, object], response["error"])
        assert error["code"] == -32602
        assert error["data"] == {"code": "unknown_tool"}
    assert transport.calls == []


def test_hmac_matches_adr_d7_vector() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    assert fixture["scheme"].startswith("v1=HMAC-SHA256(secret, method")
    vectors = fixture["vectors"]
    assert isinstance(vectors, list) and len(vectors) >= 4
    for vector in vectors:
        secret = base64.b64decode(vector["secret_b64"])
        body = vector["body"].encode("utf-8")
        expected = support.reference_signature(
            secret,
            method=vector["method"],
            path=vector["path_and_query"],
            session_id=vector["session_id"],
            body=body,
            idempotency_key=vector["idempotency_key"],
            timestamp=vector["timestamp"],
        )
        assert expected == vector["expected_signature"], vector["name"]
        actual = shim.sign_request(
            secret,
            method=vector["method"],
            path=vector["path_and_query"],
            session_id=vector["session_id"],
            body=body,
            idempotency_key=vector["idempotency_key"],
            timestamp=vector["timestamp"],
        )
        assert actual == vector["expected_signature"], vector["name"]


def test_bridge_consult_maps_only_to_advisor_consult() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    arguments = {
        "investigation_id": support.INVESTIGATION_ID,
        "consultation_type": "pivot_strategy",
        "question": "Synthetic: which pivot discriminates the two hypotheses?",
    }
    response = server.handle_message(_tools_call("autowork_bridge_consult", arguments))
    assert response is not None
    envelope = _result_envelope(response)
    assert envelope["data_not_instructions"] is True
    assert envelope["status"] == 200
    assert len(transport.calls) == 1
    call = transport.calls[0]
    assert call["method"] == "POST"
    assert call["path"] == (
        f"/api/investigations/{support.INVESTIGATION_ID}"
        f"/agent-sessions/{support.SESSION_ID}/tool-calls"
    )
    body = json.loads(cast(bytes, call["body"]).decode("utf-8"))
    assert body == {
        "tool_name": "advisor.consult",
        "schema_version": "advisor-consult.v1",
        "arguments": {
            "consultation_type": "pivot_strategy",
            "question": "Synthetic: which pivot discriminates the two hypotheses?",
        },
    }
    assert "investigation_id" not in body["arguments"]

    # A broker name is not an MCP tool name, and no argument can rewrite the
    # fixed tool_name of another tool.
    other = server.handle_message(
        _tools_call(
            "autowork_task_heartbeat",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "task_id": support.TASK_ID,
                "expected_status_version": 2,
                "lease_fence": 7,
                "tool_name": "advisor.consult",
            },
        )
    )
    assert other is not None
    assert cast(dict[str, object], other["error"])["data"] == {
        "code": "invalid_arguments",
        "field": "tool_name",
        "reason": "additional_property",
    }
    assert len(transport.calls) == 1


def test_identical_arguments_replay_client_key() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    arguments = {
        "investigation_id": support.INVESTIGATION_ID,
        "task_id": support.TASK_ID,
        "expected_status_version": 4,
        "lease_fence": 11,
        "nonce": "retry-0001",
    }
    first = server.handle_message(_tools_call("autowork_task_heartbeat", arguments, request_id=1))
    reordered = dict(reversed(list(arguments.items())))
    second = server.handle_message(_tools_call("autowork_task_heartbeat", reordered, request_id=2))
    assert first is not None and second is not None
    assert len(transport.calls) == 2
    first_call, second_call = transport.calls
    expected_key = support.expected_client_key(support.SESSION_ID, "task.heartbeat", arguments)
    assert first_call["headers"]["Idempotency-Key"] == expected_key
    assert second_call["headers"]["Idempotency-Key"] == expected_key
    assert (
        first_call["headers"]["X-AutoWork-Agent-Signature"]
        == second_call["headers"]["X-AutoWork-Agent-Signature"]
    )

    changed = dict(arguments, nonce="retry-0002")
    third = server.handle_message(_tools_call("autowork_task_heartbeat", changed, request_id=3))
    assert third is not None
    assert len(transport.calls) == 3
    assert transport.calls[2]["headers"]["Idempotency-Key"] != expected_key


def test_signed_headers_use_the_injected_clock_and_fixed_route() -> None:
    transport = RecordingTransport()
    server = _server(transport, clock_value=1_763_800_123)
    response = server.handle_message(
        _tools_call(
            "autowork_task_heartbeat",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "task_id": support.TASK_ID,
                "expected_status_version": 5,
                "lease_fence": 9,
            },
        )
    )
    assert response is not None
    call = transport.calls[0]
    headers = cast(dict[str, str], call["headers"])
    assert headers["X-AutoWork-Agent-Session"] == support.SESSION_ID
    assert headers["X-AutoWork-Agent-Timestamp"] == "1763800123"
    assert headers["X-AutoWork-Agent-Signature"] == support.reference_signature(
        support.SECRET,
        method="POST",
        path=cast(str, call["path"]),
        session_id=support.SESSION_ID,
        body=cast(bytes, call["body"]),
        idempotency_key=headers["Idempotency-Key"],
        timestamp=1_763_800_123,
    )
    assert "Authorization" not in headers
    body = json.loads(cast(bytes, call["body"]).decode("utf-8"))
    assert body == {
        "command": "heartbeat",
        "expected_status_version": 5,
        "lease_fence": 9,
    }


def test_submit_result_and_handoff_use_their_fixed_routes() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    submitted = server.handle_message(
        _tools_call(
            "autowork_task_submit_result",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "task_id": support.TASK_ID,
                "expected_status_version": 6,
                "result_refs": [{"kind": "note", "path": "results/result.md"}],
            },
            request_id=1,
        )
    )
    handoff = server.handle_message(
        _tools_call(
            "autowork_handoff_submit",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "task_id": support.TASK_ID,
                "to_actor": {"kind": "human", "actor_id": "analyst-1"},
                "next_action": "Review the synthetic pivot result.",
            },
            request_id=2,
        )
    )
    assert submitted is not None and handoff is not None
    assert len(transport.calls) == 2
    submit_call, handoff_call = transport.calls
    assert submit_call["path"] == (
        f"/api/investigations/{support.INVESTIGATION_ID}/tasks/{support.TASK_ID}/commands"
    )
    assert json.loads(cast(bytes, submit_call["body"]).decode("utf-8")) == {
        "command": "submit",
        "expected_status_version": 6,
        "result_refs": [{"kind": "note", "path": "results/result.md"}],
    }
    assert handoff_call["path"] == (f"/api/investigations/{support.INVESTIGATION_ID}/handoffs")
    assert json.loads(cast(bytes, handoff_call["body"]).decode("utf-8")) == {
        "task_id": support.TASK_ID,
        "to_actor": {"kind": "human", "actor_id": "analyst-1"},
        "next_action": "Review the synthetic pivot result.",
    }


def test_instruction_tools_are_fixed_pull_and_ack() -> None:
    """Exactly two fixed instruction tools; no URL/path/method/header knob."""
    instruction_specs = [
        spec for spec in catalog.CATALOGUE if spec.broker_name.startswith("instruction.")
    ]
    assert [spec.mcp_name for spec in instruction_specs] == [
        "autowork_instruction_pending",
        "autowork_instruction_ack",
    ]
    pending, ack = instruction_specs
    for spec, expected_properties in (
        (pending, {"investigation_id"}),
        (ack, {"investigation_id", "instruction_ids"}),
    ):
        assert spec.method == "POST"
        assert spec.path_template == catalog.INSTRUCTIONS_PATH
        assert spec.path_params == ("investigation_id",)
        assert spec.body_kind == "instruction"
        assert spec.timeout_seconds == 120.0
        schema = spec.input_schema
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        properties = cast(dict[str, object], schema["properties"])
        assert set(properties) == expected_properties
        assert set(cast(list[str], schema["required"])) == expected_properties
    ack_properties = cast(dict[str, object], ack.input_schema["properties"])
    ids_schema = cast(dict[str, object], ack_properties["instruction_ids"])
    assert ids_schema["type"] == "array"
    assert ids_schema["minItems"] == 1
    assert ids_schema["maxItems"] == 32
    assert ids_schema["uniqueItems"] is True
    assert cast(dict[str, object], ids_schema["items"])["pattern"] == (
        r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$"
    )
    for spec in instruction_specs:
        forbidden = set(cast(dict[str, object], spec.input_schema["properties"])) & {
            "url",
            "uri",
            "method",
            "path",
            "headers",
            "proxy",
        }
        assert forbidden == set()


def test_instruction_tools_build_fixed_bodies_and_paths() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    first_id = "12345678-1234-4abc-8def-1234567890ab"
    second_id = "abcdef12-3456-4abc-9def-abcdef123456"
    pending = server.handle_message(
        _tools_call(
            "autowork_instruction_pending",
            {"investigation_id": support.INVESTIGATION_ID},
            request_id=1,
        )
    )
    ack = server.handle_message(
        _tools_call(
            "autowork_instruction_ack",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "instruction_ids": [first_id, second_id],
            },
            request_id=2,
        )
    )
    assert pending is not None and ack is not None
    assert len(transport.calls) == 2
    expected_path = catalog.INSTRUCTIONS_PATH.format(
        investigation_id=support.INVESTIGATION_ID, session_id=support.SESSION_ID
    )
    for call in transport.calls:
        assert call["method"] == "POST"
        assert call["path"] == expected_path
        assert call["timeout"] == 120.0
    assert json.loads(cast(bytes, transport.calls[0]["body"]).decode("utf-8")) == {"action": "pull"}
    assert json.loads(cast(bytes, transport.calls[1]["body"]).decode("utf-8")) == {
        "action": "ack",
        "instruction_ids": [first_id, second_id],
    }
    pending_headers = cast(dict[str, str], transport.calls[0]["headers"])
    ack_headers = cast(dict[str, str], transport.calls[1]["headers"])
    assert pending_headers["Idempotency-Key"] == support.expected_client_key(
        support.SESSION_ID, "instruction.pending", {"investigation_id": support.INVESTIGATION_ID}
    )
    assert ack_headers["Idempotency-Key"] == support.expected_client_key(
        support.SESSION_ID,
        "instruction.ack",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "instruction_ids": [first_id, second_id],
        },
    )


def test_fix_t_tools_use_the_broker_route_with_closed_bodies() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    run_script = server.handle_message(
        _tools_call(
            "autowork_analysis_run_script",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "script": "print('confined')",
                "interpreter": "python3",
            },
            request_id=1,
        )
    )
    note = server.handle_message(
        _tools_call(
            "autowork_note_capture",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "relpath": "analyse/first.md",
                "text": "# Note agent\n",
            },
            request_id=2,
        )
    )
    contribution = server.handle_message(
        _tools_call(
            "autowork_contribution_import",
            {
                "investigation_id": support.INVESTIGATION_ID,
                "workspace_id": "ws-" + "a" * 32,
                "workspace_revision_id": "rev-" + "b" * 32,
                "category": "work_contribution",
            },
            request_id=3,
        )
    )
    assert run_script is not None and note is not None and contribution is not None
    assert len(transport.calls) == 3
    bodies = [json.loads(cast(bytes, call["body"]).decode("utf-8")) for call in transport.calls]
    assert all(
        call["path"]
        == (
            f"/api/investigations/{support.INVESTIGATION_ID}"
            f"/agent-sessions/{support.SESSION_ID}/tool-calls"
        )
        for call in transport.calls
    )
    assert bodies[0]["tool_name"] == "analysis.run_script"
    assert bodies[0]["arguments"] == {
        "script": "print('confined')",
        "interpreter": "python3",
    }
    assert bodies[1]["tool_name"] == "note.capture"
    assert bodies[1]["arguments"] == {
        "relpath": "analyse/first.md",
        "text": "# Note agent\n",
    }
    assert bodies[2]["tool_name"] == "contribution.import"
    assert bodies[2]["arguments"] == {
        "workspace_id": "ws-" + "a" * 32,
        "workspace_revision_id": "rev-" + "b" * 32,
        "category": "work_contribution",
    }

    for relpath in ("../escape.md", "/absolute.md", "a/./b.md", "a/../b.md", "a\\b.md"):
        refused = server.handle_message(
            _tools_call(
                "autowork_note_capture",
                {
                    "investigation_id": support.INVESTIGATION_ID,
                    "relpath": relpath,
                    "text": "x",
                },
                request_id=4,
            )
        )
        assert refused is not None
        error = cast(dict[str, object], refused["error"])
        assert error["code"] == -32602
        assert cast(dict[str, object], error["data"])["code"] == "invalid_arguments"
    assert len(transport.calls) == 3


SCHEMA_CASES: tuple[tuple[str, dict[str, object], str, str], ...] = (
    (
        "autowork_bridge_consult",
        {"investigation_id": support.INVESTIGATION_ID},
        "consultation_type",
        "required",
    ),
    (
        "autowork_bridge_consult",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "consultation_type": "write_shell",
        },
        "consultation_type",
        "enum",
    ),
    (
        "autowork_bridge_consult",
        {
            "investigation_id": "not-a-uuid",
            "consultation_type": "critique",
        },
        "investigation_id",
        "pattern",
    ),
    (
        "autowork_bridge_consult",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "consultation_type": "critique",
            "question": "x",
            "url": "http://example.invalid/",
        },
        "url",
        "additional_property",
    ),
    (
        "autowork_bridge_consult",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "consultation_type": "critique",
            "question": "x" * 70_000,
        },
        "question",
        "max_length",
    ),
    (
        "autowork_task_heartbeat",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "task_id": support.TASK_ID,
            "expected_status_version": 0,
            "lease_fence": 1,
        },
        "expected_status_version",
        "minimum",
    ),
    (
        "autowork_handoff_submit",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "task_id": support.TASK_ID,
            "to_actor": {"kind": "system", "actor_id": "nobody"},
            "next_action": "x",
        },
        "to_actor.kind",
        "enum",
    ),
    (
        "autowork_instruction_pending",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "url": "http://example.invalid/",
        },
        "url",
        "additional_property",
    ),
    (
        "autowork_instruction_ack",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "instruction_ids": [],
        },
        "instruction_ids",
        "min_items",
    ),
    (
        "autowork_instruction_ack",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "instruction_ids": [f"{index:08x}-1234-4abc-8def-1234567890ab" for index in range(33)],
        },
        "instruction_ids",
        "max_items",
    ),
    (
        "autowork_instruction_ack",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "instruction_ids": ["not-a-uuid"],
        },
        "instruction_ids[0]",
        "pattern",
    ),
    (
        "autowork_instruction_ack",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "instruction_ids": [
                "12345678-1234-4abc-8def-1234567890ab",
                "12345678-1234-4abc-8def-1234567890ab",
            ],
        },
        "instruction_ids",
        "unique_items",
    ),
    (
        "autowork_instruction_ack",
        {
            "investigation_id": support.INVESTIGATION_ID,
            "instruction_ids": ["12345678-1234-4abc-8def-1234567890ab"],
            "tool_name": "advisor.consult",
        },
        "tool_name",
        "additional_property",
    ),
)


@pytest.mark.parametrize(("tool", "arguments", "field", "reason"), SCHEMA_CASES)
def test_schema_validation_rejects_before_network(
    tool: str, arguments: dict[str, object], field: str, reason: str
) -> None:
    transport = RecordingTransport()
    server = _server(transport)
    response = server.handle_message(_tools_call(tool, arguments))
    assert response is not None
    error = cast(dict[str, object], response["error"])
    assert error["code"] == -32602
    assert error["data"] == {
        "code": "invalid_arguments",
        "field": field,
        "reason": reason,
    }
    assert transport.calls == []


def test_oversized_frame_is_rejected_without_dispatch() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    line = b"{" + b"x" * (catalog.MAX_FRAME_BYTES + 1) + b"}"
    response = server.handle_line(line)
    assert response is not None
    error = cast(dict[str, object], response["error"])
    assert error["code"] == -32700
    assert error["data"] == {"code": "frame_too_large"}
    assert transport.calls == []


def test_non_loopback_backend_is_refused() -> None:
    for raw in (
        "https://127.0.0.1:8000",
        "http://example.invalid:8000",
        "http://10.0.0.1:8000",
        "http://127.0.0.1:8000/api",
        "http://user@127.0.0.1:8000",
        "http://127.0.0.1:8000?x=1",
        "http://[::2]:8000",
    ):
        with pytest.raises(shim.ShimConfigError):
            shim.parse_backend_url(raw)


def test_tools_list_is_the_closed_catalogue() -> None:
    transport = RecordingTransport()
    server = _server(transport)
    response = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert response is not None
    result = cast(dict[str, object], response["result"])
    tools = cast(list[dict[str, Any]], result["tools"])
    names = [tool["name"] for tool in tools]
    assert names == [spec.mcp_name for spec in catalog.CATALOGUE]
    assert len(names) == len(set(names))
    assert "autowork_bridge_consult" in names
    assert "autowork_instruction_pending" in names
    assert "autowork_instruction_ack" in names
    for tool in tools:
        assert tool["inputSchema"]["type"] == "object"
        assert tool["inputSchema"]["additionalProperties"] is False
    assert transport.calls == []


def test_catalogue_has_no_url_shell_path_or_proxy_knob() -> None:
    forbidden = {
        "url",
        "uri",
        "method",
        "path",
        "headers",
        "header",
        "proxy",
        "shell",
        "command",
        "endpoint",
        "host",
        "argv",
    }
    broker_names = [spec.broker_name for spec in catalog.CATALOGUE]
    assert len(broker_names) == len(set(broker_names))
    for spec in catalog.CATALOGUE:
        properties = cast(dict[str, object], spec.input_schema["properties"])
        assert not set(properties) & forbidden, spec.mcp_name
        assert spec.method == "POST"
        assert spec.path_template.startswith("/api/investigations/")
        assert "://" not in spec.path_template
        assert spec.timeout_seconds > 0


def test_unknown_method_and_batch_are_rejected() -> None:
    server = _server(RecordingTransport())
    unknown = server.handle_message({"jsonrpc": "2.0", "id": 1, "method": "resources/list"})
    assert unknown is not None
    assert cast(dict[str, object], unknown["error"])["code"] == -32601
    batch = server.handle_message([])
    assert batch is not None
    assert cast(dict[str, object], batch["error"])["code"] == -32600
