from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.investigation_advisor_tools import register_investigation_advisor_tool
from cti_app.application.investigation_tools import InMemoryToolRegistry, RegistryToolDispatcher
from cti_app.domain.classification import TLP
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    FrameworkPreSubmissionProof,
    InvestigationFinding,
    ToolInvocationStatus,
)
from tests.test_investigation_advisor_tools import (
    FindingRepository,
    ObservationRepository,
    Transport,
    _make_downloaded_sample_fixture,
    _SampleAssociations,
    _Samples,
    make_dispatch_context,
    make_tool,
)

CONSULTATION_TYPES = (
    "plan_investigation",
    "pivot_strategy",
    "yara_draft",
    "review_text",
    "draft_text",
    "suricata_draft",
    "figure_plan",
)


def _observation(
    investigation_id, *, source, object_type, object_id, payload, kind=EvidenceKind.OTHER
):
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    blob_id = uuid4()
    observation = EvidenceObservation(
        investigation_id=investigation_id,
        kind=kind,
        blob_id=blob_id,
        sha256=hashlib.sha256(raw).hexdigest(),
        source=source,
        source_version="offline-chain-fixture.v1",
        observed_at=datetime.now(UTC),
        object_type=object_type,
        object_id=object_id,
    )
    return observation, blob_id, raw


def _seed_observation(tool, observation, blob_id, raw):
    tool._blobs.add(
        blob_id,
        sha256=observation.sha256,
        size=len(raw),
        logical_bucket="evidence",
    )
    tool._artifact_store.values[blob_id] = raw


async def _seed_chain(investigation_id, tool):
    sample_hashes = []
    sample_rows = []
    for index in range(15):
        sample_bytes = f"synthetic-chain-sample-{index}".encode("ascii")
        sample_hash = hashlib.sha256(sample_bytes).hexdigest()
        blob_id = uuid4()
        tool._blobs.add(
            blob_id,
            sha256=sample_hash,
            size=len(sample_bytes),
            logical_bucket="samples-quarantine",
        )
        tool._artifact_store.values[blob_id] = sample_bytes
        sample_hashes.append(sample_hash)
        sample_rows.append(
            SimpleNamespace(
                id=uuid4(),
                expected_hash=sample_hash,
                source_object_id=None,
                state="quarantined",
                blob_id=blob_id,
                origin_kind="source_seed",
                original_name=f"sample-{index}.bin",
                source_service="offline-fixture",
                tlp=TLP.GREEN,
                do_not_submit=False,
                external_llm_allowed=True,
                license_restriction=None,
            )
        )
    tool._samples = _Samples(sample_rows)
    tool._sample_associations = _SampleAssociations(sample_hashes)

    observations = []
    analysis_observations = []
    for index, sample_hash in enumerate(sample_hashes[:2]):
        payloads = (
            (
                "autowork.floss",
                "floss-3.1",
                "sample_analysis",
                {
                    "schema": "sample-obfuscated-strings.v1",
                    "sample_sha256": sample_hash,
                    "strings": [
                        {"offset": 512 + index, "value": f"SyntheticBeaconMarker{index}"},
                        {"offset": 620 + index, "value": "CreateRemoteThread"},
                    ],
                },
                EvidenceKind.BINARY_STRING,
            ),
            (
                "capa.analysis",
                "capa-9.1",
                "sample_analysis",
                {
                    "schema": "capa-analysis-observation.v1",
                    "sample_sha256": sample_hash,
                    "text": "capa capability: create process and resolve API by hash",
                },
                EvidenceKind.BINARY_ANALYSIS,
            ),
            (
                "dotnet.ilspycmd",
                "ilspycmd-9.0",
                "dotnet_analysis",
                {
                    "schema": "dotnet-analysis-observation.v1",
                    "sample_sha256": sample_hash,
                    "methods": [
                        {
                            "name": "InitializeLoader",
                            "code": "DecryptConfiguration(); CreateRemoteThread();",
                        }
                    ],
                },
                EvidenceKind.BINARY_ANALYSIS,
            ),
        )
        for source, source_version, object_type, payload, kind in payloads:
            observation, blob_id, raw = _observation(
                investigation_id,
                source=source,
                object_type=object_type,
                object_id=(f"{sample_hash}:ilspycmd:request" if "ilspy" in source else sample_hash),
                payload=payload,
                kind=kind,
            )
            observation = observation.__class__(
                investigation_id=observation.investigation_id,
                kind=observation.kind,
                blob_id=observation.blob_id,
                sha256=observation.sha256,
                source=observation.source,
                source_version=source_version,
                observed_at=observation.observed_at,
                object_type=observation.object_type,
                object_id=observation.object_id,
            )
            _seed_observation(tool, observation, blob_id, raw)
            observations.append(observation)
            if source == "autowork.floss":
                analysis_observations.append(observation)

    for index, sample_hash in enumerate(sample_hashes):
        observation, blob_id, raw = _observation(
            investigation_id,
            source="virustotal.file_report",
            object_type="sample",
            object_id=sample_hash,
            payload={
                "schema": "virustotal-file-report.v1",
                "sample_sha256": sample_hash,
                "detection_ratio": f"{index % 4 + 1}/70",
                "last_analysis_stats": {"malicious": index % 4 + 1, "undetected": 70},
                "popular_threat_classification": ["SyntheticLoader"],
            },
            kind=EvidenceKind.VT_FILE_REPORT,
        )
        _seed_observation(tool, observation, blob_id, raw)
        observations.append(observation)

    network, network_blob_id, network_raw = _observation(
        investigation_id,
        source="network.capture.inspect",
        object_type="network_capture",
        object_id="synthetic-capture",
        payload={
            "schema": "network-capture-inspection.v1",
            "flows": [{"src": "192.0.2.10:51322", "dst": "198.51.100.24:443"}],
            "dns": [{"query": "updates.example.test", "answers": ["198.51.100.24"]}],
            "tls": [{"sni": "updates.example.test", "version": "TLS1.2"}],
        },
    )
    _seed_observation(tool, network, network_blob_id, network_raw)
    observations.append(network)

    tool._observations = ObservationRepository(observations)
    tool._findings = FindingRepository(
        (
            InvestigationFinding(
                investigation_id=investigation_id,
                kind=FindingKind.CAPABILITY,
                assessment=FindingAssessment.INCONCLUSIVE,
                statement="The loader capability remains inconclusive.",
                observation_refs=(analysis_observations[0].id,),
                claim_refs=("S3P2.1",),
            ),
            InvestigationFinding(
                investigation_id=investigation_id,
                kind=FindingKind.CLAIM_VERIFICATION,
                assessment=FindingAssessment.QUALIFIED,
                statement="The persistence claim is qualified by static evidence.",
                observation_refs=(analysis_observations[0].id,),
                claim_refs=("S3P2.1",),
            ),
            InvestigationFinding(
                investigation_id=investigation_id,
                kind=FindingKind.NETWORK_BEHAVIOUR,
                assessment=FindingAssessment.QUALIFIED,
                statement="The observed destination is limited to one capture.",
                observation_refs=(network.id,),
                claim_refs=("S4P1.1",),
            ),
        )
    )
    investigation = await tool._investigations.get(investigation_id)
    snapshot = await tool._article_snapshots.resolve(investigation.synthesis_artifact_id)
    snapshot.article_passages = (
        SimpleNamespace(
            claim_id="S3P2.1",
            heading="Loader behavior",
            text="S3P2.1. The loader decrypts a configuration and starts a child process.",
            source_document_ids=(),
        ),
        SimpleNamespace(
            claim_id="S4P1.1",
            heading="Network behavior",
            text="S4P1.1. The sample resolves one update host and opens TLS traffic.",
            source_document_ids=(),
        ),
    )
    snapshot.article.claims = (
        SimpleNamespace(id="S3P2.1"),
        SimpleNamespace(id="S4P1.1"),
    )
    return sample_hashes


async def _dispatch(tool, context, consultation_type, *, arguments=None, pre_dispatch_check=None):
    registry = InMemoryToolRegistry()
    register_investigation_advisor_tool(registry, adapter=tool)
    request = {
        "consultation_type": consultation_type,
        "question": (
            "Review claims S3P2.1 and S4P1.1 using SyntheticBeaconMarker strings, FLOSS, "
            "capa, ILSpy, VT and network observations."
        ),
        **(arguments or {}),
    }
    return await RegistryToolDispatcher(registry).dispatch(
        context,
        tool_name="advisor.consult",
        schema_version="advisor-consult.v1",
        arguments=request,
        purpose="Run an offline minimal-call Bridge advisor chain test.",
        operation_key=context.idempotency_key,
        pre_dispatch_check=pre_dispatch_check,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("consultation_type", CONSULTATION_TYPES)
async def test_minimal_call_auto_assembles_canonical_dossier_and_returns_advisory(
    consultation_type: str,
) -> None:
    investigation_id = uuid4()
    output = (
        "The associated analysis supports a cautious conclusion; validate it locally."
        if consultation_type != "yara_draft"
        else (
            "The measured strings are a tentative indicator.\n"
            "```yara\nrule synthetic_marker {\n"
            '  strings: $a = "SyntheticBeaconMarker0"\n'
            "  condition: $a\n}\n"
            "```"
        )
    )
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text=output))
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)
    await _seed_chain(investigation_id, tool)
    context = make_dispatch_context(investigation_id)

    result = await _dispatch(tool, context, consultation_type)
    assert len(transport.calls) == 1, (
        result.status.value,
        result.error_code,
        result.message,
        dict(result.output),
        result.output.get("missing_refs"),
        [
            (item.get("kind"), item.get("id"), item.get("statuses"))
            for item in result.output.get("completeness_manifest", ())
            if isinstance(item, dict) and item.get("required")
        ],
    )
    prompt = transport.calls[0]["prompt"]
    assert "json" not in prompt.casefold()
    assert "S3P2.1" in prompt and "S4P1.1" in prompt
    assert "SyntheticBeaconMarker" in prompt
    assert "InitializeLoader" in prompt or "DecryptConfiguration" in prompt
    assert "VirusTotal" in prompt or "detection_ratio" in prompt
    assert "The loader capability remains inconclusive" in prompt
    assert "The persistence claim is qualified" in prompt
    assert result.output["untrusted"] is True
    assert output in result.output["advisory_text"]
    assert result.budget_consumed
    context_bytes = next(key for key in result.budget_consumed if key.value == "context_bytes")
    assert result.budget_consumed[context_bytes] == len(prompt.encode("utf-8"))
    if consultation_type == "suricata_draft":
        assert "updates.example.test" in prompt
    if consultation_type == "yara_draft":
        assert "SyntheticBeaconMarker0" in prompt


@pytest.mark.asyncio
async def test_explicit_restricted_sample_stays_blocked_with_actionable_reason() -> None:
    investigation_id = uuid4()
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)
    sample_hashes = await _seed_chain(investigation_id, tool)
    sample = tool._samples.samples[0]
    tool._samples.samples = (
        SimpleNamespace(**{**vars(sample), "tlp": TLP.RED}),
        *tool._samples.samples[1:],
    )
    context = make_dispatch_context(investigation_id)

    result = await _dispatch(
        tool,
        context,
        "plan_investigation",
        arguments={"sample_sha256": sample_hashes[0]},
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert "TLP RED" in result.message or "TLP:RED" in result.message
    assert transport.calls == []


@pytest.mark.asyncio
async def test_revoked_g2_stays_blocked_before_fake_bridge() -> None:
    investigation_id = uuid4()
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)
    await _seed_chain(investigation_id, tool)
    context = make_dispatch_context(investigation_id)

    async def revoked(_context):
        return FrameworkPreSubmissionProof.ENVELOPE_REVOKED

    result = await _dispatch(
        tool,
        context,
        "plan_investigation",
        pre_dispatch_check=revoked,
    )

    assert result.status is ToolInvocationStatus.POLICY_DENIED
    assert result.framework_pre_submission_proof is FrameworkPreSubmissionProof.ENVELOPE_REVOKED
    assert transport.calls == []


@pytest.mark.asyncio
async def test_unassociated_sample_hash_is_rejected_with_association_action() -> None:
    investigation_id = uuid4()
    transport = Transport(TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="unused"))
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)
    await _seed_chain(investigation_id, tool)
    context = make_dispatch_context(investigation_id)
    absent_hash = hashlib.sha256(b"not-associated").hexdigest()

    result = await _dispatch(
        tool,
        context,
        "plan_investigation",
        arguments={"sample_sha256": absent_hash},
    )

    assert result.status in {ToolInvocationStatus.FAILED, ToolInvocationStatus.POLICY_DENIED}
    assert "sample.analyze" in result.message
    assert transport.calls == []


@pytest.mark.asyncio
async def test_invalid_optional_assertion_link_does_not_block_consultive_draft() -> None:
    investigation_id = uuid4()
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="Draft prose to verify.")
    )
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)
    await _seed_chain(investigation_id, tool)
    context = make_dispatch_context(investigation_id)

    result = await _dispatch(
        tool,
        context,
        "draft_text",
        arguments={
            "assertions": [
                {
                    "id": "claim_one",
                    "text": "A consultative claim with an absent optional observation link.",
                    "nature": "analyse",
                    "refs": [{"kind": "observation", "id": str(uuid4())}],
                    "block_type": "key_findings",
                    "form": "body",
                    "anchor": None,
                }
            ]
        },
    )

    assert len(transport.calls) == 1, result
    assert "Draft prose to verify." in result.output["advisory_text"]
    assert result.output["untrusted"] is True


@pytest.mark.asyncio
async def test_unselected_restricted_sample_is_excluded_without_blocking_other_evidence() -> None:
    investigation_id = uuid4()
    transport = Transport(
        TurnSubmission(TurnSubmissionStatus.SUCCEEDED, output_text="Allowed evidence summary.")
    )
    tool, _ = make_tool(investigation_id=investigation_id, transport=transport)
    sample_hashes = await _seed_chain(investigation_id, tool)
    red_sample = tool._samples.samples[-1]
    tool._samples.samples = (
        *tool._samples.samples[:-1],
        SimpleNamespace(**{**vars(red_sample), "tlp": TLP.RED}),
    )
    context = make_dispatch_context(investigation_id)

    result = await _dispatch(tool, context, "plan_investigation")

    assert len(transport.calls) == 1, result
    assert sample_hashes[-1] not in transport.calls[0]["prompt"]
    assert "S3P2.1" in transport.calls[0]["prompt"]


@pytest.mark.asyncio
async def test_large_associated_sample_is_sent_complete_with_exact_dispatch_reservation() -> None:
    investigation_id = uuid4()
    sample_content = bytes(index % 251 for index in range(3_200_000))
    tool, transport, observation, _sample_id, _association_id, _blob_id, sample_hash = (
        _make_downloaded_sample_fixture(investigation_id, sample_content)
    )
    transport.submission = TurnSubmission(
        TurnSubmissionStatus.SUCCEEDED,
        output_text="Complete sample received for review.",
    )
    context = make_dispatch_context(investigation_id)

    result = await _dispatch(
        tool,
        context,
        "plan_investigation",
        arguments={
            "question": "Review the verified associated sample bytes.",
            "evidence_refs": [str(observation.id)],
        },
    )

    assert len(transport.calls) == 1, result
    prompt = transport.calls[0]["prompt"]
    assert base64.b64encode(sample_content).decode("ascii") in prompt
    assert "length=3200000" in prompt
    assert "byte_length=3200000" in prompt
    assert "omitted_range" not in prompt
    assert sample_hash in prompt
    prompt_bytes = len(prompt.encode("utf-8"))
    assert (
        result.budget_consumed[
            next(key for key in result.budget_consumed if key.value == "context_bytes")
        ]
        == prompt_bytes
    )
    reservation = next(
        item
        for item in context.budget.reservations
        if item.operation_key == context.idempotency_key
    )
    assert (
        reservation.amounts[
            next(key for key in reservation.amounts if key.value == "context_bytes")
        ]
        == prompt_bytes
    )
