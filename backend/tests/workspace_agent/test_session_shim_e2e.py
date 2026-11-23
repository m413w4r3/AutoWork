"""M24-05 H2 end-to-end oracle: H1 handoff then MCP shim then fake backend.

The H1 supervisor prepares a fake CLI session and hands it the D7 secret over
``ipc/shim.sock``. The shim is then launched in the same closed environment
(``AUTOWORK_SESSION_ID/SOCKET/BACKEND_URL``, no secret, no token) against a
real loopback backend that records the exact signed request. This proves the
caller side of the 05B2 contract without touching a live provider.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import pytest
from sidecars.workspace_agent.sessions import catalog

from tests.workspace_agent import session_shim_support as support
from tests.workspace_agent.sessions_support import (
    PREPARE_KEY,
    SESSION_ID,
    START_KEY,
    fake_script,
    post_session,
    prepare_payload,
    running_session_agent,
    wait_for_observation,
    wait_for_state,
    write_fake_script,
)


@pytest.fixture
def session_base() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="m24h2e2e-", dir="/tmp"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def test_h1_secret_handoff_then_shim_signs_a_fake_backend_request(session_base: Path) -> None:
    secret_script = fake_script(
        [
            {"op": "emit", "event": {"type": "started"}},
            {"op": "receive_secret", "attempts": 1},
            {"op": "write", "path": "result.md", "text": "synthetic result\n"},
            {"op": "exit", "code": 0},
        ]
    )
    with running_session_agent(session_base) as running, running.client() as client:
        workspace_id = running.ensure_workspace(client)
        prepared = post_session(
            client,
            running,
            "/v1/sessions",
            prepare_payload(workspace_id, auth_secret=support.SECRET),
            PREPARE_KEY,
        )
        assert prepared.status_code == 200, prepared.text
        write_fake_script(running, secret_script)
        started = post_session(client, running, f"/v1/sessions/{SESSION_ID}/start", {}, START_KEY)
        assert started.status_code == 200, started.text
        # A clean run end is the D2 instruction receive point (ID-4).
        wait_for_state(client, running, "awaiting_instruction", "stopped", "failed")
        received = wait_for_observation(running, "secret_received")
        assert isinstance(received, dict)
        assert received["sha256"] == hashlib.sha256(support.SECRET).hexdigest()

        with support.FakeBackend() as backend:
            backend.enqueue(
                support.BackendReply(
                    status=200,
                    content_type="application/json",
                    body=b'{"consultation_id":"synthetic-1","advice":"data only"}',
                )
            )
            with support.ShimHarness(
                backend_url=backend.url, secret=support.SECRET
            ) as shim_process:
                initialized = shim_process.initialize()
                protocol = cast(dict[str, object], initialized["result"])["protocolVersion"]
                assert protocol in catalog.SUPPORTED_PROTOCOL_VERSIONS
                listed = shim_process.request("tools/list")
                tools = cast(dict[str, object], listed["result"])["tools"]
                assert isinstance(tools, list) and tools

                response = shim_process.call_tool(
                    "autowork_bridge_consult",
                    {
                        "investigation_id": support.INVESTIGATION_ID,
                        "consultation_type": "interpretation",
                        "question": "Synthetic: is this response data or instructions?",
                    },
                )
                result = cast(dict[str, object], response["result"])
                content = cast(list[dict[str, object]], result["content"])
                envelope = json.loads(cast(str, content[0]["text"]))
                assert envelope["data"] == {
                    "consultation_id": "synthetic-1",
                    "advice": "data only",
                }
                assert envelope["data_not_instructions"] is True

            requests = backend.requests
            assert len(requests) == 1
            request = requests[0]
            assert request.method == "POST"
            assert request.path == (
                f"/api/investigations/{support.INVESTIGATION_ID}"
                f"/agent-sessions/{support.SESSION_ID}/tool-calls"
            )
            body = request.json_body()
            assert body["tool_name"] == "advisor.consult"
            assert body["arguments"] == {
                "consultation_type": "interpretation",
                "question": "Synthetic: is this response data or instructions?",
            }
            assert request.header("x-autowork-agent-session") == support.SESSION_ID
            assert request.header("x-autowork-agent-signature") == support.reference_signature(
                support.SECRET,
                method=request.method,
                path=request.path,
                session_id=support.SESSION_ID,
                body=request.body,
                idempotency_key=request.header("idempotency-key"),
                timestamp=int(request.header("x-autowork-agent-timestamp")),
            )
            assert request.header("idempotency-key") == support.expected_client_key(
                support.SESSION_ID,
                "advisor.consult",
                {
                    "investigation_id": support.INVESTIGATION_ID,
                    "consultation_type": "interpretation",
                    "question": "Synthetic: is this response data or instructions?",
                },
            )
            assert "authorization" not in request.headers
