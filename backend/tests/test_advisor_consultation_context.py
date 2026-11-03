from __future__ import annotations

import hashlib
import json
import time
from collections import Counter
from dataclasses import replace
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import pytest

from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.domain.classification import TLP
from cti_app.domain.investigation import (
    BudgetDimension,
    EvidenceKind,
    EvidenceObservation,
    InvestigationBudget,
)
from tests.test_investigation_advisor_tools import (
    ArtifactStore,
    BlobRepository,
    FindingRepository,
    ObservationRepository,
    Transport,
    async_get,
    make_context,
    make_tool,
)

INVESTIGATION_ID = uuid5(NAMESPACE_URL, "autowork:advisor-context:m12-q")
SUBJECT_ID = uuid5(NAMESPACE_URL, "autowork:advisor-context:subject")
SYNTHESIS_ID = uuid5(NAMESPACE_URL, "autowork:advisor-context:synthesis")
FIXED_TIME = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
SAMPLE_COUNT = 40
OBSERVATIONS_PER_SAMPLE = 6
SAMPLE_BYTES = 64 * 1024
CONSULTATION_TYPES = ("yara_draft", "pivot_strategy", "suricata_draft", "draft_text")
RESERVATION_PROPERTY_TYPES = (
    "yara_draft",
    "pivot_strategy",
    "suricata_draft",
    "draft_text",
    "plan_investigation",
)
# The first build is the dossier actually used for the prompt. Later base-code
# builds came from redundant alias generation and are not the consultation dossier.
BASELINE_EXECUTION = {
    "yara_draft": {
        "dossier_sha256": "a6c9481f559b4a7441ff3b6dfcd79888d03b2093c0972016bc66105e512bebe7",
        "prompt_sha256": "a94daf7bebbc7622452d0f18347fa113ba752638c13e8ee2b87b3da3c5b69f99",
        "manifest_refs_sha256": "d739192f7e232fe0243c2a16c09038a815716e0c20ba20f70d03d88aacbb2f7e",
    },
    "pivot_strategy": {
        "dossier_sha256": "127b9669e61da338a36d474b62baee0a3a4a1ce2181660021901aa445b9fc200",
        "prompt_sha256": "42aa494c4e193d327d985c86b8a369b5e0e8fa23e6ab66c841f73575141bde25",
        "manifest_refs_sha256": "a158b3a7ed25b732927b986c1fb6e3edb4721ebd3141a7d4bc5c2fabf4e1102f",
    },
    "suricata_draft": {
        "dossier_sha256": "350cba0406f137c007cbedb268fc4cb66511fb5b7760cad51ea7f9df52323d77",
        "prompt_sha256": "50db168dee0827f415307f8837afd5ec1641fece8af70c6f889cb1701bc3da3c",
        "manifest_refs_sha256": "a1dfeb134e565e654f6fa19256724aa45b60f108ba0a2340c905840abb629623",
    },
    "draft_text": {
        "dossier_sha256": "4595e65b3749ca89c21ddfd74809c9c4955f3353222d7727fb9edb7ed0d6e95e",
        "prompt_sha256": "40b8ba164daab1ae0addcf971407f690cb627620c2d0a15c390c8fc700ff74d4",
        "manifest_refs_sha256": "efe49f8f05fed58b4e7b97aa8f22098109b1e7b36767fff9c521bad6e632f8f8",
    },
}


class _CallCounter:
    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self.repo_calls = 0
        self.blob_reads = 0
        self.bytes_read = 0
        self.association_sql_queries = 0
        self.observation_blob_ids: set[UUID] = set()
        self.observation_blob_reads = 0
        self.max_observation_blob_reads = 0
        self._observation_reads: Counter[UUID] = Counter()

    def reset(self) -> None:
        self.calls.clear()
        self.repo_calls = 0
        self.blob_reads = 0
        self.bytes_read = 0
        self.association_sql_queries = 0
        self.observation_blob_reads = 0
        self.max_observation_blob_reads = 0
        self._observation_reads.clear()

    def snapshot(self) -> dict[str, object]:
        return {
            "calls": dict(sorted(self.calls.items())),
            "repo_calls": self.repo_calls,
            "blob_reads": self.blob_reads,
            "bytes_read": self.bytes_read,
            "association_sql_queries": self.association_sql_queries,
            "observation_blob_reads": self.observation_blob_reads,
            "max_observation_blob_reads": self.max_observation_blob_reads,
        }


class _CountingProxy:
    def __init__(self, target: object, label: str, counter: _CallCounter) -> None:
        self._target = target
        self._label = label
        self._counter = counter

    def __getattr__(self, name: str) -> object:
        value = getattr(self._target, name)
        if not callable(value):
            return value

        async def async_call(*args: object, **kwargs: object) -> object:
            self._record(name)
            result = await value(*args, **kwargs)
            self._record_result(name, result, args)
            return result

        if callable(value) and getattr(value, "__code__", None) is not None:
            if value.__code__.co_flags & 0x80:
                return async_call

        def sync_call(*args: object, **kwargs: object) -> object:
            self._record(name)
            result = value(*args, **kwargs)
            self._record_result(name, result, args)
            return result

        return sync_call

    def _record(self, name: str) -> None:
        key = f"{self._label}.{name}"
        self._counter.calls[key] += 1
        if self._label != "artifact_store":
            self._counter.repo_calls += 1

    def _record_result(self, name: str, result: object, args: tuple[object, ...]) -> None:
        if self._label == "sample_associations" and name == "list_for_investigation":
            # The batched SQL repository uses three SELECTs independent of S.
            self._counter.association_sql_queries += 3
        if self._label == "artifact_store" and name == "read_bytes":
            self._counter.blob_reads += 1
            self._counter.bytes_read += len(result)  # type: ignore[arg-type]
            blob_id = args[0] if args else None
            if blob_id in self._counter.observation_blob_ids:
                self._counter.observation_blob_reads += 1
                self._counter._observation_reads[blob_id] += 1
                self._counter.max_observation_blob_reads = max(
                    self._counter.max_observation_blob_reads,
                    self._counter._observation_reads[blob_id],
                )


def _uid(value: str) -> UUID:
    return uuid5(NAMESPACE_URL, f"autowork:advisor-context:{value}")


def _json_bytes(value: object) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _m12_q_fixture():
    transport = Transport(
        TurnSubmission(
            TurnSubmissionStatus.SUCCEEDED,
            output_text="The evidence supports a bounded offline characterization.",
            bridge_invocation_id="00000000-0000-0000-0000-000000000001",
        )
    )
    tool, _unused_sample_blob_id = make_tool(
        investigation_id=INVESTIGATION_ID,
        transport=transport,
    )
    snapshot = SimpleNamespace(
        subject_id=SUBJECT_ID,
        external_llm_allowed=True,
        do_not_submit=False,
        tlp="clear",
        synthesis_sha256="a" * 64,
        article=SimpleNamespace(
            short_summary="Fixed M12-Q article summary.",
            open_questions=("Which protocol marker is shared?",),
        ),
        article_passages=(
            SimpleNamespace(
                claim_id="S1P1",
                heading="Observed behavior",
                text="S1P1. A fixed synthetic article passage supports the fixture.",
            ),
        ),
    )
    investigation = SimpleNamespace(
        subject_id=SUBJECT_ID,
        synthesis_artifact_id=SYNTHESIS_ID,
    )
    tool._investigations = SimpleNamespace(get=async_get(investigation))
    tool._article_snapshots.resolve = async_get(snapshot)

    observations: list[EvidenceObservation] = []
    payloads: dict[UUID, bytes] = {}
    associations: list[SimpleNamespace] = []
    samples: list[SimpleNamespace] = []
    sample_blob_values: dict[UUID, bytes] = {}
    for sample_index in range(SAMPLE_COUNT):
        sample_bytes = bytes(
            (sample_index * 17 + byte_index * 13) % 251 for byte_index in range(SAMPLE_BYTES)
        )
        sample_sha256 = hashlib.sha256(sample_bytes).hexdigest()
        sample_blob_id = _uid(f"sample-blob:{sample_index}")
        sample_id = _uid(f"sample:{sample_index}")
        association_id = _uid(f"association:{sample_index}")
        sample_blob_values[sample_blob_id] = sample_bytes
        associations.append(
            SimpleNamespace(
                id=association_id,
                sample_sha256=sample_sha256,
                qualification="confirmed",
            )
        )
        samples.append(
            SimpleNamespace(
                id=sample_id,
                expected_hash=sample_sha256,
                source_object_id=None,
                state="quarantined",
                blob_id=sample_blob_id,
                origin_kind="source_seed",
                original_name=f"synthetic-{sample_index:02d}.bin",
                source_service="fixture",
                tlp=TLP.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
                license_restriction=None,
            )
        )
        for observation_index in range(OBSERVATIONS_PER_SAMPLE):
            if observation_index < 4:
                payload = {
                    "schema": "sample-obfuscated-strings.v1",
                    "sample_sha256": sample_sha256,
                    "nested_metadata": {"sample": {"sample_sha256": sample_sha256}},
                    "strings": [
                        {
                            "value": f"SyntheticMarker_{sample_index:02d}_{observation_index}",
                            "offset": 128 + observation_index * 64,
                        }
                    ],
                }
                source = "autowork.floss"
                kind = EvidenceKind.BINARY_STRING
                object_type = "sample"
                object_id = sample_sha256
            elif observation_index == 4:
                payload = {
                    "schema": "synthetic-network-observation.v1",
                    "sample": {"sample_sha256": sample_sha256},
                    "result": {
                        "tls": [
                            {
                                "server_name": f"updates-{sample_index:02d}.example.test",
                                "port": 443,
                            }
                        ]
                    },
                }
                source = "fixture.network.capture"
                kind = EvidenceKind.OTHER
                object_type = "network_capture"
                object_id = f"network-{sample_index:02d}"
            else:
                payload = {
                    "schema": "synthetic-nested-reference.v1",
                    "metadata": {"parent": {"sample_sha256": sample_sha256}},
                    "marker": f"nested-{sample_index:02d}",
                }
                source = "fixture.nested-reference"
                kind = EvidenceKind.OTHER
                object_type = "analysis_summary"
                object_id = f"summary-{sample_index:02d}"
            raw = _json_bytes(payload)
            blob_id = _uid(f"evidence-blob:{sample_index}:{observation_index}")
            observation = EvidenceObservation(
                investigation_id=INVESTIGATION_ID,
                id=_uid(f"observation:{sample_index}:{observation_index}"),
                kind=kind,
                blob_id=blob_id,
                sha256=hashlib.sha256(raw).hexdigest(),
                source=source,
                source_version="fixture.v1",
                observed_at=FIXED_TIME,
                object_type=object_type,
                object_id=object_id,
            )
            observations.append(observation)
            payloads[blob_id] = raw

    observations_repository = ObservationRepository(observations)
    findings_repository = FindingRepository()
    association_repository = SimpleNamespace(list_for_investigation=async_get(tuple(associations)))
    sample_repository = SimpleNamespace(list_for_subject=async_get(tuple(samples)))
    blob_repository = BlobRepository()
    artifact_store = ArtifactStore(payloads | sample_blob_values)
    for observation in observations:
        raw = payloads[observation.blob_id]
        blob_repository.add(
            observation.blob_id,
            sha256=observation.sha256,
            size=len(raw),
            logical_bucket="investigation-evidence",
        )
    counter = _CallCounter()
    counter.observation_blob_ids = {item.blob_id for item in observations}
    for sample, association in zip(samples, associations, strict=True):
        raw = sample_blob_values[sample.blob_id]
        blob_repository.add(
            sample.blob_id,
            sha256=association.sample_sha256,
            size=len(raw),
            logical_bucket="samples-quarantine",
        )

    tool._observations = _CountingProxy(observations_repository, "observations", counter)
    tool._findings = _CountingProxy(findings_repository, "findings", counter)
    tool._sample_associations = _CountingProxy(
        association_repository, "sample_associations", counter
    )
    tool._samples = _CountingProxy(sample_repository, "samples", counter)
    tool._blobs = _CountingProxy(blob_repository, "blobs", counter)
    tool._artifact_store = _CountingProxy(artifact_store, "artifact_store", counter)
    tool._investigations = _CountingProxy(tool._investigations, "investigations", counter)
    tool._article_snapshots = _CountingProxy(tool._article_snapshots, "article_snapshots", counter)
    tool._investigation_states = _CountingProxy(
        tool._investigation_states, "investigation_states", counter
    )
    tool._source_documents = _CountingProxy(SimpleNamespace(), "source_documents", counter)
    tool._opinions = _CountingProxy(SimpleNamespace(), "opinions", counter)
    return tool, transport, counter


def _manifest_reference_digest(rows: list[dict[str, object]]) -> str:
    encoded = json.dumps(rows, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("consultation_type", CONSULTATION_TYPES)
async def test_m12_q_reservation_and_execution_characterization(
    consultation_type: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cti_app.application.advisor_wire as advisor_wire_module
    import cti_app.application.investigation_advisor_tools as advisor_tools_module

    tool, transport, counter = _m12_q_fixture()
    capture: dict[str, list[dict[str, Any]]] = {"builds": [], "prompts": []}
    original_build = advisor_tools_module.build_advisor_dossier
    original_render = advisor_tools_module.render_advisor_prompt

    def capture_build(*args: object, **kwargs: object) -> object:
        dossier = original_build(*args, **kwargs)
        manifest_refs = [dict(item) for item in dossier.manifest]
        capture["builds"].append(
            {
                "dossier_sha256": dossier.sha256,
                "manifest_refs": manifest_refs,
                "manifest_refs_sha256": _manifest_reference_digest(manifest_refs),
            }
        )
        return dossier

    def capture_render(*args: object, **kwargs: object) -> str:
        prompt = original_render(*args, **kwargs)
        capture["prompts"].append(
            {
                "prompt_sha256": hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
                "bytes": len(prompt.encode("utf-8")),
            }
        )
        return prompt

    monkeypatch.setattr(advisor_tools_module, "build_advisor_dossier", capture_build)
    monkeypatch.setattr(advisor_wire_module, "build_advisor_dossier", capture_build)
    monkeypatch.setattr(advisor_tools_module, "render_advisor_prompt", capture_render)

    arguments = {
        "consultation_type": consultation_type,
        "question": "Compare only the fixed synthetic evidence in this offline fixture.",
    }
    operation_key = f"advisor-context-characterization:{consultation_type}"
    estimate_context = make_context(INVESTIGATION_ID, key=operation_key)
    reservation_started = time.perf_counter()
    reserved_cost = await tool.reservation_cost(arguments, estimate_context)
    reservation_ms = round((time.perf_counter() - reservation_started) * 1000)
    reservation_counts = counter.snapshot()
    reservation_builds = list(capture["builds"])
    reservation_prompts = list(capture["prompts"])

    budget = InvestigationBudget(limits={})
    budget.reserve(
        operation_key,
        {
            BudgetDimension.BRIDGE_TURNS: 1,
            BudgetDimension.TOOL_SECONDS: 3_600,
            **reserved_cost,
        },
    )
    context = replace(
        estimate_context,
        budget=budget,
        envelope=replace(estimate_context.envelope, budget=budget),
    )
    counter.reset()
    capture["builds"] = []
    capture["prompts"] = []
    execution_started = time.perf_counter()
    result = await tool.execute(arguments, context)
    execution_ms = round((time.perf_counter() - execution_started) * 1000)
    execution_counts = counter.snapshot()
    execution_builds = list(capture["builds"])
    execution_prompts = list(capture["prompts"])

    manifest_refs = [
        item for call in (*reservation_builds, *execution_builds) for item in call["manifest_refs"]
    ]

    def compact_builds(builds: list[dict[str, object]]) -> list[dict[str, object]]:
        return [
            {
                "dossier_sha256": item["dossier_sha256"],
                "manifest_refs_sha256": item["manifest_refs_sha256"],
                "manifest_rows": len(item["manifest_refs"]),
            }
            for item in builds
        ]

    report = {
        "consultation_type": consultation_type,
        "reservation": {
            "wall_ms": reservation_ms,
            "counts": reservation_counts,
            "dossier_builds": compact_builds(reservation_builds),
            "prompts": reservation_prompts,
        },
        "execute": {
            "wall_ms": execution_ms,
            "counts": execution_counts,
            "dossier_builds": compact_builds(execution_builds),
            "prompts": execution_prompts,
            "result_status": result.status.value,
            "transport_calls": len(transport.calls),
        },
        "manifest_ref_count": len(manifest_refs),
        "manifest_refs_sha256": _manifest_reference_digest(manifest_refs),
    }
    print(json.dumps(report, sort_keys=True))

    assert len(reservation_prompts) == 1
    assert len(execution_prompts) == 1
    assert reservation_prompts == execution_prompts
    assert len(reservation_builds) == 1
    assert len(execution_builds) == 1
    assert reservation_builds == execution_builds
    assert "content_base64:" not in transport.calls[0]["prompt"]
    assert (
        "content_omission_reason=whole_sample_not_needed_for_consultation"
        in (transport.calls[0]["prompt"])
    )
    # The final pre-send sample-policy recheck reloads associations after
    # preparation, response lookup, and pacing.
    expected_association_reads = 3
    assert (
        execution_counts["calls"].get("sample_associations.list_for_investigation")
        == expected_association_reads
    )
    assert execution_counts["calls"].get("samples.list_for_subject") == expected_association_reads
    assert reservation_counts["calls"].get("sample_associations.list_for_investigation") == 1
    assert execution_counts["association_sql_queries"] == 3 * expected_association_reads
    assert reservation_counts["association_sql_queries"] == 3
    assert execution_counts["max_observation_blob_reads"] <= 1
    assert execution_counts["observation_blob_reads"] <= len(counter.observation_blob_ids)
    assert reservation_counts["blob_reads"] == len(counter.observation_blob_ids)
    assert reservation_counts["bytes_read"] == execution_counts["bytes_read"]
    assert result.output["durations_ms"]["reservation_cost_ms"] >= 0
    diagnostics = result.output["diagnostics"]
    assert diagnostics["repo_calls"] > 0
    assert diagnostics["blob_reads"] == execution_counts["blob_reads"]
    assert diagnostics["bytes_read"] == execution_counts["bytes_read"]
    assert diagnostics["observations"] == 240
    assert diagnostics["samples"] == SAMPLE_COUNT


@pytest.mark.asyncio
@pytest.mark.parametrize("consultation_type", RESERVATION_PROPERTY_TYPES)
@pytest.mark.parametrize("include_sample_content", (False, True))
async def test_reservation_estimate_covers_measured_prompt(
    consultation_type: str,
    include_sample_content: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setitem(globals(), "SAMPLE_COUNT", 1)
    monkeypatch.setitem(globals(), "OBSERVATIONS_PER_SAMPLE", 6)
    monkeypatch.setitem(globals(), "SAMPLE_BYTES", 256 * 1024)
    tool, transport, _counter = _m12_q_fixture()
    if not include_sample_content:
        for blob_id, blob in tool._blobs._target.descriptors.items():
            if blob.descriptor.logical_bucket == "samples-quarantine":
                blob.descriptor.size = 0
                tool._artifact_store._target.values[blob_id] = b""

    operation_key = f"advisor-reservation-property:{consultation_type}:{include_sample_content}"
    arguments = {
        "consultation_type": consultation_type,
        "question": "Compare only the fixed synthetic evidence in this offline fixture.",
    }
    estimate_context = make_context(INVESTIGATION_ID, key=operation_key)
    reserved = await tool.reservation_cost(arguments, estimate_context)
    budget = InvestigationBudget(limits={})
    budget.reserve(
        operation_key,
        {
            BudgetDimension.BRIDGE_TURNS: 1,
            BudgetDimension.TOOL_SECONDS: 3_600,
            **reserved,
        },
    )
    context = replace(
        estimate_context,
        budget=budget,
        envelope=replace(estimate_context.envelope, budget=budget),
    )
    result = await tool.execute(arguments, context)

    assert len(transport.calls) == 1, result.output
    prompt_bytes = len(transport.calls[0]["prompt"].encode("utf-8"))
    actual_tokens = (prompt_bytes + 3) // 4
    reserved_bytes = reserved[BudgetDimension.CONTEXT_BYTES]
    reserved_tokens = reserved[BudgetDimension.CONTEXT_TOKENS]
    print(
        f"reservation_selection type={consultation_type} sample_content={include_sample_content} "
        f"estimated_bytes={reserved_bytes} actual_bytes={prompt_bytes}"
    )
    prompt = transport.calls[0]["prompt"]
    assert "content_base64:" not in prompt
    assert "content_omission_reason=whole_sample_not_needed_for_consultation" in prompt
    assert reserved_bytes < SAMPLE_BYTES * 2
    assert reserved_tokens < SAMPLE_BYTES // 2
    reservation = next(
        item for item in context.budget.reservations if item.operation_key == operation_key
    )
    assert reservation.amounts[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert reservation.amounts[BudgetDimension.CONTEXT_TOKENS] == actual_tokens
    assert result.budget_consumed[BudgetDimension.CONTEXT_BYTES] == prompt_bytes
    assert result.budget_consumed[BudgetDimension.CONTEXT_TOKENS] == actual_tokens
