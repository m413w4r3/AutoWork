"""W16 PostgreSQL proof for durable interrupt, operator read, and resume."""

from __future__ import annotations

import asyncio
from pathlib import Path
from uuid import UUID

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import select

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import ScriptedTurnTransport
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import BudgetDimension
from cti_app.domain.investigation_state import StopReason
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.investigation import InvestigationWorkingStateRow
from tests.workflow_emulator.investigation_support import (
    HashCheckedVirusTotal,
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


class _WaitingBridge(ScriptedTurnTransport):
    def __init__(self) -> None:
        super().__init__(
            [decision("Finish from the persisted Bridge response.", next_action="finish")]
        )
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def submit(self, **kwargs):
        self.entered.set()
        await self.release.wait()
        return await super().submit(**kwargs)


async def _working_state_checkpoint(uow_factory, investigation_id: UUID) -> tuple[int, object]:
    async with uow_factory() as uow:
        row = await uow._session.scalar(
            select(InvestigationWorkingStateRow).where(
                InvestigationWorkingStateRow.investigation_id == investigation_id
            )
        )
        assert row is not None
        return row.version, row.updated_at


@pytest.mark.asyncio
async def test_w16_interrupt_during_bridge_wait_is_durable_read_only_and_resumable(
    uow_factory, tmp_path: Path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "w16-investigation-blobs"), uow_factory)
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix="w16-" + tmp_path.name,
    )
    transport = _WaitingBridge()
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=investigation_settings(providers=False),
        transport=transport,
    )
    app, registry, dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await start_investigation_http(client, seeded)
        approved = await approve_envelope_http(
            client,
            seeded.investigation.id,
            providers=(),
            budget_limits={"bridge_turns": 4},
            allow_acquisition=False,
        )
        assert approved.status_code == 200, approved.text
        job_id = UUID(started["job_id"])
        worker = asyncio.create_task(run_http_job(app, uow_factory, registry, dispatcher, job_id))
        await asyncio.wait_for(transport.entered.wait(), timeout=10)

        version_before_read, updated_at_before_read = await _working_state_checkpoint(
            uow_factory, seeded.investigation.id
        )
        status_before = await client.get(
            f"/api/investigations/{seeded.investigation.id}/operator-status"
        )
        assert status_before.status_code == 200, status_before.text
        status_body = status_before.json()
        assert status_body["investigation_id"] == str(seeded.investigation.id)
        assert status_body["subject_id"] == str(seeded.subject.id)
        assert status_body["job_generation"] == 1
        assert status_body["pending_context_snapshot_id"] is not None
        assert status_body["last_operation"]["kind"] == "bridge_turn"
        assert status_body["last_operation"]["id"]
        assert status_body["g2"]["approved"] is True

        version_after_read, updated_at_after_read = await _working_state_checkpoint(
            uow_factory, seeded.investigation.id
        )
        assert (version_after_read, updated_at_after_read) == (
            version_before_read,
            updated_at_before_read,
        )

        interrupted = await client.post(
            f"/api/investigations/{seeded.investigation.id}/interrupt",
            json={"expected_version": status_body["working_state_version"]},
            headers={"Idempotency-Key": "w16-durable-interrupt"},
        )
        assert interrupted.status_code == 200, interrupted.text
        assert interrupted.json()["investigation"]["stop_reason"] == StopReason.USER_INTERRUPT.value
        transport.release.set()
        await worker

        async with uow_factory() as uow:
            stopped = await uow.investigation_states.get(seeded.investigation.id)
        assert stopped is not None
        assert stopped.stop_reason is StopReason.USER_INTERRUPT
        assert stopped.pending_context_snapshot_id is not None
        assert transport.submission_count == 1

        resumed = await client.post(
            f"/api/investigations/{seeded.investigation.id}/resume",
            json={"expected_version": interrupted.json()["investigation"]["version"]},
            headers={"Idempotency-Key": "w16-durable-resume"},
        )
        assert resumed.status_code == 200, resumed.text
        await run_http_job(
            app,
            uow_factory,
            registry,
            dispatcher,
            UUID(resumed.json()["job_id"]),
        )

    async with uow_factory() as uow:
        completed = await uow.investigation_states.get(seeded.investigation.id)
    assert completed is not None
    assert completed.turn_index == 1
    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert transport.submission_count == 1


class _WaitingVirusTotal(HashCheckedVirusTotal):
    def __init__(self, material: SyntheticSampleMaterial) -> None:
        super().__init__(material=material)
        self.entered = asyncio.Event()
        self.release = asyncio.Event()

    async def file_report(self, file_hash: str):
        self.entered.set()
        await self.release.wait()
        return await super().file_report(file_hash)


@pytest.mark.asyncio
async def test_w16_g2_revocation_during_vt_archives_reply_and_blocks_followup(
    uow_factory, tmp_path: Path
) -> None:
    material = SyntheticSampleMaterial.unique()
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "w16-revocation-blobs"), uow_factory)
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix="w16-revoke-" + tmp_path.name,
        material=material,
    )
    first_call = {
        "tool_name": "vt.file_report",
        "schema_version": "1",
        "arguments": {"file_hash": material.seed_hash},
        "purpose": "Read the first synthetic file report.",
    }
    second_call = {
        **first_call,
        "purpose": "Read the report again under a distinct operation key.",
    }
    response = decision("Read two bounded synthetic file reports.", next_action="finish")
    response["proposed_tool_calls"] = [first_call, second_call]
    transport = ScriptedTurnTransport([response])
    virustotal = _WaitingVirusTotal(material)
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=investigation_settings(providers=True),
        transport=transport,
        virustotal=virustotal,
    )
    app, registry, dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await start_investigation_http(client, seeded)
        approved = await approve_envelope_http(
            client,
            seeded.investigation.id,
            providers=("vt",),
            budget_limits={"bridge_turns": 4, "vt_units": 4},
            allow_acquisition=False,
        )
        assert approved.status_code == 200, approved.text
        worker = asyncio.create_task(
            run_http_job(app, uow_factory, registry, dispatcher, UUID(started["job_id"]))
        )
        await asyncio.wait_for(virustotal.entered.wait(), timeout=10)

        revoked = await client.post(
            f"/api/investigations/{seeded.investigation.id}/envelope/revoke",
            json={
                "expected_version": approved.json()["version"],
                "reason": "Revoke G2 while the first read is in flight.",
                "correlation_id": "w16-revoke-in-flight-vt",
            },
            headers={"Idempotency-Key": "w16-g2-revoke-in-flight"},
        )
        assert revoked.status_code == 200, revoked.text
        virustotal.release.set()
        await worker
        async with uow_factory() as uow:
            stopped = await uow.investigation_states.get(seeded.investigation.id)
            invocations = await uow.tool_invocations.list_for_investigation(seeded.investigation.id)
            observations = await uow.evidence_observations.list_for_investigation(
                seeded.investigation.id
            )
            current_envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
        assert stopped is not None
        assert stopped.stop_reason is StopReason.PERMISSION_REQUIRED
        assert current_envelope is not None
        assert current_envelope.allowed_providers == ()
        assert virustotal.calls["file_report"] == 1
        succeeded = [item for item in invocations if item.status.value == "succeeded"]
        denied = [item for item in invocations if item.status.value == "policy_denied"]
        assert len(succeeded) == 1
        assert len(denied) == 1
        assert len(succeeded[0].response_refs) == 1
        first_observation_id = succeeded[0].response_refs[0]
        assert len(observations) == 1
        assert transport.submission_count == 1

        extended = await client.post(
            f"/api/investigations/{seeded.investigation.id}/envelope/extend",
            json={
                "expected_version": current_envelope.version,
                "allowed_providers": ["vt"],
                "budget_limits": {"bridge_turns": 4, "vt_units": 4},
                "acquire_hits_allowed": False,
                "max_acquire_volume": 0,
                "disclosure": {
                    "tlp": "clear",
                    "do_not_submit": False,
                    "external_llm_allowed": True,
                },
                "reason": "Restore the bounded VT read permission for the next run.",
                "correlation_id": "w16-reapprove-after-revoke",
            },
            headers={"Idempotency-Key": "w16-g2-extend-after-revoke"},
        )
        assert extended.status_code == 200, extended.text
        resumed = await client.post(
            f"/api/investigations/{seeded.investigation.id}/resume",
            json={"expected_version": stopped.version},
            headers={"Idempotency-Key": "w16-resume-after-g2-revoke"},
        )
        assert resumed.status_code == 200, resumed.text
        await run_http_job(
            app,
            uow_factory,
            registry,
            dispatcher,
            UUID(resumed.json()["job_id"]),
        )

    async with uow_factory() as uow:
        completed = await uow.investigation_states.get(seeded.investigation.id)
        final_invocations = await uow.tool_invocations.list_for_investigation(
            seeded.investigation.id
        )
        final_observations = await uow.evidence_observations.list_for_investigation(
            seeded.investigation.id
        )
        final_envelope = await uow.investigation_envelopes.get(seeded.investigation.id)
    assert completed is not None
    assert completed.stop_reason is StopReason.AGENT_FINISHED
    assert final_envelope is not None
    assert final_envelope.budget.consumed[BudgetDimension.VT_UNITS] == 2
    assert virustotal.calls["file_report"] == 2
    assert transport.submission_count == 1
    assert len([item for item in final_invocations if item.status.value == "succeeded"]) == 2
    assert len([item for item in final_invocations if item.status.value == "policy_denied"]) == 1
    assert len(final_observations) == 2
    assert sum(item.id == first_observation_id for item in final_observations) == 1
