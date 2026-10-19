from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace
from uuid import UUID, uuid4

from cti_app.api.main import _operator_client_route_allowed
from cti_app.application.identity import Identity, IdentityScope
from cti_app.domain.investigation import ToolInvocationStatus
from tests.test_investigation_api import (
    INVESTIGATION_ID,
    _client,
    _CompositionFactory,
    _Rows,
    _SnapshotPort,
    _Uow,
)


class _OperatorIdentityProvider:
    async def current(self) -> Identity:
        return Identity(
            actor_id="operator:claude-client",
            scopes=frozenset({IdentityScope.OPERATOR_CLIENT}),
        )


def test_operator_client_can_read_analysis_and_create_supported_draft() -> None:
    store = _client()[1]
    observation_id = uuid4()
    blob_id = uuid4()
    association_id = uuid4()
    sample_sha256 = "a" * 64
    canonical_payload = json.dumps(
        {
            "schema": "sample-analysis.v1",
            "partial_reason": "floss:partial (output byte limit reached: 1048576/1048576 bytes)",
            "strings": [{"value": "CreateProcessW", "blob_id": str(blob_id), "offset": 64}],
        },
        separators=(",", ":"),
    ).encode()
    observation = SimpleNamespace(
        id=observation_id,
        investigation_id=INVESTIGATION_ID,
        blob_id=blob_id,
        sha256=hashlib.sha256(canonical_payload).hexdigest(),
    )

    class _ArtifactStore:
        async def read_bytes(self, requested_blob_id: UUID, *, max_bytes: int) -> bytes:
            assert requested_blob_id == blob_id
            assert max_bytes == 8 * 1024 * 1024
            return canonical_payload

    def uow_factory():
        uow = _Uow(store)
        uow.blobs = _Rows(
            {
                blob_id: SimpleNamespace(
                    descriptor=SimpleNamespace(
                        mime_type="application/json",
                        logical_bucket="investigation-evidence",
                    )
                )
            }
        )
        return uow

    class _InvocationService:
        def __init__(self) -> None:
            self.calls: list[dict[str, object]] = []
            self.version = 1
            self.finding_id = uuid4()

        async def invoke(self, **kwargs):
            self.calls.append(kwargs)
            self.version += 1
            tool_name = kwargs["tool_name"]
            refs: tuple[UUID, ...] = ()
            if tool_name == "vt.download_sample":
                data = {
                    "association_id": str(association_id),
                    "sample_sha256": sample_sha256,
                    "qualification": "candidate",
                }
            elif tool_name == "sample.analyze":
                if not any(item.id == observation_id for item in store.observations):
                    store.observations.append(observation)
                refs = (observation_id,)
                data = {
                    "engines_executed": ["capa", "floss", "smda", "static"],
                    "engine_statuses": {"floss": "partial"},
                    "partial_reason": (
                        "floss:partial (output byte limit reached: 1048576/1048576 bytes)"
                    ),
                    "observation_ids": [str(observation_id)],
                }
            elif tool_name == "local.run_command":
                data = {
                    "command": kwargs["arguments"]["command_id"],
                    "range": kwargs["arguments"]["options"],
                    "output": "CreateProcessW",
                }
            elif tool_name == "ghidra.list_functions":
                data = {
                    "functions": [{"address": "0x401000", "name": "entry"}],
                    "limit": kwargs["arguments"]["limit"],
                }
            elif tool_name == "finding.propose":
                data = {
                    "finding_id": str(self.finding_id),
                    "assessment": "supported",
                    "observation_refs": kwargs["arguments"]["observation_refs"],
                }
            elif tool_name == "opinion.compose":
                data = {
                    "status": "draft",
                    "finding_ids": kwargs["arguments"]["selection"]["finding_ids"],
                }
            else:
                raise AssertionError(f"unexpected operator tool: {tool_name}")
            return SimpleNamespace(
                investigation_id=INVESTIGATION_ID,
                working_state_version=self.version,
                error_code=None,
                job_id=None,
                idempotent_replay=False,
                output={
                    "kind": tool_name,
                    "status": "succeeded",
                    "untrusted_data": True,
                    "data": data,
                },
                invocation=SimpleNamespace(
                    id=uuid4(),
                    operation_key=f"operator:{tool_name}:{self.version}",
                    tool_name=tool_name,
                    schema_version=kwargs["schema_version"],
                    status=ToolInvocationStatus.SUCCEEDED,
                    version=self.version,
                    response_refs=refs,
                ),
            )

    class _OperatorFactory(_CompositionFactory):
        def __init__(self) -> None:
            super().__init__(_SnapshotPort())
            self.invocations = _InvocationService()

        def build(self, _uow, _investigation_id, *, operator_mode=False):
            assert operator_mode is True
            return SimpleNamespace(operator_tool_invocations=self.invocations)

    factory = _OperatorFactory()
    client, _ = _client(store=store, factory=factory)
    client.app.state.uow_factory = uow_factory
    client.app.state.production_artifact_store = _ArtifactStore()
    client.app.state.identity_provider = _OperatorIdentityProvider()

    def invoke(tool_name: str, schema_version: str, arguments: dict[str, object], version: int):
        return client.post(
            f"/api/investigations/{INVESTIGATION_ID}/operator/tools/{tool_name}",
            json={
                "schema_version": schema_version,
                "arguments": arguments,
                "purpose": f"Operator workflow: {tool_name}",
                "expected_version": version,
            },
            headers={"Idempotency-Key": f"workflow-{tool_name}"},
        )

    attached = invoke(
        "vt.download_sample",
        "1",
        {
            "sha256": sample_sha256,
            "purpose": "Analyze the attached sample.",
            "evidence_refs": [],
        },
        1,
    )
    assert attached.status_code == 200, attached.text
    attached_data = attached.json()["output"]["data"]
    assert attached_data["association_id"] == str(association_id)

    analyzed = invoke(
        "sample.analyze",
        "1",
        {
            "association_id": attached_data["association_id"],
            "purpose": "Read bounded static analysis results.",
            "evidence_refs": [],
        },
        2,
    )
    assert analyzed.status_code == 200, analyzed.text
    analysis_output = analyzed.json()["output"]
    assert analysis_output["data"]["engine_statuses"]["floss"] == "partial"
    assert "output byte limit reached" in analysis_output["data"]["partial_reason"]

    observation_response = client.get(
        f"/api/investigations/{INVESTIGATION_ID}/operator/observations/{observation_id}"
    )
    assert observation_response.status_code == 200, observation_response.text
    assert "CreateProcessW" in observation_response.json()["payload"]
    assert observation_response.json()["untrusted_data"] is True

    strings = invoke(
        "local.run_command",
        "1",
        {
            "command_id": "strings",
            "sample_sha256": sample_sha256,
            "options": {"offset": 64, "length": 128},
        },
        3,
    )
    functions = invoke(
        "ghidra.list_functions",
        "1",
        {"sample_sha256": sample_sha256, "limit": 20},
        4,
    )
    assert strings.status_code == functions.status_code == 200
    assert factory.invocations.calls[2]["arguments"]["options"] == {
        "offset": 64,
        "length": 128,
    }
    assert functions.json()["output"]["data"]["functions"][0]["address"] == "0x401000"

    finding = invoke(
        "finding.propose",
        "operator-finding.v1",
        {
            "action": "propose",
            "kind": "network_behaviour",
            "assessment": "supported",
            "statement": "The sample creates a process.",
            "subject_ref": None,
            "observation_refs": [str(observation_id)],
            "claim_refs": [],
            "novelty": None,
            "scope": "Sample behavior",
            "limitations": ["One sample observed."],
        },
        5,
    )
    assert finding.status_code == 200, finding.text
    assert finding.json()["output"]["data"]["assessment"] == "supported"
    assert finding.json()["output"]["data"]["observation_refs"] == [str(observation_id)]

    composed = invoke(
        "opinion.compose",
        "operator-opinion.v1",
        {
            "action": "compose",
            "selection": {
                "finding_ids": [str(factory.invocations.finding_id)],
                "claim_ids": [],
                "block_types": [],
            },
        },
        6,
    )
    assert composed.status_code == 200, composed.text
    assert composed.json()["output"]["data"]["status"] == "draft"

    assert not _operator_client_route_allowed(
        "POST", f"/api/investigations/{INVESTIGATION_ID}/envelope/approve"
    )
    assert not _operator_client_route_allowed(
        "POST", f"/api/investigations/{INVESTIGATION_ID}/opinions/approve"
    )
