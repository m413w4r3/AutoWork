from __future__ import annotations

import hashlib
import json
from contextlib import AbstractAsyncContextManager
from dataclasses import replace
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.apk_analysis import (
    APK_ANALYSIS_RESULT_SCHEMA,
    JADX_VERSION,
    JADX_ZIP_SHA256,
    ApkAnalysisOperation,
)
from cti_app.application.confined_analysis import (
    ConfinedAnalysisFamilyStatus,
    ConfinedAnalysisWorkerAttestation,
    deterministic_confined_analysis_key,
)
from cti_app.application.investigation_apk_tools import (
    APK_TOOL_NAMES,
    _tool_schemas,
    _worker_parameters,
    apk_tool_capabilities,
    register_apk_tools,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolExecutionContext,
    validate_json_schema,
)
from cti_app.config import Settings
from cti_app.domain.entities import SampleState
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    InvestigationBudget,
    ToolInvocationStatus,
)

SAMPLE_SHA256 = "a" * 64
_ITEMS = [
    {
        "package_name": "org.example",
        "class_name": "org.example.First",
        "source_path": "sources/org/example/First.java",
    },
    {
        "package_name": "org.example",
        "class_name": "org.example.Second",
        "source_path": "sources/org/example/Second.java",
    },
]


class _UowContext(AbstractAsyncContextManager):
    def __init__(self, uow: SimpleNamespace) -> None:
        self.uow = uow

    async def __aenter__(self) -> SimpleNamespace:
        return self.uow

    async def __aexit__(self, *_args: object) -> None:
        return None


class _Execution:
    available = True

    def __init__(self, *, family_available: bool = True) -> None:
        self.requests = []
        self.result = _worker_result()
        self._attestation = ConfinedAnalysisWorkerAttestation(
            reachable=True,
            confined=True,
            isolation_profile="analysis-worker-test-v1",
            versions={
                "jadx": JADX_VERSION,
                "jadx_zip_sha256": JADX_ZIP_SHA256,
                "jadx_jre_version": "17.0.14",
            },
            families={
                "apk": ConfinedAnalysisFamilyStatus(
                    "apk",
                    family_available,
                    JADX_VERSION,
                    None if family_available else "jadx_jre_version_unattested_17",
                )
            },
            dependencies_ready=True,
        )

    @property
    def family_statuses(self):
        return self._attestation.families

    @property
    def attestation(self):
        return self._attestation

    async def execute(self, request):
        self.requests.append(request)
        return SimpleNamespace(
            status=ToolInvocationStatus.SUCCEEDED,
            available_reason=None,
            payload={"result": self.result},
        )


class _Recorder:
    def __init__(self) -> None:
        self.calls = []
        self._by_key = {}

    async def record_json(self, **kwargs):
        self.calls.append(kwargs)
        encoded = json.dumps(
            kwargs["payload"],
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        record_key = kwargs["idempotency_key"]
        record_id = self._by_key.setdefault(record_key, uuid4())
        return SimpleNamespace(id=record_id, sha256=hashlib.sha256(encoded).hexdigest())


def _worker_result(
    *,
    operation: str = "apk.classes.list",
    status: str = "SUCCEEDED",
    data: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": APK_ANALYSIS_RESULT_SCHEMA,
        "operation": operation,
        "status": status,
        "sample_sha256": SAMPLE_SHA256,
        "engine": "jadx",
        "jadx_version": JADX_VERSION,
        "jadx_zip_sha256": JADX_ZIP_SHA256,
        "jre_version": "17.0.14",
        "data": data
        or {
            "offset": 0,
            "limit": 2,
            "total": 3,
            "items": _ITEMS,
            "has_more": True,
            "truncated": False,
        },
    }


def _sample_fixture(
    *,
    association_sha256: str = SAMPLE_SHA256,
    blob_sha256: str = SAMPLE_SHA256,
    state: SampleState = SampleState.QUARANTINED,
) -> tuple[SimpleNamespace, object, object, object, object]:
    investigation_id = uuid4()
    association_id = uuid4()
    sample_id = uuid4()
    blob_id = uuid4()
    subject_id = uuid4()
    association = SimpleNamespace(id=association_id, sample_sha256=association_sha256)
    associations = SimpleNamespace(
        list_for_investigation=lambda requested_id: _list_associations(
            requested_id, investigation_id, association
        )
    )
    blob = SimpleNamespace(
        id=blob_id,
        descriptor=SimpleNamespace(sha256=blob_sha256, size=4096),
    )
    sample = SimpleNamespace(id=sample_id, blob_id=blob_id, state=state)
    uow = SimpleNamespace(
        analyst_investigations=SimpleNamespace(
            get=lambda requested_id: _get_investigation(requested_id, investigation_id, subject_id)
        ),
        blobs=SimpleNamespace(get_by_address=lambda _bucket, _sha: _return(blob)),
        samples=SimpleNamespace(get_by_subject_and_blob=lambda _subject, _blob: _return(sample)),
    )
    return (
        SimpleNamespace(
            investigation_id=investigation_id,
            association_id=association_id,
            sample_id=sample_id,
            blob_id=blob_id,
            subject_id=subject_id,
        ),
        associations,
        blob,
        sample,
        uow,
    )


async def _list_associations(requested_id, expected_id, association):
    return (association,) if requested_id == expected_id else ()


async def _get_investigation(requested_id, expected_id, subject_id):
    return SimpleNamespace(subject_id=subject_id) if requested_id == expected_id else None


async def _return(value):
    return value


def _register(
    execution: _Execution | None = None,
    associations: Any = None,
    recorder: _Recorder | None = None,
    uow: SimpleNamespace | None = None,
) -> InMemoryToolRegistry:
    registry = InMemoryToolRegistry()
    register_apk_tools(
        registry,
        execution=execution,
        sample_associations=associations,
        recorder=recorder,
        uow_factory=lambda: _UowContext(uow or SimpleNamespace()),
        settings=Settings(_env_file=None),
    )
    return registry


def _context(investigation_id, capabilities=frozenset({"investigation.apk"})):
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(),
        idempotency_key="apk-tool-test-call",
        available_capabilities=capabilities,
    )


def test_apk_schemas_are_strict_bounded_and_semantically_validated() -> None:
    schemas = _tool_schemas()
    assert set(schemas) == set(APK_TOOL_NAMES)
    assert all(schema["$schema"].endswith("2020-12/schema") for schema in schemas.values())
    assert all(schema["additionalProperties"] is False for schema in schemas.values())

    valid_manifest = {"sample_sha256": SAMPLE_SHA256}
    validate_json_schema(valid_manifest, schemas["apk.manifest"])
    valid_examples = {
        "apk.manifest": valid_manifest,
        "apk.classes.list": {"sample_sha256": SAMPLE_SHA256, "offset": 20, "limit": 10},
        "apk.strings.extract": {"sample_sha256": SAMPLE_SHA256, "offset": 20, "limit": 10},
        "apk.source.read": {
            "sample_sha256": SAMPLE_SHA256,
            "source_path": "sources/org/example/First.java",
            "start_line": 3,
            "line_count": 20,
        },
        "apk.source.search": {
            "sample_sha256": SAMPLE_SHA256,
            "pattern": "https?://",
            "regex": True,
            "context_lines": 1,
            "offset": 20,
            "limit": 10,
        },
    }
    for name, arguments in valid_examples.items():
        validate_json_schema(arguments, schemas[name])
        assert _worker_parameters(ApkAnalysisOperation(name), arguments)["operation"] == name
    with pytest.raises(ValueError):
        validate_json_schema({**valid_manifest, "unexpected": True}, schemas["apk.manifest"])
    with pytest.raises(ValueError):
        validate_json_schema({"sample_sha256": "A" * 64}, schemas["apk.manifest"])

    valid_source = {"sample_sha256": SAMPLE_SHA256, "class_name": "org.example.First"}
    validate_json_schema(valid_source, schemas["apk.source.read"])
    for invalid_source in (
        {"sample_sha256": SAMPLE_SHA256},
        {
            "sample_sha256": SAMPLE_SHA256,
            "class_name": "org.example.First",
            "source_path": "sources/org/example/First.java",
        },
        {"sample_sha256": SAMPLE_SHA256, "source_path": "sources/../escape.java"},
    ):
        with pytest.raises(ValueError):
            validate_json_schema(invalid_source, schemas["apk.source.read"])

    with pytest.raises(ValueError):
        validate_json_schema(
            {"sample_sha256": SAMPLE_SHA256, "offset": 0, "limit": 201},
            schemas["apk.classes.list"],
        )
    with pytest.raises(ValueError):
        _worker_parameters(
            ApkAnalysisOperation.SOURCE_SEARCH,
            {"pattern": "(", "regex": True},
        )


@pytest.mark.asyncio
async def test_apk_tools_require_an_attested_jadx_family_and_associated_quarantined_sample() -> (
    None
):
    fixture, associations, _blob, _sample, uow = _sample_fixture()
    unavailable_execution = _Execution(family_available=False)
    unavailable_recorder = _Recorder()
    assert (
        apk_tool_capabilities(
            execution=unavailable_execution,
            sample_associations=associations,
            recorder=unavailable_recorder,
        )
        == frozenset()
    )
    unavailable_registry = _register(unavailable_execution, associations, unavailable_recorder, uow)
    unavailable = unavailable_registry.get("apk.manifest", "1")
    assert unavailable is not None
    _definition, unavailable_adapter = unavailable
    unavailable_result = await unavailable_adapter.execute(
        {"sample_sha256": SAMPLE_SHA256}, _context(fixture.investigation_id)
    )
    assert unavailable_result.status is ToolInvocationStatus.UNAVAILABLE
    assert unavailable_result.message == "jadx_jre_version_unattested_17"
    assert unavailable_execution.requests == []

    execution = _Execution()
    recorder = _Recorder()
    no_association_fixture, no_associations, _, _, no_association_uow = _sample_fixture(
        association_sha256="b" * 64
    )
    registry = _register(execution, no_associations, recorder, no_association_uow)
    found = registry.get("apk.manifest", "1")
    assert found is not None
    _, adapter = found
    no_association = await adapter.execute(
        {"sample_sha256": SAMPLE_SHA256}, _context(no_association_fixture.investigation_id)
    )
    assert no_association.status is ToolInvocationStatus.POLICY_DENIED
    assert no_association.message == "sample_not_associated"

    mismatch_fixture, mismatch_associations, _, _, mismatch_uow = _sample_fixture(
        blob_sha256="b" * 64
    )
    mismatch_registry = _register(execution, mismatch_associations, recorder, mismatch_uow)
    mismatch_found = mismatch_registry.get("apk.manifest", "1")
    assert mismatch_found is not None
    _, mismatch_adapter = mismatch_found
    mismatch = await mismatch_adapter.execute(
        {"sample_sha256": SAMPLE_SHA256}, _context(mismatch_fixture.investigation_id)
    )
    assert mismatch.status is ToolInvocationStatus.POLICY_DENIED
    assert mismatch.message == "sample_not_quarantined"
    assert execution.requests == []


@pytest.mark.asyncio
async def test_apk_tool_paginates_persists_canonical_observation_and_reuses_identity() -> None:
    fixture, associations, _blob, _sample, uow = _sample_fixture(state=SampleState.REVIEW_CANDIDATE)
    execution = _Execution()
    page_items = list(reversed(_ITEMS))
    execution.result = _worker_result(
        data={
            "offset": 2,
            "limit": 2,
            "total": 6,
            "items": page_items,
            "has_more": True,
            "truncated": False,
        }
    )
    recorder = _Recorder()
    registry = _register(execution, associations, recorder, uow)
    assert set(item.name for item in registry.list_registered()) == set(APK_TOOL_NAMES)
    assert apk_tool_capabilities(
        execution=execution,
        sample_associations=associations,
        recorder=recorder,
    ) == frozenset({"investigation.apk"})

    context = _context(fixture.investigation_id)
    found = registry.get("apk.classes.list", "1")
    assert found is not None
    definition, adapter = found
    assert definition.capability_key == "investigation.apk"
    assert definition.cost_model[BudgetDimension.TOOL_SECONDS] <= 60
    arguments = {"sample_sha256": SAMPLE_SHA256, "offset": 2, "limit": 2}
    first = await adapter.execute(arguments, context)
    second = await adapter.execute(arguments, context)

    assert first.status is ToolInvocationStatus.SUCCEEDED
    assert first.output["result"]["data"]["offset"] == 2
    assert [dict(item) for item in first.output["result"]["data"]["items"]] == page_items
    assert first.output["result"]["data"]["has_more"] is True
    assert str(first.observation_refs[0]) == first.output["evidence_observation_id"]
    assert second.status is ToolInvocationStatus.SUCCEEDED
    assert execution.requests[0].artifact_id == fixture.blob_id
    assert execution.requests[0].association_id == fixture.association_id
    assert execution.requests[0].sample_id == fixture.sample_id
    assert execution.requests[0].analysis_parameters == {
        "operation": "apk.classes.list",
        "offset": "2",
        "limit": "2",
    }
    assert execution.requests[0].timeout_seconds == 60
    assert execution.requests[0].max_output_bytes == 192 * 1024
    assert execution.requests[0].sha256 == SAMPLE_SHA256

    evidence = recorder.calls[0]
    assert evidence["kind"] is EvidenceKind.BINARY_ANALYSIS
    assert evidence["source"] == "apk.jadx"
    assert evidence["source_version"].startswith(
        f"apk-analysis-observation.v1:jadx:{JADX_VERSION}:archive:{JADX_ZIP_SHA256}:jre:"
    )
    payload = evidence["payload"]
    assert payload["schema_version"] == "apk-analysis-observation.v1"
    assert payload["sample_sha256"] == SAMPLE_SHA256
    assert payload["parameters"] == execution.requests[0].analysis_parameters
    canonical_result = json.dumps(
        execution.result,
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    assert payload["result_json_sha256"] == hashlib.sha256(canonical_result).hexdigest()
    assert "jadx" in payload["source"]["engine"]
    assert payload["source"]["version"] == JADX_VERSION
    assert payload["source"]["archive_sha256"] == JADX_ZIP_SHA256
    assert payload["source"]["jre_version"] == "17.0.14"
    assert payload["source"]["runtime_jre_version"] == "17.0.14"
    assert any("obfusqué" in item for item in payload["limitations"])
    assert (
        first.output["evidence_observation_sha256"]
        == hashlib.sha256(
            json.dumps(
                payload,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
        ).hexdigest()
    )
    assert evidence["idempotency_key"] == recorder.calls[1]["idempotency_key"]
    assert first.output["evidence_observation_id"] == second.output["evidence_observation_id"]
    assert (
        adapter.operation_cache_identity["worker_attestation"]["versions"]["jadx"] == JADX_VERSION
    )
    worker_versions = execution.attestation.versions
    first_operation_key = deterministic_confined_analysis_key(
        execution.requests[0], worker_versions=worker_versions
    )
    changed_parameters = replace(
        execution.requests[0],
        analysis_parameters={**execution.requests[0].analysis_parameters, "offset": "3"},
    )
    assert (
        deterministic_confined_analysis_key(changed_parameters, worker_versions=worker_versions)
        != first_operation_key
    )
    changed_versions = {**worker_versions, "jadx_jre_version": "17.0.15"}
    assert (
        deterministic_confined_analysis_key(execution.requests[0], worker_versions=changed_versions)
        != first_operation_key
    )

    changed_attestation = replace(
        execution.attestation,
        versions={**execution.attestation.versions, "jadx_jre_version": "17.0.15"},
    )
    before = adapter.operation_cache_identity
    execution._attestation = changed_attestation
    assert adapter.operation_cache_identity != before


@pytest.mark.asyncio
async def test_apk_response_marks_worker_and_operator_truncation() -> None:
    fixture, associations, _blob, _sample, uow = _sample_fixture()
    execution = _Execution()
    execution.result = _worker_result(
        operation="apk.source.read",
        data={
            "class_name": "org.example.First",
            "source_path": "sources/org/example/First.java",
            "start_line": 1,
            "line_count": 20,
            "total_lines": 500,
            "content": "A" * 12_000,
            "has_more": True,
            "truncated": False,
        },
    )
    recorder = _Recorder()
    registry = _register(execution, associations, recorder, uow)
    found = registry.get("apk.source.read", "1")
    assert found is not None
    _definition, adapter = found
    result = await adapter.execute(
        {
            "sample_sha256": SAMPLE_SHA256,
            "class_name": "org.example.First",
            "start_line": 1,
            "line_count": 20,
        },
        _context(fixture.investigation_id),
    )
    assert result.status is ToolInvocationStatus.SUCCEEDED
    assert result.output["result_truncated_by_response"] is True
    assert result.output["result"]["operator_output_truncated"] is True
    assert result.output["result"]["data"]["content"].endswith("...[autowork truncated]")
    assert (
        len(
            json.dumps(
                result.output["result"],
                ensure_ascii=False,
                separators=(",", ":"),
                default=dict,
            ).encode()
        )
        <= 8 * 1024
    )
    assert recorder.calls[0]["payload"]["result_truncated_by_operator_response"] is True
