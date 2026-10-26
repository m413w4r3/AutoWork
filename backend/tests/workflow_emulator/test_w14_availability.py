"""PostgreSQL proof that optional-tool outages stay local and resumable."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import replace
from typing import Any
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.behavior_report_import import BehaviorReportImporter
from cti_app.application.bridge_investigator import TurnSubmission, TurnSubmissionStatus
from cti_app.application.investigation_composition import OptionalInvestigationToolPorts
from cti_app.domain.analyst_opinion import OpinionBlockType, OpinionClaimQualification
from cti_app.domain.behavior_evidence import BehaviorEvidenceSource
from cti_app.domain.investigation import (
    AgentNextAction,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    NoveltyClass,
    SampleOrigin,
    ToolInvocationStatus,
)
from tests.workflow_emulator.driver import WorkflowDriver
from tests.workflow_emulator.investigation_support import (
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_envelope_http,
    approve_opinion_http,
    build_investigation_app,
    compose_opinion_http,
    composition_factory,
    decision,
    investigation_settings,
    persist_sample_association_fixture,
    revise_opinion_http,
    run_http_job,
    seed_verified_article,
    start_investigation_http,
)
from tests.workflow_emulator.test_w14_agent_opinion import (
    _assert_observations_reach_following_bridge_turns,
    _call,
)

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]


class _UnavailableFloss:
    available = False
    unavailable_reason = "floss_not_installed_in_workflow_fixture"

    def __init__(self) -> None:
        self.calls = 0

    async def extract(self, sample_sha256: str, *, max_strings: int) -> Any:
        del sample_sha256, max_strings
        self.calls += 1
        raise AssertionError("unavailable FLOSS fake must never run")


class _UnavailableMwcp:
    available = False
    unavailable_reason = "mwcp_not_installed_in_workflow_fixture"

    def __init__(self) -> None:
        self.calls = 0
        self.allowed_parsers = {"strict-fixture-parser": "not-installed"}

    async def extract(self, sample_sha256: str, *, parser_id: str) -> Any:
        del sample_sha256, parser_id
        self.calls += 1
        raise AssertionError("unavailable MWCP fake must never run")


@pytest.mark.asyncio
async def test_w14_f_optional_outages_preserve_proof_and_resume_without_replay(
    workflow_driver: WorkflowDriver,
    uow_factory,
) -> None:
    driver = workflow_driver
    driver.begin("W14-F-optional-outages-resume")
    async with driver:
        bytes_sample = b"MZ\x00W14-F-static-proof:" + uuid4().bytes + b":inert"
        sample_sha = hashlib.sha256(bytes_sample).hexdigest()
        material = replace(
            SyntheticSampleMaterial.unique(),
            seed_hash=sample_sha,
            sample_bytes=bytes_sample,
            sample_hash=sample_sha,
        )
        seeded = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w14-f-{uuid4().hex[:8]}",
            material=material,
            article_claim=f"The bounded report documents quarantined sample {sample_sha}.",
        )
        await persist_sample_association_fixture(
            uow_factory,
            driver.artifact_store,
            investigation_id=seeded.investigation.id,
            subject_id=seeded.subject.id,
            sample_bytes=bytes_sample,
            origin=SampleOrigin.SEED,
            logical_name="w14-f-inert.bin",
        )
        report_json = json.dumps(
            {
                "sample_sha256": sample_sha,
                "process_tree": [{"pid": 1, "name": "sample.exe"}],
                "network_sessions": [
                    {
                        "protocol": "tcp",
                        "destination_ip": "192.0.2.144",
                        "destination_port": 443,
                        "host": "available-tool-fixture.invalid",
                    }
                ],
            },
            sort_keys=True,
        )
        report_sha256 = hashlib.sha256(report_json.encode("utf-8")).hexdigest()
        floss = _UnavailableFloss()
        mwcp = _UnavailableMwcp()
        transport_holder: dict[str, ScriptedInvestigationTransport] = {}

        async def decide_from_persisted_evidence(
            observations: tuple[EvidenceObservation, ...],
        ) -> Mapping[str, Any] | TurnSubmission:
            async with uow_factory() as uow:
                invocations = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                findings = tuple(
                    await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
                )
            called = {item.tool_name for item in invocations}
            if "native.inspect" not in called:
                return decision(
                    "Check the default-disabled native inspection capability without changing "
                    "policy.",
                    tool_call=_call(
                        "native.inspect",
                        {"sample_sha256": sample_sha, "operation": "imports"},
                    ),
                )
            if "vt.file_report" not in called:
                return decision(
                    "Check that a missing VT provider is reported locally.",
                    tool_call=_call("vt.file_report", {"file_hash": sample_sha}),
                )
            if "vt.retrohunt.submit" not in called:
                return decision(
                    "Check that Retrohunt is unavailable without a proxy route or human rule "
                    "approval.",
                    tool_call=_call(
                        "vt.retrohunt.submit",
                        {
                            "version_id": str(uuid4()),
                            "source_sha256": "a" * 64,
                            "corpus_id": "unavailable-fixture",
                            "corpus_sha256": "b" * 64,
                            "parameters": {},
                        },
                    ),
                )
            if "sample.extract_obfuscated_strings" not in called:
                return decision(
                    "Ask the unavailable FLOSS adapter and continue using other local tools.",
                    tool_call=_call(
                        "sample.extract_obfuscated_strings",
                        {"sample_sha256": sample_sha},
                    ),
                )
            if "sample.extract_configuration" not in called:
                return decision(
                    "Ask the unavailable but parser-allowlisted MWCP adapter.",
                    tool_call=_call(
                        "sample.extract_configuration",
                        {"sample_sha256": sample_sha, "parser_id": "strict-fixture-parser"},
                    ),
                )
            if "sandbox.report.import" not in called:
                return decision(
                    "Import the exact operator report while optional extractors are unavailable.",
                    tool_call=_call(
                        "sandbox.report.import",
                        {
                            "sample_sha256": sample_sha,
                            "source": BehaviorEvidenceSource.OPERATOR.value,
                            "report_json": report_json,
                        },
                    ),
                )
            if not findings:
                static = next(
                    item
                    for item in observations
                    if item.kind is EvidenceKind.BINARY_ANALYSIS
                    and item.source == "autowork.analysis_worker"
                    and item.object_id == sample_sha
                )
                imported = next(
                    item
                    for item in observations
                    if item.source == "autowork.sandbox-report.operator"
                    and item.object_id == sample_sha
                )
                transport_holder["transport"]._responses.append(
                    TurnSubmission(
                        TurnSubmissionStatus.NEEDS_REVIEW,
                        detail="simulated Bridge loss after the local finding was persisted",
                    )
                )
                return decision(
                    "Use the retained static and imported report evidence; label the combined "
                    "chain as qualified.",
                    next_action=AgentNextAction.CONTINUE.value,
                    finding_proposals=[
                        {
                            "kind": FindingKind.INFECTION_CHAIN.value,
                            "assessment": FindingAssessment.QUALIFIED.value,
                            "statement": (
                                "Static analysis and the supplied operator report describe one "
                                "bounded sample; their relationship remains qualified evidence."
                            ),
                            "subject_ref": sample_sha,
                            "observation_refs": [str(static.id), str(imported.id)],
                            "claim_refs": [],
                            "novelty": NoveltyClass.NOT_CONFIRMED.value,
                            "scope": "One exact quarantined sample and one imported report.",
                            "limitations": [
                                "Unavailable tools did not contribute evidence.",
                                "The report describes only its supplied run.",
                            ],
                        }
                    ],
                )
            return TurnSubmission(
                TurnSubmissionStatus.NEEDS_REVIEW,
                detail="resume must reuse this ambiguous Bridge turn",
            )

        settings = investigation_settings(providers=False, local_analysis=True, rules=False)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=ScriptedInvestigationTransport(
                [],
                uow_factory=uow_factory,
                investigation_id=seeded.investigation.id,
                material=material,
                inject_analysis_at_turn=None,
                analyze_pending_samples=True,
                final_response_builder=decide_from_persisted_evidence,
            ),
            include_local_analysis=True,
            additional_ports=OptionalInvestigationToolPorts(
                floss_extraction=floss,
                configuration_extraction=mwcp,
                behavior_report_importer=BehaviorReportImporter(),
            ),
        )
        transport_holder["transport"] = factory.turn_transport  # type: ignore[assignment]
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=factory,
        )
        async with uow_factory() as uow:
            declared = factory.build(uow, seeded.investigation.id)
            assert "native.inspect" in declared.unavailable_tools
            assert "vt.file_report" in declared.unavailable_tools
            assert "vt.retrohunt.submit" in declared.unavailable_tools
            assert "sample.extract_obfuscated_strings" in declared.unavailable_tools
            assert "sample.extract_configuration" in declared.unavailable_tools
            assert "sample.analyze" in declared.available_tools
            assert "sandbox.report.import" in declared.available_tools

        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1"
        ) as client:
            started = await start_investigation_http(client, seeded)
            approved = await approve_envelope_http(
                client,
                seeded.investigation.id,
                providers=(),
                budget_limits={
                    "bridge_turns": 30,
                    "vt_units": 0,
                    "shodan_credits": 0,
                    "downloads": 0,
                },
                allow_acquisition=False,
                max_acquire_volume=0,
            )
            assert approved.status_code == 200, approved.text
            await run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))
            transport = transport_holder["transport"]
            assert transport.submission_count >= 8
            attempts_before_resume = transport.attempt_count
            submissions_before_resume = transport.submission_count
            async with uow_factory() as uow:
                invocations_before = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                observations_before = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
                findings_before = tuple(
                    await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
                )
                state_before = await uow.investigation_states.get(seeded.investigation.id)
                persisted_envelope_before = await uow.investigation_envelopes.get(
                    seeded.investigation.id
                )

            assert state_before is not None and state_before.stop_reason is not None
            assert state_before.stop_reason.value == "submission_needs_review"
            assert len(findings_before) == 1
            names = {item.tool_name for item in invocations_before}
            assert {
                "sample.analyze",
                "sandbox.report.import",
                "native.inspect",
                "vt.file_report",
                "vt.retrohunt.submit",
                "sample.extract_obfuscated_strings",
                "sample.extract_configuration",
            } <= names
            by_name = {item.tool_name: item for item in invocations_before}
            assert all(
                by_name[name].status is ToolInvocationStatus.UNAVAILABLE
                for name in (
                    "native.inspect",
                    "vt.file_report",
                    "vt.retrohunt.submit",
                    "sample.extract_obfuscated_strings",
                    "sample.extract_configuration",
                )
            )
            assert by_name["sandbox.report.import"].status is ToolInvocationStatus.SUCCEEDED
            assert by_name["sample.analyze"].status in {
                ToolInvocationStatus.SUCCEEDED,
                ToolInvocationStatus.PARTIAL,
            }
            assert floss.calls == mwcp.calls == 0
            static_observation = next(
                item
                for item in observations_before
                if item.kind is EvidenceKind.BINARY_ANALYSIS
                and item.source == "autowork.analysis_worker"
                and item.object_id == sample_sha
            )
            report_observation = next(
                item
                for item in observations_before
                if item.source == "autowork.sandbox-report.operator"
                and item.object_id == sample_sha
            )
            static_payload_bytes = await driver.artifact_store.read_bytes(
                static_observation.blob_id
            )
            assert hashlib.sha256(static_payload_bytes).hexdigest() == static_observation.sha256
            report_payload = await driver.artifact_store.read_json(report_observation.blob_id)
            assert report_payload["source_report_sha256"] == report_sha256
            assert (
                report_observation.sha256
                == hashlib.sha256(
                    await driver.artifact_store.read_bytes(report_observation.blob_id)
                ).hexdigest()
            )
            assert (
                sum(
                    item.kind is EvidenceKind.BINARY_ANALYSIS
                    and item.source == "autowork.analysis_worker"
                    and item.object_id == sample_sha
                    for item in observations_before
                )
                == 1
            )
            assert (
                sum(
                    item.source == "autowork.sandbox-report.operator"
                    for item in observations_before
                )
                == 1
            )
            _assert_observations_reach_following_bridge_turns(transport, invocations_before)

            current = await client.get(f"/api/investigations/{seeded.investigation.id}")
            assert current.status_code == 200, current.text
            resumed = await client.post(
                f"/api/investigations/{seeded.investigation.id}/resume",
                json={"expected_version": current.json()["version"]},
                headers={"Idempotency-Key": f"w14-f-resume-{seeded.investigation.id}"},
            )
            assert resumed.status_code == 200, resumed.text
            await run_http_job(
                app,
                uow_factory,
                registry,
                dispatcher,
                UUID(resumed.json()["job_id"]),
            )
            assert transport.submission_count == submissions_before_resume
            assert transport.attempt_count == attempts_before_resume + 1

            async with uow_factory() as uow:
                invocations_after = tuple(
                    await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
                )
                observations_after = tuple(
                    await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
                )
                findings_after = tuple(
                    await uow.investigation_findings.list_for_investigation(seeded.investigation.id)
                )
                state_after = await uow.investigation_states.get(seeded.investigation.id)
                persisted_envelope_after = await uow.investigation_envelopes.get(
                    seeded.investigation.id
                )
            assert len(invocations_after) == len(invocations_before)
            assert {item.id for item in observations_after} == {
                item.id for item in observations_before
            }
            assert len(findings_after) == 1
            assert state_after is not None
            assert state_after.stop_reason is not None
            assert state_after.stop_reason.value == "submission_needs_review"
            assert persisted_envelope_before == persisted_envelope_after
            assert persisted_envelope_after is not None
            assert persisted_envelope_after.allowed_providers == ()
            assert persisted_envelope_after.budget.limits.get("vt_units", 0) == 0
            assert started["job_id"]

            final_finding = findings_after[0]
            composed = await compose_opinion_http(client, seeded.investigation.id)
            assert composed.status_code == 200, composed.text
            draft = composed.json()["draft"]
            revised = await revise_opinion_http(
                client,
                UUID(draft["id"]),
                expected_version=draft["version"],
                blocks=(
                    {
                        "block_type": OpinionBlockType.INFECTION_CHAIN_AND_CAPABILITIES.value,
                        "text": (
                            "Static sample inspection and the supplied operator report were "
                            "retained. "
                            "Their connection in one bounded chain is an inference; unavailable "
                            "REA, VT, FLOSS, and MWCP contributed no evidence."
                        ),
                        "finding_refs": [str(final_finding.id)],
                        "evidence_refs": [str(static_observation.id), str(report_observation.id)],
                        "qualification": OpinionClaimQualification.INFERENCE.value,
                        "certainty": 0.58,
                    },
                ),
                unresolved_points=("Optional native and provider tools remain unavailable.",),
            )
            assert revised.status_code == 200, revised.text
            opinion = revised.json()["draft"]
            approved_opinion = await approve_opinion_http(
                client,
                UUID(opinion["id"]),
                version=opinion["version"],
                sha256=opinion["content_sha256"],
            )
            assert approved_opinion.status_code == 200, approved_opinion.text
            async with uow_factory() as uow:
                persisted_opinion = await uow.analyst_opinions.get(UUID(opinion["id"]))
            assert persisted_opinion is not None and persisted_opinion.status.value == "approved"
            assert persisted_opinion.blocks[0].qualification is OpinionClaimQualification.INFERENCE
