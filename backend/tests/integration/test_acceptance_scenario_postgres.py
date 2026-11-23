"""M24 lot 10-S — synthetic 24-step acceptance scenario.

Every layer is exercised in order against a real PostgreSQL unit of work, the
real host workspace sidecar on a tmpdir AF_UNIX socket, the real FastAPI
application (``create_app`` + ASGITransport) and the real Typst compiler.
VirusTotal, Bridge and the Codex/Claude CLIs are deterministic fakes; no live
provider, CLI, network call or real CTI data is ever contacted.

The scenario is a sequential class: each ``step_NN_*`` method is one auditable
assertion of the user mandate, and every step appends a line to the journal.
A failure stops the run at that step and prints the journal, so later steps are
reported as "not reached" instead of silently passing.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import stat
import subprocess
import time
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

import pytest

from cti_app.domain.analyst_opinion import (
    OpinionBlockType,
    note_ioc_table_title,
    verify_opinion_citations,
)
from cti_app.domain.editorial import AnalystDecisionType
from cti_app.domain.entities import SampleState
from cti_app.domain.investigation import (
    FindingAssessment,
    InvestigationExecutionMode,
    ToolInvocationActorKind,
    is_advisory_observation,
)
from cti_app.domain.production import ProseAuthority
from tests.acceptance.scenario_support.harness import (
    ANALYSIS_SECTION_PARAGRAPH,
    TAIL_SECTION_PARAGRAPH,
    RunningScenarioAgent,
    ScenarioApplication,
    ScenarioMaterial,
    SeededArticle,
    advisory_consult_text,
    bridge_submission,
    build_scenario_application,
    install_fake_cli_profiles,
    note_draft_response,
    plan_text,
    running_scenario_agent,
    seed_article,
    session_secret,
    settle_and_sync,
    signed_tool_call,
)

pytestmark = pytest.mark.integration

NOTE_PATH = "notes/humaines/analyste-note.md"
OBSERVATION_NOTE_PATH = "notes/humaines/claude-observation.md"
FINDING_NOTE_PATH = "notes/humaines/claude-verification.md"
NOTE_A_ANCHOR = "lead:0001"
NOTE_B_ANCHOR = "section:1:paragraph:0001"
# The overview restates the lead: the reader projection drops it, so a note may
# not anchor there even though the raw text-anchor space contains it.
DEDUPLICATED_ANCHOR = "section:0:paragraph:0001"
NOTE_A_TEXT = "NOTE-ALPHA-ANCRAGE: le chargeur synthetique contacte le domaine de commande."
NOTE_A_REVISED = (
    "NOTE-ALPHA-ANCRAGE-REVISE: le chargeur synthetique contacte le domaine de commande "
    "apres une verification independante."
)
NOTE_B_TEXT = "NOTE-BETA-ANCRAGE: la verification confirme le second etage synthetique."


@dataclass
class StepJournal:
    entries: list[tuple[str, str]] = field(default_factory=list)

    def record(self, step: str, detail: str) -> None:
        self.entries.append((step, detail))

    def render(self) -> str:
        return "\n".join(f"{step}: {detail}" for step, detail in self.entries)


def _json(response: Any, *, expected: int = 200) -> Any:
    assert response.status_code == expected, (
        f"{response.request.method} {response.request.url} -> "
        f"{response.status_code}: {response.text[:4000]}"
    )
    return response.json()


@dataclass
class JourneyState:
    seeded: SeededArticle | None = None
    investigation_id: UUID | None = None
    workspace_id: str = ""
    workspace_root: Path | None = None
    snapshot_id: UUID | None = None
    plan_version_id: UUID | None = None
    task_ids: dict[str, UUID] = field(default_factory=dict)
    loader_task_id: UUID | None = None
    codex_task_id: UUID | None = None
    codex_session_id: UUID | None = None
    codex_secret: bytes = b""
    claude_session_id: UUID | None = None
    claude_secret: bytes = b""
    codex_vt_observation_id: UUID | None = None
    claude_vt_observation_id: UUID | None = None
    imported_observation_id: UUID | None = None
    imported_finding_id: UUID | None = None
    proposal_a_id: UUID | None = None
    proposal_b_id: UUID | None = None
    correction_proposal_id: UUID | None = None
    opinion_v1_sha: str = ""
    opinion_v2_sha: str = ""
    opinion_v3_sha: str = ""
    opinion_id: UUID | None = None
    pdf_path: Path | None = None


class ScenarioJourney:
    """One sequential run of the user's 24-step synthetic scenario."""

    def __init__(
        self,
        *,
        scenario: ScenarioApplication,
        agent: RunningScenarioAgent,
        uow_factory: Any,
        tmp_path: Path,
    ) -> None:
        self.scenario = scenario
        self.agent = agent
        self.uow_factory = uow_factory
        self.tmp_path = tmp_path
        self.journal = StepJournal()
        self.state = JourneyState()

    # ------------------------------------------------------------- helpers

    async def _human(self, client: Any, method: str, path: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}))
        headers.update(self.scenario.human_headers)
        response = await client.request(method, path, headers=headers, **kwargs)
        return response

    async def _operator_state(self, client: Any) -> dict[str, Any]:
        response = await self._human(
            client,
            "GET",
            f"/api/investigations/{self.state.investigation_id}/operator-status",
            params={"view": "compact"},
        )
        return cast(dict[str, Any], _json(response))

    async def _opinion(self, client: Any) -> dict[str, Any]:
        response = await self._human(
            client,
            "GET",
            f"/api/investigations/{self.state.investigation_id}/opinion",
        )
        payload = _json(response)
        assert payload["draft"] is not None, payload
        return cast(dict[str, Any], payload["draft"])

    async def _accept_proposal(
        self,
        client: Any,
        proposal: dict[str, Any],
        *,
        base_version: int | None,
        base_sha256: str | None,
        key: str,
    ) -> dict[str, Any]:
        response = await self._human(
            client,
            "POST",
            f"/api/investigations/{self.state.investigation_id}/note-proposals/"
            f"{proposal['id']}/accept",
            json={
                "expected_status_version": proposal["status_version"],
                "expected_base_version": base_version,
                "expected_base_sha256": base_sha256,
                "reason": "Acceptation humaine par version et hash",
            },
            headers={"Idempotency-Key": key},
        )
        return cast(dict[str, Any], _json(response))

    # --------------------------------------------------------------- steps

    async def step_01_start_investigation_via_api(self, client: Any) -> None:
        assert self.state.seeded is not None
        seeded = self.state.seeded
        response = await self._human(
            client,
            "POST",
            f"/api/subjects/{seeded.subject.id}/investigations",
            json={
                "production_run_id": str(seeded.run.id),
                "synthesis_artifact_id": str(seeded.synthesis.id),
                "synthesis_sha256": seeded.synthesis_sha256,
                "execution_mode": "operator_led",
                "creation_profile": "m24_workshop",
            },
        )
        payload = _json(response)
        investigation_id = UUID(payload["investigation"]["id"])
        self.state.investigation_id = investigation_id
        async with self.uow_factory() as uow:
            investigation = await uow.analyst_investigations.get(investigation_id)
            state = await uow.investigation_states.get(investigation_id)
        assert investigation is not None
        assert investigation.prose_authority is ProseAuthority.BRIDGE_ONLY
        assert state is not None
        assert state.execution_mode is InvestigationExecutionMode.OPERATOR_LED
        assert self.scenario.dispatcher.job_ids, "the start route must submit one durable job"
        self.journal.record(
            "step_01_start_investigation_via_api",
            f"investigation={investigation_id} operator_led bridge_only job_submitted",
        )

    async def step_02_workspace_created_by_real_sidecar(self, client: Any) -> None:
        response = await self._human(
            client,
            "GET",
            f"/api/investigations/{self.state.investigation_id}/workspace",
        )
        payload = _json(response)
        assert payload["workspace_available"] is True, payload
        binding = payload["binding"]
        assert binding is not None
        self.state.workspace_id = binding["workspace_id"]
        seeded = self.state.seeded
        assert seeded is not None
        root = self.agent.root / "IR202611" / seeded.subject.slug
        manifest_path = root / ".autowork" / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert manifest["investigation_id"] == str(self.state.investigation_id)
        assert manifest["synthesis_sha256"] == seeded.synthesis_sha256
        assert manifest["subject_id"] == str(seeded.subject.id)
        async with self.uow_factory() as uow:
            stored = await uow.workspace_bindings.get_for_investigation(self.state.investigation_id)
        assert stored is not None
        assert stored.workspace_id == self.state.workspace_id
        self.state.workspace_root = root
        self.journal.record(
            "step_02_workspace_created_by_real_sidecar",
            f"workspace={self.state.workspace_id} manifest={manifest_path.name} real_uds",
        )

    async def step_03_ioc_inventory(self, client: Any) -> None:
        response = await self._human(
            client,
            "GET",
            f"/api/investigations/{self.state.investigation_id}/bootstrap/inventory",
        )
        inventory = _json(response)
        material = self.scenario.material
        seeds = inventory["seeds"]
        by_kind: dict[str, list[str]] = {}
        for seed in seeds:
            by_kind.setdefault(seed["kind"], []).append(seed["value"])
        assert set(by_kind["sample"]) == set(material.all_hashes)
        assert set(by_kind["domain"]) == set(material.domains)
        assert set(by_kind["ipv4"]) == set(material.ips)
        assert inventory["pinned_synthesis_sha256"] == self.state.seeded.synthesis_sha256
        assert inventory["workspace_binding_id"] == self.state.workspace_id
        self.journal.record(
            "step_03_ioc_inventory",
            "5 hashes + 2 domains + 2 ips seeded inventory, synthesis pinned",
        )

    async def step_04_g2_envelope_proposed_and_approved(self, client: Any) -> None:
        proposal = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/bootstrap/envelope-proposal",
            )
        )
        assert proposal["approved"] is False
        assert "vt" in proposal["allowed_providers"], proposal
        assert proposal["acquire_hits_allowed"] is True, proposal
        assert proposal["planned_operations"] >= 14, proposal
        approved = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/bootstrap/envelope/approve",
                json={
                    "reason": "Approbation humaine G2 de l'enveloppe initiale synthetique",
                    "correlation_id": "m24-acceptance-g2",
                    "proposal_sha256": proposal["proposal_sha256"],
                },
                headers={"Idempotency-Key": "m24-acceptance-g2-approve"},
            )
        )
        assert approved["approved"] is True, approved
        async with self.uow_factory() as uow:
            envelope = await uow.investigation_envelopes.get(self.state.investigation_id)
            decisions = await uow.analyst_decisions.list_for_investigation(
                self.state.investigation_id
            )
        assert envelope is not None
        assert envelope.approval_decision_id is not None
        assert envelope.disclosure.external_llm_allowed is True
        assert any(
            decision.decision_type is AnalystDecisionType.PIVOT_APPROVE for decision in decisions
        )
        self.journal.record(
            "step_04_g2_envelope_proposed_and_approved",
            f"proposal_sha={proposal['proposal_sha256'][:12]} approved_version="
            f"{envelope.version} human_decision",
        )

    async def step_05_fake_vt_collection(self, client: Any) -> None:
        snapshot = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/bootstrap/run",
                headers={"Idempotency-Key": "m24-acceptance-bootstrap-run"},
            )
        )
        self.state.snapshot_id = UUID(snapshot["snapshot_id"])
        material = self.scenario.material
        iocs = {ioc["value"]: ioc for ioc in snapshot["iocs"]}
        assert set(material.all_hashes) <= set(iocs)
        downloaded = [
            ioc
            for ioc in snapshot["iocs"]
            if ioc["download"] is not None and ioc["download"]["status"] in {"downloaded", "reused"}
        ]
        failed = [
            ioc
            for ioc in snapshot["iocs"]
            if ioc["download"] is not None and ioc["download"]["status"] == "failed"
        ]
        assert len(downloaded) == 3, json.dumps(snapshot, indent=2, ensure_ascii=False)
        assert len(failed) == 2, snapshot
        for ioc in downloaded:
            assert ioc["download"]["hash_verified_quarantine"] is True
        assert all(
            operation["status"] in {"succeeded", "partial"}
            for ioc in snapshot["iocs"]
            for operation in ioc["operations"]
            if operation["tool_name"] in {"vt.file_report", "vt.object_report"}
        )
        assert self.scenario.virus_total.calls["file_report"] >= 5
        assert self.scenario.virus_total.calls["object_report"] >= 4
        async with self.uow_factory() as uow:
            observations = await uow.evidence_observations.list_for_investigation(
                self.state.investigation_id
            )
        assert any(
            observation.object_type == "investigation_bootstrap_snapshot"
            for observation in observations
        )
        self.journal.record(
            "step_05_fake_vt_collection",
            "snapshot persisted, 5 file reports, 4 object reports, 3 downloads, 2 unavailable",
        )

    async def step_06_controlled_sample_materialization(self, client: Any) -> None:
        material = self.scenario.material
        root = self.state.workspace_root
        assert root is not None
        for digest in material.available_hashes:
            async with self.uow_factory() as uow:
                samples = await uow.samples.list_by_subject_and_sha256(
                    self.state.seeded.subject.id, digest
                )
            controlled = [
                sample
                for sample in samples
                if sample.state in {SampleState.QUARANTINED, SampleState.REVIEW_CANDIDATE}
            ]
            assert len(controlled) == 1, (digest, samples)
            assert all(sample.state is not SampleState.VALIDATED for sample in samples)
            content = await self.scenario.artifact_store.read_bytes(controlled[0].blob_id)
            assert hashlib.sha256(content).hexdigest() == digest
            result = await self.scenario.workspace_port.materialize_base_sample(
                self.state.workspace_id,
                sha256=digest,
                content=content,
                size=len(content),
                idempotency_key=f"m24-acceptance-materialize-{digest[:8]}",
            )
            assert result.sha256 == digest
            assert result.mode == "0440"
            target = root / "base_samples" / digest
            assert target.is_file()
            assert stat.S_IMODE(target.stat().st_mode) == 0o440
            assert target.read_bytes() == content
            replay = await self.scenario.workspace_port.materialize_base_sample(
                self.state.workspace_id,
                sha256=digest,
                content=content,
                size=len(content),
                idempotency_key=f"m24-acceptance-materialize-{digest[:8]}",
            )
            assert replay.already_present is True
        for digest in material.unavailable_hashes:
            async with self.uow_factory() as uow:
                samples = await uow.samples.list_by_subject_and_sha256(
                    self.state.seeded.subject.id, digest
                )
            assert not samples
            assert not (root / "base_samples" / digest).exists()
        materialized = sorted(
            path.name for path in (root / "base_samples").iterdir() if path.is_file()
        )
        assert materialized == sorted(material.available_hashes)
        self.journal.record(
            "step_06_controlled_sample_materialization",
            "3 quarantined samples materialized read-only (0440), 2 unavailable absent",
        )

    async def step_07_bridge_plan_accepted(self, client: Any) -> None:
        state = await self._operator_state(client)
        self.scenario.bridge.enqueue(
            bridge_submission(plan_text(self.scenario.material, snapshot_id=self.state.snapshot_id))
        )
        requested = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/plans/request",
                json={
                    "expected_version": state["working_state_version"],
                    "expected_sha256": state["working_state_sha256"],
                    "question": "Etablis le plan synthetique d'acceptation.",
                },
                headers={"Idempotency-Key": "m24-acceptance-plan-request"},
            )
        )
        assert requested["advice_only"] is False, requested
        assert requested["plan_version_id"] is not None
        assert requested["plan_parse_status"] in {"parsed", "partially_parsed"}
        plan_version_id = UUID(requested["plan_version_id"])
        self.state.plan_version_id = plan_version_id
        detail = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/plans/{plan_version_id}",
            )
        )
        accepted = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/plans/{plan_version_id}/accept",
                json={
                    "expected_sha256": detail["content_sha256"],
                    "reason": "Plan synthetique accepte par l'humain",
                },
                headers={"Idempotency-Key": "m24-acceptance-plan-accept"},
            )
        )
        tasks = accepted["tasks"]
        assert len(tasks) >= 2
        loader = next(task for task in tasks if "chargeur" in task["title"].casefold())
        self.state.task_ids = {task["task_key"]: UUID(task["task_id"]) for task in tasks}
        self.state.loader_task_id = UUID(loader["task_id"])
        async with self.uow_factory() as uow:
            stored = await uow.investigation_tasks.list_for_investigation(
                self.state.investigation_id
            )
            decisions = await uow.analyst_decisions.list_for_investigation(
                self.state.investigation_id
            )
        assert len(stored) == len(tasks)
        assert any(
            decision.decision_type is AnalystDecisionType.PLAN_ACCEPT for decision in decisions
        )
        self.journal.record(
            "step_07_bridge_plan_accepted",
            f"plan v{requested['plan_version']} materialized {len(stored)} tasks in one decision",
        )

    async def step_08_task_assigned_and_codex_session(self, client: Any) -> None:
        task_id = self.state.loader_task_id
        assert task_id is not None
        task = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/tasks/{task_id}",
            )
        )
        assigned = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/tasks/{task_id}/commands",
                json={
                    "command": "assign",
                    "expected_status_version": task["status_version"],
                    "target": {"actor_kind": "operator", "actor_id": "codex:operator-1"},
                },
                headers={"Idempotency-Key": "m24-acceptance-assign-codex"},
            )
        )
        assert assigned["task"]["status"] == "assigned", assigned
        handoff = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/handoffs",
                json={
                    "task_id": str(task_id),
                    "to_actor": {"kind": "operator", "actor_id": "codex:operator-1"},
                    "next_action": "Inspecter l'echantillon synthetique materialise.",
                },
                headers={"Idempotency-Key": "m24-acceptance-handoff-codex"},
            )
        )
        assert handoff["version"] == 1
        opened = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/agent-sessions",
                json={
                    "task_id": str(task_id),
                    "adapter_kind": "codex_cli",
                    "handoff_id": handoff["handoff_id"],
                    "executable": "/usr/bin/fake-codex",
                    "model": "fake-codex-model",
                },
                headers={"Idempotency-Key": "m24-acceptance-open-codex"},
            )
        )
        session = opened["session"]
        self.state.codex_session_id = UUID(session["id"])
        self.state.codex_task_id = task_id
        self.state.codex_secret = await session_secret(
            self.uow_factory, self.state.codex_session_id
        )
        assert session["state"] == "running", session
        running_task = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/tasks/{task_id}",
            )
        )
        assert running_task["status"] == "running"
        assert running_task["lease_fence"] == 1
        assert running_task["lease_session_id"] == str(self.state.codex_session_id)
        self.journal.record(
            "step_08_task_assigned_and_codex_session",
            f"task assigned, handoff v1, real sidecar session {self.state.codex_session_id} "
            "running with lease fence 1",
        )

    async def step_09_codex_inspects_sample_via_broker(self, client: Any) -> None:
        material = self.scenario.material
        response = await signed_tool_call(
            client,
            investigation_id=self.state.investigation_id,
            session_id=self.state.codex_session_id,
            secret=self.state.codex_secret,
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"file_hash": material.available_hashes[0]},
            idempotency_key="m24-acceptance-codex-vt-read",
        )
        payload = _json(response)
        assert payload["status"] in {"succeeded", "partial"}, payload
        assert payload["error_code"] is None, payload
        async with self.uow_factory() as uow:
            invocation = await uow.tool_invocations.get(UUID(payload["invocation_id"]))
            observations = await uow.evidence_observations.list_for_investigation(
                self.state.investigation_id
            )
        assert invocation is not None
        assert invocation.task_id == self.state.codex_task_id
        assert invocation.agent_session_id == self.state.codex_session_id
        assert invocation.actor_kind is ToolInvocationActorKind.OPERATOR
        assert invocation.actor_id == "codex:operator-1"
        reader = next(
            observation
            for observation in observations
            if observation.object_id == material.available_hashes[0]
            and observation.kind.value == "vt_file_report"
        )
        self.state.codex_vt_observation_id = reader.id
        self.journal.record(
            "step_09_codex_inspects_sample_via_broker",
            f"broker invocation {invocation.id} attributed task/session, VT observation "
            f"{reader.id}",
        )

    async def step_10_codex_consults_bridge(self, client: Any) -> None:
        self.scenario.bridge.enqueue(bridge_submission(advisory_consult_text()))
        response = await signed_tool_call(
            client,
            investigation_id=self.state.investigation_id,
            session_id=self.state.codex_session_id,
            secret=self.state.codex_secret,
            tool_name="advisor.consult",
            schema_version="advisor-consult.v1",
            arguments={
                "consultation_type": "pivot_strategy",
                "question": "Quel pivot synthetique confirme le rapport fichier ?",
                "evidence_refs": [str(self.state.codex_vt_observation_id)],
            },
            idempotency_key="m24-acceptance-codex-bridge",
        )
        payload = _json(response)
        assert payload["status"] in {"succeeded", "partial"}, payload
        assert payload["error_code"] is None, payload
        async with self.uow_factory() as uow:
            invocation = await uow.tool_invocations.get(UUID(payload["invocation_id"]))
            observations = await uow.evidence_observations.list_for_investigation(
                self.state.investigation_id
            )
        assert invocation is not None
        assert invocation.task_id == self.state.codex_task_id
        assert invocation.agent_session_id == self.state.codex_session_id
        sources = {observation.source for observation in observations}
        assert {"advisor.consult", "advisor.consult.response"} <= sources
        assert any(
            observation.source == "advisor.consult.response"
            and observation.id in invocation.response_refs
            for observation in observations
        )
        self.journal.record(
            "step_10_codex_consults_bridge",
            f"advisor.consult {invocation.id} persisted call+response for the signed session",
        )

    async def step_11_human_local_markdown_note(self, client: Any) -> None:
        root = self.state.workspace_root
        assert root is not None
        target = root / NOTE_PATH
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            "# Note humaine\n\nObservation locale synthetique ecrite sur disque.\n",
            encoding="utf-8",
        )
        report = await settle_and_sync(client, self.state.investigation_id)
        assert report["events_inserted"] >= 1, report
        async with self.uow_factory() as uow:
            revisions = await uow.workspace_file_revisions.list_for_workspace(
                self.state.workspace_id
            )
            events = await uow.workspace_file_events.list_for_workspace(self.state.workspace_id)
        matching = [revision for revision in revisions if revision.path == NOTE_PATH]
        assert len(matching) == 1, matching
        assert any(event.path == NOTE_PATH for event in events)
        self.journal.record(
            "step_11_human_local_markdown_note",
            f"disk note versioned revision={matching[0].revision_id} real watcher ingest",
        )

    async def step_12_note_visible_via_api(self, client: Any) -> None:
        listing = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/workspace/files",
                params={"prefix": "notes/humaines"},
            )
        )
        view = next(item for item in listing["files"] if item["path"] == NOTE_PATH)
        assert view["import_status"] == "received"
        assert view["origin"] == "filesystem"
        content = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/workspace/files/content",
                params={"path": NOTE_PATH},
            )
        )
        assert "Observation locale synthetique" in content["text"]
        assert content["sha256"] == view["sha256"]
        note_view = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/workspace/notes/"
                f"{NOTE_PATH}/view",
            )
        )
        assert note_view["path"] == NOTE_PATH
        assert note_view["latest"]["sha256"] == view["sha256"]
        assert note_view["is_evidence"] is False
        self.journal.record(
            "step_12_note_visible_via_api",
            f"note listed+read via API sha={view['sha256'][:12]}, note view non-evidence",
        )

    async def step_13_transfer_to_claude_code(self, client: Any) -> None:
        task_id = self.state.loader_task_id
        assert task_id is not None
        task = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/tasks/{task_id}",
            )
        )
        transferred = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/tasks/{task_id}/transfer",
                json={
                    "to_actor_kind": "operator",
                    "to_adapter_kind": "claude_code",
                    "expected_status_version": task["status_version"],
                    "open_session": True,
                    "executable": "/usr/bin/fake-claude",
                    "model": "fake-claude-model",
                    "instruction": "Approfondis la verification du second echantillon synthetique.",
                },
                headers={"Idempotency-Key": "m24-acceptance-transfer-claude"},
            )
        )
        assert transferred["destination"]["actor_id"] == "operator:claude_code"
        assert transferred["handoff"]["version"] == 2, transferred["handoff"]
        assert transferred["previous_handoff_version"] == 1
        session = transferred["session"]
        assert session is not None, transferred
        assert session["state"] == "running", session
        self.state.claude_session_id = UUID(session["id"])
        self.state.claude_secret = await session_secret(
            self.uow_factory, self.state.claude_session_id
        )
        sessions = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/agent-sessions",
                params={"task_id": str(task_id)},
            )
        )
        codex = next(item for item in sessions if item["id"] == str(self.state.codex_session_id))
        assert codex["state"] in {"stopped", "closed", "lost"}, codex
        self.journal.record(
            "step_13_transfer_to_claude_code",
            f"transfer via handoff v2 to operator:claude_code, Claude session "
            f"{self.state.claude_session_id} running, Codex {codex['state']}",
        )

    async def step_14_claude_deepens_verification(self, client: Any) -> None:
        material = self.scenario.material
        response = await signed_tool_call(
            client,
            investigation_id=self.state.investigation_id,
            session_id=self.state.claude_session_id,
            secret=self.state.claude_secret,
            tool_name="vt.file_report",
            schema_version="1",
            arguments={"file_hash": material.available_hashes[1]},
            idempotency_key="m24-acceptance-claude-vt-read",
        )
        payload = _json(response)
        assert payload["status"] in {"succeeded", "partial"}, payload
        assert payload["error_code"] is None, payload
        async with self.uow_factory() as uow:
            invocation = await uow.tool_invocations.get(UUID(payload["invocation_id"]))
            observations = await uow.evidence_observations.list_for_investigation(
                self.state.investigation_id
            )
        assert invocation is not None
        assert invocation.agent_session_id == self.state.claude_session_id
        assert invocation.task_id == self.state.codex_task_id
        assert invocation.actor_id == "operator:claude_code"
        deep = next(
            observation
            for observation in observations
            if observation.object_id == material.available_hashes[1]
            and observation.kind.value == "vt_file_report"
        )
        self.state.claude_vt_observation_id = deep.id
        self.journal.record(
            "step_14_claude_deepens_verification",
            f"signed Claude invocation {invocation.id} deepened sample 2 -> observation {deep.id}",
        )

    async def step_15_import_observation_and_finding(self, client: Any) -> None:
        root = self.state.workspace_root
        assert root is not None
        observation_note = root / OBSERVATION_NOTE_PATH
        observation_note.parent.mkdir(parents=True, exist_ok=True)
        observation_note.write_text(
            "Extraction synthetique: chaine NOTE-OBS-IMPORT attendue dans l'echantillon.\n",
            encoding="utf-8",
        )
        await settle_and_sync(client, self.state.investigation_id)
        listing = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/workspace/files",
                params={"prefix": "notes/humaines"},
            )
        )
        observation_view = next(
            item for item in listing["files"] if item["path"] == OBSERVATION_NOTE_PATH
        )
        observation_contribution = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/contributions",
                json={
                    "workspace_id": self.state.workspace_id,
                    "workspace_revision_id": observation_view["revision_id"],
                    "category": "observation",
                    "origin": "human",
                    "author_actor_id": "operator:static-token",
                    "observation": {
                        "object_type": "file",
                        "method": "string_extraction",
                        "object_id": observation_view["sha256"],
                        "tool": "strings",
                        "tool_version": "synthetic-1",
                        "parameters": {"encoding": "utf-8"},
                        "limitations": ["Synthetic acceptance fixture only."],
                        "observed_at": "2026-11-22T12:30:00Z",
                    },
                },
                headers={"Idempotency-Key": "m24-acceptance-import-observation"},
            )
        )
        assert observation_contribution["import_status"] == "imported", observation_contribution
        assert observation_contribution["observation_id"] is not None
        assert observation_contribution["finding_id"] is None
        imported_observation_id = UUID(observation_contribution["observation_id"])
        self.state.imported_observation_id = imported_observation_id

        finding_note = root / FINDING_NOTE_PATH
        finding_note.write_text(
            "Verification synthetique de la chaine en deux etages; finding non auto-supporte.\n",
            encoding="utf-8",
        )
        await settle_and_sync(client, self.state.investigation_id)
        listing = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/workspace/files",
                params={"prefix": "notes/humaines"},
            )
        )
        finding_view = next(item for item in listing["files"] if item["path"] == FINDING_NOTE_PATH)
        finding_contribution = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/contributions",
                json={
                    "workspace_id": self.state.workspace_id,
                    "workspace_revision_id": finding_view["revision_id"],
                    "category": "finding",
                    "origin": "human",
                    "author_actor_id": "operator:static-token",
                    "finding": {
                        "kind": "claim_verification",
                        "assessment": "supported",
                        "statement": (
                            "NOTE-FINDING-IMPORT: la verification synthetique confirme le "
                            "second etage sans preuve directe suffisante."
                        ),
                        "observation_refs": [
                            str(imported_observation_id),
                            str(self.state.claude_vt_observation_id),
                        ],
                        "novelty": "new_to_article",
                        "scope": "Synthetic acceptance scope.",
                        "limitations": ["Imported finding is never auto-supported."],
                    },
                },
                headers={"Idempotency-Key": "m24-acceptance-import-finding"},
            )
        )
        assert finding_contribution["import_status"] == "imported", finding_contribution
        assert finding_contribution["finding_id"] is not None
        finding_id = UUID(finding_contribution["finding_id"])
        self.state.imported_finding_id = finding_id
        async with self.uow_factory() as uow:
            finding = await uow.investigation_findings.get(finding_id)
            observation = await uow.evidence_observations.get(imported_observation_id)
        assert finding is not None
        assert finding.assessment is FindingAssessment.INCONCLUSIVE, finding
        assert observation is not None
        assert observation.source == "workspace_import"
        self.journal.record(
            "step_15_import_observation_and_finding",
            f"observation {imported_observation_id} imported via workspace_import; finding "
            f"{finding_id} downgraded to inconclusive (never auto-supported)",
        )

    async def step_16_bridge_drafts_two_notes(self, client: Any) -> None:
        finding_id = self.state.imported_finding_id
        observation_id = self.state.imported_observation_id
        assert finding_id is not None and observation_id is not None
        state = await self._operator_state(client)
        refused = await self._human(
            client,
            "POST",
            f"/api/investigations/{self.state.investigation_id}/note-proposals",
            json={
                "anchor": DEDUPLICATED_ANCHOR,
                "target_block_type": "section",
                "bridge_block_type": "key_findings",
                "consultation_type": "draft_text",
                "finding_refs": [str(finding_id)],
                "observation_refs": [str(self.state.claude_vt_observation_id)],
                "task_id": str(self.state.loader_task_id),
                "expected_working_state_version": state["working_state_version"],
                "expected_working_state_sha256": state["working_state_sha256"],
            },
            headers={"Idempotency-Key": "m24-acceptance-note-deduplicated-refused"},
        )
        assert refused.status_code == 422, refused.text
        assert refused.json()["detail"]["code"] == "invalid_note_anchor", refused.json()
        drafts = (
            (NOTE_A_ANCHOR, "lead", NOTE_A_TEXT, "m24-acceptance-note-a"),
            (NOTE_B_ANCHOR, "section", NOTE_B_TEXT, "m24-acceptance-note-b"),
        )
        proposal_ids: list[UUID] = []
        for anchor, target_block_type, text, key in drafts:
            state = await self._operator_state(client)
            invocation_id = uuid4()
            draft = note_draft_response(
                block_type="key_findings",
                anchor=anchor,
                text=text,
                bridge_invocation_id=invocation_id,
            )
            self.scenario.bridge.enqueue(
                bridge_submission(
                    draft["output_text"],
                    bridge_invocation_id=invocation_id,
                )
            )
            body: dict[str, Any] = {
                "anchor": anchor,
                "target_block_type": target_block_type,
                "bridge_block_type": "key_findings",
                "consultation_type": "draft_text",
                "finding_refs": [str(finding_id)],
                "observation_refs": [str(self.state.claude_vt_observation_id)],
                "task_id": str(self.state.loader_task_id),
                "expected_working_state_version": state["working_state_version"],
                "expected_working_state_sha256": state["working_state_sha256"],
            }
            if anchor == NOTE_A_ANCHOR:
                # B5a: an IOC table is a reference, never prose; its rows are
                # derived from the canonical imported observation.
                assert self.state.imported_observation_id is not None
                body["content_blocks"] = [
                    {
                        "block_type": "ioc_table",
                        "observation_ids": [str(self.state.imported_observation_id)],
                        "title": note_ioc_table_title(1),
                    }
                ]
            response = _json(
                await self._human(
                    client,
                    "POST",
                    f"/api/investigations/{self.state.investigation_id}/note-proposals",
                    json=body,
                    headers={"Idempotency-Key": key},
                )
            )
            proposal = response["proposal"]
            assert response["materialised"] is True, response
            assert proposal["status"] == "proposed", proposal
            assert proposal["proposed_text"] == text, proposal
            assert proposal["anchor"] == anchor
            assert proposal["bridge_invocation_id"] == str(invocation_id)
            if anchor == NOTE_A_ANCHOR:
                assert proposal["content_blocks"] == body["content_blocks"], proposal
            proposal_ids.append(UUID(proposal["id"]))
        self.state.proposal_a_id, self.state.proposal_b_id = proposal_ids
        assert NOTE_A_ANCHOR != NOTE_B_ANCHOR
        self.journal.record(
            "step_16_bridge_drafts_two_notes",
            f"the deduplicated anchor {DEDUPLICATED_ANCHOR} was refused "
            f"invalid_note_anchor; two Bridge notes materialised at {NOTE_A_ANCHOR} "
            f"and {NOTE_B_ANCHOR}",
        )

    async def step_17_human_accepts_both_drafts(self, client: Any) -> None:
        proposal_a = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/note-proposals/"
                f"{self.state.proposal_a_id}",
            )
        )
        accepted_a = await self._accept_proposal(
            client,
            proposal_a["proposal"],
            base_version=None,
            base_sha256=None,
            key="m24-acceptance-accept-note-a",
        )
        assert accepted_a["opinion_version"] == 1, accepted_a
        assert accepted_a["opinion_sha256"]
        self.state.opinion_v1_sha = accepted_a["opinion_sha256"]
        async with self.uow_factory() as uow:
            accepted_v1 = await uow.analyst_opinions.get(
                UUID(accepted_a["opinion_id"]), version=accepted_a["opinion_version"]
            )
        assert accepted_v1 is not None
        note_a = next(item for item in accepted_v1.observations if item.anchor == NOTE_A_ANCHOR)
        assert [block.to_payload() for block in note_a.content_blocks] == [
            {
                "block_type": "ioc_table",
                "observation_ids": [str(self.state.imported_observation_id)],
                "title": note_ioc_table_title(1),
            }
        ], note_a.content_blocks
        observations_projection = next(
            block
            for block in accepted_v1.blocks
            if block.block_type is OpinionBlockType.ANALYST_OBSERVATIONS
        )
        assert json.loads(observations_projection.text)["schema"] == ("analyst-observations.v2")
        # The second draft was pinned to the empty opinion: accepting it after A
        # must be refused as stale, never silently rebased.  The human asks
        # Bridge for the same note against the new head and accepts that one.
        stale_b = await self._human(
            client,
            "POST",
            f"/api/investigations/{self.state.investigation_id}/note-proposals/"
            f"{self.state.proposal_b_id}/accept",
            json={
                "expected_status_version": _json(
                    await self._human(
                        client,
                        "GET",
                        f"/api/investigations/{self.state.investigation_id}/note-proposals/"
                        f"{self.state.proposal_b_id}",
                    )
                )["proposal"]["status_version"],
                "expected_base_version": None,
                "expected_base_sha256": None,
                "reason": "Tentative d'acceptation d'une proposition pinnee obsolete",
            },
            headers={"Idempotency-Key": "m24-acceptance-accept-note-b-stale"},
        )
        assert stale_b.status_code == 409, stale_b.text
        assert stale_b.json()["detail"]["code"] == "stale_note_proposal"
        state = await self._operator_state(client)
        invocation_id = uuid4()
        draft = note_draft_response(
            block_type="key_findings",
            anchor=NOTE_B_ANCHOR,
            text=NOTE_B_TEXT,
            bridge_invocation_id=invocation_id,
        )
        self.scenario.bridge.enqueue(
            bridge_submission(draft["output_text"], bridge_invocation_id=invocation_id)
        )
        response_b = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/note-proposals",
                json={
                    "anchor": NOTE_B_ANCHOR,
                    "target_block_type": "section",
                    "bridge_block_type": "key_findings",
                    "consultation_type": "draft_text",
                    "finding_refs": [str(self.state.imported_finding_id)],
                    "observation_refs": [str(self.state.claude_vt_observation_id)],
                    "task_id": str(self.state.loader_task_id),
                    "base_opinion_version": accepted_a["opinion_version"],
                    "base_opinion_sha256": accepted_a["opinion_sha256"],
                    "expected_working_state_version": state["working_state_version"],
                    "expected_working_state_sha256": state["working_state_sha256"],
                },
                headers={"Idempotency-Key": "m24-acceptance-note-b-repinned"},
            )
        )
        assert response_b["materialised"] is True, response_b
        accepted_b = await self._accept_proposal(
            client,
            response_b["proposal"],
            base_version=accepted_a["opinion_version"],
            base_sha256=accepted_a["opinion_sha256"],
            key="m24-acceptance-accept-note-b-repinned",
        )
        assert accepted_b["opinion_version"] == 2, accepted_b
        self.state.opinion_v2_sha = accepted_b["opinion_sha256"]
        draft = await self._opinion(client)
        assert draft["version"] == 2
        notes = {note["anchor"]: note["text"] for note in draft["observations"]}
        assert notes[NOTE_A_ANCHOR] == NOTE_A_TEXT
        assert notes[NOTE_B_ANCHOR] == NOTE_B_TEXT
        async with self.uow_factory() as uow:
            decisions = await uow.analyst_decisions.list_for_investigation(
                self.state.investigation_id
            )
        approvals = [
            decision
            for decision in decisions
            if decision.decision_type is AnalystDecisionType.NOTE_APPROVE
        ]
        assert len(approvals) == 2
        self.journal.record(
            "step_17_human_accepts_both_drafts",
            f"human accepted note A as v1 {self.state.opinion_v1_sha[:12]}; a stale second "
            f"draft was refused 409, re-pinned on v1 and accepted as v2 "
            f"{self.state.opinion_v2_sha[:12]}",
        )

    async def step_18_human_requests_correction(self, client: Any) -> None:
        head = await self._opinion(client)
        assert head["version"] == 2
        invocation_id = uuid4()
        draft = note_draft_response(
            block_type="key_findings",
            anchor=NOTE_A_ANCHOR,
            text=NOTE_A_REVISED,
            bridge_invocation_id=invocation_id,
        )
        self.scenario.bridge.enqueue(
            bridge_submission(draft["output_text"], bridge_invocation_id=invocation_id)
        )
        state = await self._operator_state(client)
        response = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/note-proposals",
                json={
                    "anchor": NOTE_A_ANCHOR,
                    "target_block_type": "lead",
                    "bridge_block_type": "key_findings",
                    "consultation_type": "draft_text",
                    "finding_refs": [str(self.state.imported_finding_id)],
                    "observation_refs": [str(self.state.claude_vt_observation_id)],
                    "task_id": str(self.state.loader_task_id),
                    "question": (
                        "Corrige la note existante : precise qu'une verification "
                        "independante a ete menee."
                    ),
                    "base_opinion_version": head["version"],
                    "base_opinion_sha256": head["content_sha256"],
                    "expected_working_state_version": state["working_state_version"],
                    "expected_working_state_sha256": state["working_state_sha256"],
                },
                headers={"Idempotency-Key": "m24-acceptance-correction-a"},
            )
        )
        proposal = response["proposal"]
        assert response["materialised"] is True, response
        assert proposal["proposed_text"] == NOTE_A_REVISED, proposal
        assert proposal["base_opinion_version"] == 2
        assert proposal["base_opinion_sha256"] == head["content_sha256"]
        self.state.correction_proposal_id = UUID(proposal["id"])
        self.journal.record(
            "step_18_human_requests_correction",
            f"correction requested at {NOTE_A_ANCHOR} on opinion v2 {head['content_sha256'][:12]}",
        )

    async def step_19_human_accepts_correction(self, client: Any) -> None:
        head = await self._opinion(client)
        assert head["version"] == 2
        proposal = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/note-proposals/"
                f"{self.state.correction_proposal_id}",
            )
        )
        accepted = await self._accept_proposal(
            client,
            proposal["proposal"],
            base_version=head["version"],
            base_sha256=head["content_sha256"],
            key="m24-acceptance-accept-correction",
        )
        assert accepted["opinion_version"] == 3, accepted
        self.state.opinion_v3_sha = accepted["opinion_sha256"]
        draft = await self._opinion(client)
        assert draft["version"] == 3
        notes = {note["anchor"]: note["text"] for note in draft["observations"]}
        assert notes[NOTE_A_ANCHOR] == NOTE_A_REVISED
        assert notes[NOTE_B_ANCHOR] == NOTE_B_TEXT
        self.state.opinion_id = UUID(draft["id"])
        self.journal.record(
            "step_19_human_accepts_correction",
            f"correction accepted as opinion v3 {self.state.opinion_v3_sha[:12]}, two notes kept",
        )

    async def step_20_real_typst_pdf(self, client: Any) -> None:
        from cti_app.domain.production_synthesis import production_synthesis_from_json
        from cti_app.domain.publication_document import (
            publication_document_article_v5_body,
            publication_document_from_json,
            serialize_publication_document,
        )

        # The preview re-reads the exact canonical SYNTHESIS V1 and the V5
        # publication body; assert those pins before asking the renderer so a
        # fixture drift cannot be mistaken for a Typst failure.
        raw_synthesis = await self.scenario.artifact_store.read_json(
            self.state.seeded.synthesis.canonical_blob_id
        )
        parsed_synthesis = production_synthesis_from_json(raw_synthesis)
        async with self.uow_factory() as uow:
            preview_opinion = await uow.analyst_opinions.get(self.state.opinion_id)
            preview_base = await uow.production_artifacts.get(
                preview_opinion.base_publication_artifact_id
            )
            preview_synthesis = await uow.production_artifacts.get(
                preview_opinion.synthesis_artifact_id
            )
            preview_current = await uow.production_artifacts.get_current_for_revision(
                self.state.seeded.run.id, "publication"
            )
        assert preview_opinion is not None
        assert preview_base is not None and preview_synthesis is not None
        assert preview_current is not None and preview_current.id == preview_base.id
        base_bytes = await self.scenario.artifact_store.read_bytes(preview_base.canonical_blob_id)
        document = publication_document_from_json(json.loads(base_bytes))
        body = publication_document_article_v5_body(document)
        assert body is not None
        assert (
            self.scenario.artifact_store.canonical_json_bytes(
                serialize_publication_document(document)
            )
            == base_bytes
        )
        base_sha = hashlib.sha256(base_bytes).hexdigest()
        assert base_sha == preview_opinion.base_publication_sha256
        synthesis_bytes = await self.scenario.artifact_store.read_bytes(
            preview_synthesis.canonical_blob_id
        )
        assert hashlib.sha256(synthesis_bytes).hexdigest() == preview_opinion.synthesis_sha256
        assert parsed_synthesis.subject_id == body.subject_id
        assert parsed_synthesis.title == body.title, (parsed_synthesis.title, body.title)
        assert parsed_synthesis.publication_language == body.publication_language
        draft = await self._opinion(client)
        assert draft["version"] == 3
        response = await self._human(
            client,
            "GET",
            f"/api/opinions/{draft['id']}/preview.pdf",
            params={
                "version": draft["version"],
                "opinion_sha256": draft["content_sha256"],
                "base_publication_sha256": draft["base_publication_sha256"],
            },
        )
        assert response.status_code == 200, response.text[:2000]
        assert response.headers["content-type"].startswith("application/pdf")
        assert response.headers["content-disposition"].startswith("inline;"), response.headers
        assert response.content.startswith(b"%PDF-1.7"), response.content[:16]
        pdf_sha = response.headers["x-pdf-sha256"]
        assert len(pdf_sha) == 64
        lineage = json.loads(response.headers["x-pdf-lineage"])
        assert lineage["nature"] == "opinion_draft_preview"
        assert lineage["opinion"]["sha256"] == draft["content_sha256"]
        assert lineage["compiler"]["version"] == "0.15.1"
        assert lineage["template"]["sha256"]
        self.state.pdf_path = self.tmp_path / f"opinion-{draft['id']}-v3.pdf"
        self.state.pdf_path.write_bytes(response.content)
        async with self.uow_factory() as uow:
            head = await uow.analyst_opinions.get(self.state.opinion_id)
        assert head is not None and head.version == 3
        self.journal.record(
            "step_20_real_typst_pdf",
            f"real Typst preview.pdf {len(response.content)} bytes sha={pdf_sha[:12]}",
        )

    async def step_21_notes_at_two_anchors(self, client: Any) -> None:
        assert self.state.pdf_path is not None
        extracted = await asyncio.to_thread(
            subprocess.run,
            ["pdftotext", "-layout", str(self.state.pdf_path), "-"],
            check=True,
            capture_output=True,
            text=True,
        )
        normalized = " ".join(extracted.stdout.split())
        assert "NOTE-ALPHA-ANCRAGE-REVISE" in normalized, normalized[:2000]
        assert "NOTE-BETA-ANCRAGE" in normalized, normalized[:2000]
        # Placement, not mere presence: each note must follow exactly the
        # paragraph it anchors on, before the next article paragraph.
        lead_position = normalized.index("Example finding.")
        note_a_position = normalized.index("NOTE-ALPHA-ANCRAGE-REVISE")
        anchor_position = normalized.index(ANALYSIS_SECTION_PARAGRAPH)
        note_b_position = normalized.index("NOTE-BETA-ANCRAGE")
        tail_position = normalized.index(TAIL_SECTION_PARAGRAPH)
        assert (
            lead_position < note_a_position < anchor_position < note_b_position < tail_position
        ), (
            lead_position,
            note_a_position,
            anchor_position,
            note_b_position,
            tail_position,
            normalized[:3000],
        )
        assert normalized.count("NOTE-BETA-ANCRAGE") == 1
        draft = await self._opinion(client)
        anchors = {note["anchor"] for note in draft["observations"]}
        assert anchors == {NOTE_A_ANCHOR, NOTE_B_ANCHOR}
        async with self.uow_factory() as uow:
            head = await uow.analyst_opinions.get(self.state.opinion_id)
            findings = await uow.investigation_findings.list_for_investigation(
                self.state.investigation_id
            )
            observations = await uow.evidence_observations.list_for_investigation(
                self.state.investigation_id
            )
        assert head is not None
        violations = verify_opinion_citations(head, findings, observations)
        assert violations == (), violations
        self.journal.record(
            "step_21_notes_at_two_anchors",
            "each note is printed immediately after its anchor paragraph in the real PDF; "
            "every citation resolves canonically",
        )

    async def step_22_interrupt_claude_session(self, client: Any) -> None:
        before = await self._artifact_counts()
        interrupted = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/agent-sessions/"
                f"{self.state.claude_session_id}/interrupt",
                json={"reason": "Interruption humaine de fin de scenario"},
            )
        )
        assert interrupted["state"] in {"stopped", "interrupt_requested"}, interrupted
        host = self.scenario.session_runtime.host_sessions
        assert host is not None
        status = await self._wait_host_state(host, {"stopped", "lost"}, deadline_seconds=20)
        assert status.state in {"stopped", "lost"}, status
        after = await self._artifact_counts()
        assert after == before, (before, after)
        self.journal.record(
            "step_22_interrupt_claude_session",
            f"session interrupted (host {status.state}); artifact counts unchanged",
        )

    async def step_23_resume_without_loss(self, client: Any) -> None:
        before = await self._artifact_counts()
        resumed = _json(
            await self._human(
                client,
                "POST",
                f"/api/investigations/{self.state.investigation_id}/agent-sessions/"
                f"{self.state.claude_session_id}/resume",
            )
        )
        assert resumed["state"] == "ready", resumed
        host = self.scenario.session_runtime.host_sessions
        assert host is not None
        status = await self._wait_host_state(host, {"running", "ready", "awaiting_instruction"})
        assert len(status.runs) >= 2, status
        after = await self._artifact_counts()
        assert after == before, (before, after)
        self.journal.record(
            "step_23_resume_without_loss",
            f"session resumed as a new run ({len(status.runs)} runs), no artifact lost",
        )

    async def step_24_final_audit(self, client: Any) -> None:
        timeline = _json(
            await self._human(
                client,
                "GET",
                f"/api/investigations/{self.state.investigation_id}/timeline",
                params={"limit": 200},
            )
        )
        items = timeline["items"]
        assert items
        types = {item["item_type"] for item in items}
        assert "bridge_consultation" in types, types
        assert any(item["evidence"] is True for item in items)
        assert all(
            item["evidence"] is False
            for item in items
            if item["item_type"] in {"bridge_consultation", "plan", "handoff", "contribution"}
        )
        async with self.uow_factory() as uow:
            findings = await uow.investigation_findings.list_for_investigation(
                self.state.investigation_id
            )
            observations = await uow.evidence_observations.list_for_investigation(
                self.state.investigation_id
            )
            samples = await uow.samples.list_by_subject_and_sha256(
                self.state.seeded.subject.id, self.scenario.material.available_hashes[0]
            )
            envelope = await uow.investigation_envelopes.get(self.state.investigation_id)
            investigation = await uow.analyst_investigations.get(self.state.investigation_id)
        by_id = {observation.id: observation for observation in observations}
        for finding in findings:
            for reference in finding.observation_refs:
                cited = by_id.get(reference)
                assert cited is not None
                assert not is_advisory_observation(cited), (finding.id, cited.id)
        assert all(sample.state is not SampleState.VALIDATED for sample in samples)
        assert envelope is not None
        assert envelope.approval_actor_id == "operator:static-token"
        assert investigation is not None
        assert investigation.prose_authority is ProseAuthority.BRIDGE_ONLY
        assert self.scenario.virus_total.calls["file_report"] >= 5
        self.journal.record(
            "step_24_final_audit",
            "timeline evidence flags, no advisory citation, no G3 auto-validation, "
            "human G2 and bridge_only preserved",
        )

    # --------------------------------------------------------- audit helpers

    async def _artifact_counts(self) -> dict[str, int]:
        async with self.uow_factory() as uow:
            observations = await uow.evidence_observations.list_for_investigation(
                self.state.investigation_id
            )
            findings = await uow.investigation_findings.list_for_investigation(
                self.state.investigation_id
            )
            handoffs = await uow.investigation_handoffs.list_for_investigation(
                self.state.investigation_id
            )
            contributions = await uow.work_contributions.list_for_investigation(
                self.state.investigation_id
            )
            proposals = await uow.investigation_note_proposals.list_for_investigation(
                self.state.investigation_id
            )
            tasks = await uow.investigation_tasks.list_for_investigation(
                self.state.investigation_id
            )
            head = await uow.analyst_opinions.get(self.state.opinion_id)
        return {
            "observations": len(observations),
            "findings": len(findings),
            "handoffs": len(handoffs),
            "contributions": len(contributions),
            "proposals": len(proposals),
            "tasks": len(tasks),
            "opinion_version": head.version if head is not None else 0,
        }

    async def _wait_host_state(
        self, host: Any, expected: set[str], *, deadline_seconds: float = 15.0
    ) -> Any:
        deadline = time.monotonic() + deadline_seconds
        last: Any = None
        while time.monotonic() < deadline:
            last = await host.get_session_status(self.state.claude_session_id)
            if last.state in expected:
                return last
            await asyncio.sleep(0.05)
        raise AssertionError(f"host session never reached {expected}: {last}")


# --------------------------------------------------------------- fixtures


@pytest.fixture
def acceptance_profiles(monkeypatch: pytest.MonkeyPatch) -> None:
    install_fake_cli_profiles(monkeypatch)


@pytest.fixture
def acceptance_agent(acceptance_profiles: None) -> Iterator[RunningScenarioAgent]:
    with running_scenario_agent() as running:
        yield running


@pytest.fixture
async def acceptance_scenario(
    acceptance_agent: RunningScenarioAgent,
    uow_factory: Any,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[ScenarioApplication]:
    application = await build_scenario_application(
        monkeypatch=monkeypatch,
        uow_factory=uow_factory,
        agent=acceptance_agent,
        material=ScenarioMaterial.synthetic(),
        blob_root=tmp_path / "blobs",
    )
    try:
        yield application
    finally:
        await application.aclose()


# ------------------------------------------------------------------- test


async def test_full_24_step_scenario(
    acceptance_scenario: ScenarioApplication,
    acceptance_agent: RunningScenarioAgent,
    uow_factory: Any,
    tmp_path: Path,
) -> None:
    journey = ScenarioJourney(
        scenario=acceptance_scenario,
        agent=acceptance_agent,
        uow_factory=uow_factory,
        tmp_path=tmp_path,
    )
    journey.state.seeded = await seed_article(
        uow_factory=uow_factory,
        artifact_store=acceptance_scenario.artifact_store,
        material=acceptance_scenario.material,
    )
    steps = (
        journey.step_01_start_investigation_via_api,
        journey.step_02_workspace_created_by_real_sidecar,
        journey.step_03_ioc_inventory,
        journey.step_04_g2_envelope_proposed_and_approved,
        journey.step_05_fake_vt_collection,
        journey.step_06_controlled_sample_materialization,
        journey.step_07_bridge_plan_accepted,
        journey.step_08_task_assigned_and_codex_session,
        journey.step_09_codex_inspects_sample_via_broker,
        journey.step_10_codex_consults_bridge,
        journey.step_11_human_local_markdown_note,
        journey.step_12_note_visible_via_api,
        journey.step_13_transfer_to_claude_code,
        journey.step_14_claude_deepens_verification,
        journey.step_15_import_observation_and_finding,
        journey.step_16_bridge_drafts_two_notes,
        journey.step_17_human_accepts_both_drafts,
        journey.step_18_human_requests_correction,
        journey.step_19_human_accepts_correction,
        journey.step_20_real_typst_pdf,
        journey.step_21_notes_at_two_anchors,
        journey.step_22_interrupt_claude_session,
        journey.step_23_resume_without_loss,
        journey.step_24_final_audit,
    )
    reached = 0
    try:
        async with acceptance_scenario.client() as client:
            for step in steps:
                await step(client)
                reached += 1
    except BaseException:
        print(f"\n=== M24 acceptance journal (stopped at step {reached + 1}) ===")
        print(journey.journal.render())
        raise
    print("\n=== M24 acceptance journal ===")
    print(journey.journal.render())
    assert reached == len(steps)
