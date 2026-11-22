"""Cross-contract oracle: the host shim vectors and the backend verifier agree.

The golden vectors are produced by an independent stdlib reference in the shim
tests (lot H2). The backend message builder (lot 05B1) must reproduce each
expected signature byte for byte, otherwise a signed host call could never be
verified (or a forged one accepted).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
from pathlib import Path
from typing import Any
from uuid import UUID

import pytest

from cti_app.application.agent_session_verification import build_agent_session_message

_FIXTURE = Path(__file__).parent / "workspace_agent" / "fixtures" / "session_signing_v1.json"


def _vectors() -> list[dict[str, Any]]:
    document = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    vectors = document["vectors"]
    assert isinstance(vectors, list) and vectors
    return [dict(item) for item in vectors]


@pytest.mark.parametrize("vector", _vectors(), ids=lambda item: str(item["name"]))
def test_backend_message_signs_to_the_shim_golden_vector(vector: dict[str, Any]) -> None:
    message = build_agent_session_message(
        method=str(vector["method"]),
        path_and_query=str(vector["path_and_query"]),
        session_id=UUID(str(vector["session_id"])),
        body=str(vector["body"]).encode("utf-8"),
        idempotency_key=str(vector["idempotency_key"]),
        timestamp=str(vector["timestamp"]),
    )
    secret = base64.b64decode(str(vector["secret_b64"]))
    signature = hmac.new(secret, message, hashlib.sha256).hexdigest()
    expected = str(vector["expected_signature"]).removeprefix("v1=")
    assert signature == expected
