from __future__ import annotations

import hashlib
from dataclasses import replace
from uuid import UUID, uuid4

import httpx
import pytest

from cti_app.application.investigation_composition import OptionalInvestigationToolPorts
from cti_app.domain.investigation import ToolInvocationStatus
from cti_app.domain.investigation_state import StopReason
from tests.workflow_emulator.driver import WorkflowDriver
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal,
    ScriptedInvestigationTransport,
    SyntheticSampleMaterial,
    approve_envelope_http,
    build_investigation_app,
    composition_factory,
    decision,
    investigation_settings,
    run_http_job,
    seed_verified_article,
    start_investigation_http,
)

pytestmark = [pytest.mark.integration, pytest.mark.workflow_fast]


class _G1Transport(ScriptedInvestigationTransport):
    def __init__(self, *, uow_factory, investigation_id, material, evidence_id) -> None:
        super().__init__(
            (),
            uow_factory=uow_factory,
            investigation_id=investigation_id,
            material=material,
            inject_analysis_at_turn=None,
            analyze_pending_samples=False,
        )
        self._evidence_id = evidence_id
        self._phase = 0
        self.association_id: UUID | None = None

    async def submit(self, **kwargs):
        async with self._uow_factory() as uow:
            associations = tuple(
                await uow.sample_associations.list_for_investigation(self._investigation_id)
            )
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(self._investigation_id)
            )
        association = next(
            (item for item in associations if item.sample_sha256 == self._material.sample_hash),
            None,
        )
        if self._phase == 0:
            response = decision(
                "Download the exact synthetic VT sample.",
                tool_call={
                    "tool_name": "vt.download_sample",
                    "schema_version": "1",
                    "purpose": "Acquire the article-linked sample for local analysis.",
                    "arguments": {
                        "sha256": self._material.sample_hash,
                        "purpose": "Acquire one quarantined synthetic sample.",
                        "evidence_refs": [str(self._evidence_id)],
                    },
                },
            )
        elif self._phase == 1:
            assert association is not None
            self.association_id = association.id
            assert str(association.id) in kwargs["prompt"]
            assert "not_analyzed" in kwargs["prompt"]
            response = decision(
                "Attempt to finish before analyzing the downloaded sample.",
                next_action="finish",
            )
        elif self._phase == 2:
            assert association is not None
            assert "finish_gate" in kwargs["prompt"]
            sample_evidence = next(
                item
                for item in observations
                if item.object_type == "sample" and item.object_id == association.sample_sha256
            )
            response = decision(
                "Analyze the association identified by the acquisition result.",
                tool_call={
                    "tool_name": "sample.analyze",
                    "schema_version": "1",
                    "purpose": "Run bounded static analysis on the quarantined sample.",
                    "arguments": {
                        "association_id": str(association.id),
                        "purpose": "Inspect the acquired sample in the isolated worker.",
                        "evidence_refs": [str(sample_evidence.id)],
                    },
                },
            )
        else:
            assert association is not None
            assert "analyzed" in kwargs["prompt"]
            response = decision(
                "Finish after the local sample analysis was recorded.",
                next_action="finish",
            )
        self._phase += 1
        self._responses.append(response)
        return await super().submit(**kwargs)


async def test_w15_download_finish_refusal_analysis_then_finish(
    workflow_driver: WorkflowDriver,
    uow_factory,
) -> None:
    driver = workflow_driver
    driver.begin("W15-G1-download-analysis-finish-gate")
    async with driver:
        material = SyntheticSampleMaterial.unique()
        sample_bytes = b"MZ\x00W15-inert-sample:" + uuid4().bytes
        sample_hash = hashlib.sha256(sample_bytes).hexdigest()
        material = replace(material, sample_bytes=sample_bytes, sample_hash=sample_hash)
        seeded = await seed_verified_article(
            uow_factory,
            driver.artifact_store,
            suffix=f"w15-g1-{uuid4().hex[:8]}",
            material=material,
            article_claim=f"The report documents sample SHA-256 {sample_hash}.",
        )
        vt = HashCheckedVirusTotal(material=material)
        transport = _G1Transport(
            uow_factory=uow_factory,
            investigation_id=seeded.investigation.id,
            material=material,
            evidence_id=seeded.seed_observation.id,
        )
        settings = investigation_settings(providers=True, local_analysis=True, rules=False)
        factory = composition_factory(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            settings=settings,
            transport=transport,
            virustotal=vt,
            include_local_analysis=True,
            additional_ports=OptionalInvestigationToolPorts(),
        )
        app, registry, dispatcher = build_investigation_app(
            uow_factory=uow_factory,
            artifact_store=driver.artifact_store,
            composition_factory=factory,
        )
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://w15-g1"
        ) as client:
            started = await start_investigation_http(client, seeded)
            approved = await approve_envelope_http(
                client,
                seeded.investigation.id,
                providers=("vt",),
                budget_limits={"bridge_turns": 8, "vt_units": 8, "downloads": 1},
                allow_acquisition=True,
                max_acquire_volume=1,
            )
            assert approved.status_code == 200, approved.text
            driver.edition_id = seeded.edition.id
            await driver._perform(
                "run_g1_workflow",
                lambda: run_http_job(
                    app, uow_factory, registry, dispatcher, UUID(started["job_id"])
                ),
                via="service",
                action_name="download, gate early finish, analyze, and finish",
                identities={"investigation": seeded.investigation.id},
                assertions=("registry-dispatch-context-flow", "postgres-persistence", "no-network"),
            )

        async with uow_factory() as uow:
            associations = tuple(
                await uow.sample_associations.list_for_investigation(seeded.investigation.id)
            )
            invocations = tuple(
                await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
            )
            observations = tuple(
                await uow.evidence_observations.list_for_investigation(seeded.investigation.id)
            )
            state = await uow.investigation_states.get(seeded.investigation.id)

        association = next(item for item in associations if item.sample_sha256 == sample_hash)
        download = next(item for item in invocations if item.tool_name == "vt.download_sample")
        analysis = next(item for item in invocations if item.tool_name == "sample.analyze")
        assert download.status is ToolInvocationStatus.SUCCEEDED
        assert analysis.status in {ToolInvocationStatus.SUCCEEDED, ToolInvocationStatus.PARTIAL}
        assert analysis.normalized_parameters["association_id"] == str(association.id)
        assert analysis.response_refs
        assert transport.association_id == association.id
        assert str(association.id) in transport.prompts[1]
        assert "finish_gate" in transport.prompts[2]
        assert "analyzed" in transport.prompts[3]
        assert state is not None and state.stop_reason is StopReason.AGENT_FINISHED
        assert any(action.startswith("finish_gate_refused:") for action in state.recent_actions)
        assert any(
            item.source.startswith("autowork.analysis_worker") and item.object_id == sample_hash
            for item in observations
        )
