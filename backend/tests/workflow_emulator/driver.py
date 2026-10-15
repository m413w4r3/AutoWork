"""Explicit driver for real application services and official ASGI routes."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import replace
from datetime import date
from hashlib import sha256
from pathlib import Path
from typing import Any, TypeVar, cast
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from cti_app.api.editions import router as editions_router
from cti_app.api.publication import router as publication_router
from cti_app.api.selection import selection_router
from cti_app.application.discovery.contracts import (
    discover_parameters_from_run,
    discovery_job_idempotency_key,
)
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.fusion import FusionService
from cti_app.application.discovery.jobs import DISCOVERY_JOB_KIND
from cti_app.application.discovery.service import DiscoveryService
from cti_app.application.edition_composition import EditionCompositionService
from cti_app.application.edition_publication import (
    EditionAssemblyService,
    EditionPublicationService,
)
from cti_app.application.edition_review import EditionReviewService
from cti_app.application.editions import EditionService
from cti_app.application.identity import LocalIdentityProvider
from cti_app.application.jobs import JobExecutor, JobService, create_job_registry
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.selection import SelectionService
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import DiscoveryBatch
from cti_app.domain.discovery_cumulative import DiscoveryInputMode
from cti_app.domain.edition_composition import (
    CompositionDisposition,
    EditionCompositionEntry,
)
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.jobs import JobStatus
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_review import PublicationDecision
from tests.discovery_support import make_discovery_run_for_edition
from tests.integration.edition_codes import reserve_edition_code
from tests.integration.production.support import ProductionScenario

from . import invariants
from .fixtures import persist_discovery_fixture
from .providers import (
    CaptureBundle,
    DeterministicFusionPlanner,
    capture_bundle,
    discovery_report_for_candidates,
    script_discovery_report,
)
from .trace import TraceEvent, TraceVia, TraceWriter, short_identity

ScenarioFactory = Callable[[Mapping[str, Mapping[str, object]]], ProductionScenario]
T = TypeVar("T")
AfterAction = Callable[[T], Awaitable[tuple[str, ...] | None]]


def build_emulator_app(
    uow_factory: UnitOfWorkFactory, artifact_store: ProductionArtifactStore
) -> FastAPI:
    """Build the official routers with real PostgreSQL-backed services."""
    application = FastAPI(title="Workflow Emulator")
    application.include_router(editions_router)
    application.include_router(publication_router)
    application.include_router(selection_router)
    application.state.uow_factory = uow_factory
    application.state.edition_service = EditionService(uow_factory)
    application.state.edition_composition_service = EditionCompositionService(uow_factory)
    application.state.edition_review_service = EditionReviewService(uow_factory)
    application.state.edition_publication_service = EditionPublicationService(
        uow_factory, artifact_store
    )
    application.state.production_artifact_store = artifact_store
    application.state.selection_service = SelectionService(uow_factory)
    application.state.identity_provider = LocalIdentityProvider("workflow-analyst")
    return application


class WorkflowDriver:
    def __init__(
        self,
        *,
        uow_factory: UnitOfWorkFactory,
        artifact_store: ProductionArtifactStore,
        production_scenario_factory: ScenarioFactory,
        trace_root: Path,
    ) -> None:
        self.uow_factory = uow_factory
        self.artifact_store = artifact_store
        self.production_scenario_factory = production_scenario_factory
        self.app = build_emulator_app(uow_factory, artifact_store)
        self.client = AsyncClient(
            transport=ASGITransport(app=self.app), base_url="http://workflow-emulator"
        )
        self.scenario = "workflow"
        self.trace = TraceWriter(self.scenario, trace_root)
        self.trace_root = trace_root
        self.edition_id: UUID | None = None
        self.discovery_batches: dict[str, DiscoveryBatch] = {}
        self.discovery_bundles: dict[str, CaptureBundle] = {}
        self.production_scenarios: dict[str, ProductionScenario] = {}
        self.production_subjects: dict[str, UUID] = {}
        self.production_run_ids: dict[str, UUID] = {}
        self.production_batch_ids: dict[str, UUID] = {}
        self.accepted_manifest: Any | None = None

    async def __aenter__(self) -> WorkflowDriver:
        return self

    async def __aexit__(self, *args: object) -> None:
        del args
        await self.client.aclose()

    def begin(self, scenario: str) -> None:
        self.scenario = scenario
        self.trace = TraceWriter(scenario, self.trace_root)

    def set_artifact_store(self, artifact_store: ProductionArtifactStore) -> None:
        self.artifact_store = artifact_store
        self.app.state.production_artifact_store = artifact_store
        self.app.state.edition_publication_service = EditionPublicationService(
            self.uow_factory, artifact_store
        )

    def _edition_id(self) -> UUID:
        if self.edition_id is None:
            raise AssertionError(f"{self.scenario} / driver: edition has not been created")
        return self.edition_id

    async def _perform(
        self,
        step: str,
        action: Callable[[], Awaitable[T]],
        *,
        via: TraceVia,
        action_name: str,
        identities: Mapping[str, object] | None = None,
        assertions: Sequence[str] = (),
        after: AfterAction[T] | None = None,
    ) -> T:
        verified = list(assertions)
        short = {key: short_identity(value) for key, value in (identities or {}).items()}
        try:
            result = await action()
            if after is not None:
                verified.extend(await after(result) or ())
        except Exception as exc:
            self.trace.append(
                TraceEvent(
                    scenario=self.scenario,
                    step=step,
                    action=action_name,
                    via=via,
                    status="failed",
                    identities=short,
                    assertions=tuple(verified),
                )
            )
            message = str(exc)
            prefix = f"{self.scenario} / {step}:"
            if message.startswith(prefix):
                raise
            raise AssertionError(f"{prefix} {message}") from exc
        self.trace.append(
            TraceEvent(
                scenario=self.scenario,
                step=step,
                action=action_name,
                via=via,
                status="ok",
                identities=short,
                assertions=tuple(verified),
            )
        )
        return result

    async def _edition(self) -> Edition:
        edition_id = self._edition_id()
        async with self.uow_factory() as uow:
            edition = await uow.editions.get(edition_id)
        if edition is None:
            raise AssertionError(f"{self.scenario} / database: edition is missing")
        return edition

    async def create_edition(self, *, tlp: TLP = TLP.AMBER) -> UUID:
        country_code = reserve_edition_code()
        payload = {
            "country": f"Workflow emulator {country_code}",
            "country_code": country_code,
            "period_start": date(2026, 8, 1).isoformat(),
            "period_end": date(2026, 8, 31).isoformat(),
            "tlp": tlp.value,
            "languages": ["fr"],
        }

        async def action() -> UUID:
            response = await self.client.post("/api/editions", json=payload)
            if response.status_code != 201:
                raise AssertionError(f"edition route returned HTTP {response.status_code}")
            return UUID(response.json()["id"])

        async def verify(edition_id: UUID) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                persisted = await uow.editions.get(edition_id)
            invariants.require(
                persisted is not None and persisted.state.value == "open",
                self.scenario,
                "create_edition",
                "edition was not persisted as open",
            )
            assert persisted is not None
            self.edition_id = edition_id
            return ("edition-read-back-from-postgresql", "edition-open")

        return await self._perform(
            "create_edition",
            action,
            via="http",
            action_name="POST /api/editions",
            after=verify,
        )

    async def run_discovery(
        self,
        batch_label: str,
        subject_labels: Sequence[str],
        *,
        bundle_overrides: Mapping[str, CaptureBundle] | None = None,
    ) -> DiscoveryBatch:
        edition = await self._edition()
        before = await invariants.production_state(self.uow_factory, edition.id)
        overrides = dict(bundle_overrides or {})
        unknown_overrides = set(overrides).difference(subject_labels)
        if unknown_overrides:
            raise ValueError(f"Unknown Discovery bundle overrides: {sorted(unknown_overrides)}")
        for label in subject_labels:
            self.discovery_bundles[label] = (
                overrides[label] if label in overrides else capture_bundle(label)
            )

        async def action() -> DiscoveryBatch:
            batch = await persist_discovery_fixture(
                self.uow_factory,
                edition,
                batch_label=batch_label,
                subject_labels=subject_labels,
                bundles=self.discovery_bundles,
            )
            self.discovery_batches[batch_label] = batch
            return batch

        async def verify(batch: DiscoveryBatch) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                persisted = await uow.discovery_batches.get(batch.id)
                candidates = await uow.discovery_candidates.list_for_batch(batch.id)
            invariants.require(
                persisted is not None and len(candidates) == len(subject_labels),
                self.scenario,
                f"{batch_label}.discovery",
                "fixture Discovery batch or its candidate rows are missing",
            )
            unchanged = await invariants.assert_production_state_unchanged(
                self.uow_factory,
                edition.id,
                before,
                scenario=self.scenario,
                step=f"{batch_label}.discovery",
            )
            return ("discovery-batch-and-candidates-read-from-postgresql", *unchanged)

        return await self._perform(
            f"{batch_label}.discovery",
            action,
            via="fixture",
            action_name="persist_versioned_discovery_fixture",
            identities={"edition": edition.id, "batch": batch_label},
            after=verify,
        )

    async def run_discovery_with_scripted_model(
        self, batch_label: str, subject_labels: Sequence[str]
    ) -> DiscoveryBatch:
        """Run the real Discovery service/parser with one local scripted model response."""
        edition = await self._edition()
        before = await invariants.production_state(self.uow_factory, edition.id)
        for label in subject_labels:
            self.discovery_bundles[label] = capture_bundle(label)
        report = discovery_report_for_candidates(subject_labels, self.discovery_bundles)
        model_scenario = self.production_scenario_factory(
            self.discovery_bundles[subject_labels[0]].sources
        )
        script_discovery_report(model_scenario.model, report)

        async def action() -> DiscoveryBatch:
            run = await make_discovery_run_for_edition(
                self.uow_factory,
                edition,
                source_profile="workflow-emulator",
                complementary_axis=f"workflow-{batch_label}",
                actor_id="workflow-analyst",
            )
            parameters = discover_parameters_from_run(run)
            discovery = DiscoveryService(
                self.uow_factory,
                model_scenario.model,
                archive=model_scenario.model,
            )
            registry = create_job_registry(
                model_gateway=model_scenario.model,
                discovery_service=discovery,
            )
            jobs = JobService(self.uow_factory, registry)
            executor = JobExecutor(self.uow_factory, registry)
            job = await jobs.submit(
                kind=DISCOVERY_JOB_KIND,
                aggregate_type="discovery_run",
                aggregate_id=run.id,
                idempotency_key=discovery_job_idempotency_key(run.id),
                correlation_id=f"workflow-discovery-{batch_label}-{uuid4()}",
                input_parameters=parameters.model_dump(mode="json"),
                max_attempts=1,
            )
            completed = await executor.execute(job.id)
            if completed.status is not JobStatus.SUCCEEDED:
                raise AssertionError(
                    f"Discovery job ended {completed.status.value}: "
                    f"{completed.error_code} {completed.error_message}"
                )
            batches = await discovery.list_batches(edition.id)
            matching = [item for item in batches if item.discovery_run_id == run.id]
            if len(matching) != 1:
                raise AssertionError(
                    f"expected one persisted Discovery batch for run {run.id}, "
                    f"found {len(matching)}"
                )
            batch = matching[0]
            self.discovery_batches[batch_label] = batch
            return batch

        async def verify(batch: DiscoveryBatch) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                persisted = await uow.discovery_batches.get(batch.id)
                candidates = await uow.discovery_candidates.list_for_batch(batch.id)
                model_run = await uow.model_runs.get(batch.discovery_model_run_id)
            invariants.require(
                persisted is not None and len(candidates) == len(subject_labels),
                self.scenario,
                f"{batch_label}.discovery",
                "real Discovery batch or candidate rows are missing from PostgreSQL",
            )
            invariants.require(
                persisted is not None
                and persisted.parser_version == "chatgpt-markdown-v3-structured"
                and persisted.report_sha256 == sha256(report.encode("utf-8")).hexdigest(),
                self.scenario,
                f"{batch_label}.discovery",
                "persisted Discovery parser version or report hash differs from the script",
            )
            invariants.require(
                model_run is not None
                and model_run.prompt_template_id == "monthly-cti-discovery"
                and model_run.status.value == "succeeded"
                and bool(model_run.output_references),
                self.scenario,
                f"{batch_label}.discovery",
                "Discovery model run is absent, unsuccessful, or does not archive its response",
            )
            assert model_run is not None
            archived = await model_scenario.model.read_output(
                model_run.output_references[0], max_bytes=10_000_000
            )
            invariants.require(
                archived.decode("utf-8") == report,
                self.scenario,
                f"{batch_label}.discovery",
                "model-run archive does not contain the versioned scripted Discovery response",
            )
            expected_titles = tuple(
                f"ExampleMalware-{label} campaign {label}" for label in subject_labels
            )
            invariants.require(
                tuple(candidate.title for candidate in candidates) == expected_titles
                and all(len(candidate.evidence.sources) == 2 for candidate in candidates),
                self.scenario,
                f"{batch_label}.discovery",
                "the real Discovery parser did not persist the expected candidates and sources",
            )
            unchanged = await invariants.assert_production_state_unchanged(
                self.uow_factory,
                edition.id,
                before,
                scenario=self.scenario,
                step=f"{batch_label}.discovery",
            )
            return (
                "discovery-service-job-and-parser-ran",
                "scripted-response-read-from-model-run-archive",
                "candidate-rows-read-from-postgresql",
                *unchanged,
            )

        return await self._perform(
            f"{batch_label}.discovery",
            action,
            via="service",
            action_name="DiscoveryService.discover_edition via JobExecutor",
            identities={"edition": edition.id, "batch": batch_label},
            assertions=(
                "local-versioned-scripted-model-response",
                "real-discovery-parser-and-persistence",
            ),
            after=verify,
        )

    async def record_seed(self, subjects: Mapping[str, UUID], *, mechanism: str) -> None:
        """Record fixture-created READY runs after confirming their canonical DB state."""
        edition_id = self._edition_id()
        identities = {label: short_identity(subject_id) for label, subject_id in subjects.items()}
        async with self.uow_factory() as uow:
            runs = await uow.production_runs.list_for_edition(edition_id)
            for label, subject_id in subjects.items():
                run = next((item for item in runs if item.subject_id == subject_id), None)
                invariants.require(
                    run is not None and run.status.value == "ready",
                    self.scenario,
                    "seed_ready_runs",
                    f"subject {label} has no READY production run after fixture seeding",
                )
                assert run is not None
                artifact = await uow.production_artifacts.get_current(
                    run.id, ProductionArtifactStage.PUBLICATION.value
                )
                invariants.require(
                    artifact is not None and artifact.status is ProductionArtifactStatus.VERIFIED,
                    self.scenario,
                    "seed_ready_runs",
                    f"subject {label} has no VERIFIED PUBLICATION artifact after fixture seeding",
                )
        self.trace.append(
            TraceEvent(
                scenario=self.scenario,
                step="seed_ready_runs",
                action="seed_ready_runs",
                via="fixture",
                status="ok",
                identities=identities,
                assertions=(mechanism, "ready-runs-and-verified-artifacts-read-from-postgresql"),
            )
        )

    async def apply_fusion(self, batch_label: str) -> Any:
        batch = self.discovery_batches[batch_label]
        edition_id = self._edition_id()

        async def action() -> Any:
            _, snapshot = await CumulativeDiscoveryService(
                self.uow_factory, planner=DeterministicFusionPlanner()
            ).reconcile_batch(
                batch,
                input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
                actor_id="workflow-analyst",
            )
            return snapshot

        async def verify(snapshot: Any) -> tuple[str, ...]:
            board = await FusionService(self.uow_factory).get_board(edition_id)
            async with self.uow_factory() as uow:
                persisted = await uow.discovery_snapshots.get_active(edition_id)
                merge_runs = await uow.discovery_merge_runs.list_for_edition(edition_id)
            invariants.require(
                persisted is not None
                and persisted.id == snapshot.id
                and board.snapshot_version == snapshot.version,
                self.scenario,
                f"{batch_label}.fusion",
                "Fusion snapshot is not the active PostgreSQL snapshot",
            )
            invariants.require(
                any(run.id == snapshot.merge_run_id for run in merge_runs),
                self.scenario,
                f"{batch_label}.fusion",
                "Fusion merge run is missing from PostgreSQL",
            )
            return ("fusion-snapshot-and-merge-run-read-from-postgresql",)

        return await self._perform(
            f"{batch_label}.fusion",
            action,
            via="service",
            action_name="CumulativeDiscoveryService.reconcile_batch",
            identities={"edition": edition_id, "batch": batch.id},
            after=verify,
        )

    async def select_subjects(
        self, batch_label: str, subject_labels: Sequence[str]
    ) -> dict[str, UUID]:
        batch = self.discovery_batches[batch_label]
        edition_id = self._edition_id()
        async with self.uow_factory() as uow:
            snapshot = await uow.discovery_snapshots.get_active(edition_id)
        if snapshot is None:
            raise AssertionError(f"{self.scenario} / {batch_label}.selection: no active snapshot")
        identities: dict[str, UUID] = {}
        for label in subject_labels:
            title = f"ExampleMalware-{label} campaign {label}"
            match = next(
                (item for item in snapshot.subjects if item.candidate.title == title), None
            )
            if match is None:
                raise AssertionError(
                    f"{self.scenario} / {batch_label}.selection: candidate {label} is missing"
                )
            identities[label] = match.subject_id
        key = f"workflow-select-{batch.id}-{uuid4()}"
        payload = {
            "snapshot_version": snapshot.version,
            "decisions": [
                {
                    "discovery_subject_id": str(discovery_subject_id),
                    "action": "select",
                    "expected_decision_id": None,
                }
                for discovery_subject_id in identities.values()
            ],
        }

        async def action() -> dict[str, UUID]:
            response = await self.client.post(
                f"/api/editions/{edition_id}/selection/decisions",
                headers={"Idempotency-Key": key},
                json=payload,
            )
            if response.status_code != 200:
                raise AssertionError(f"selection route returned HTTP {response.status_code}")
            async with self.uow_factory() as uow:
                origins = await uow.subject_discovery_origins.list_for_edition(edition_id)
            subject_by_origin = {
                origin.discovery_subject_id: origin.subject_id
                for origin in origins
                if origin.discovery_subject_id in identities.values()
            }
            return {
                label: subject_by_origin[discovery_subject_id]
                for label, discovery_subject_id in identities.items()
            }

        async def verify(selected_subjects: dict[str, UUID]) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                origins = await uow.subject_discovery_origins.list_for_edition(edition_id)
                subjects = await uow.subjects.list_for_edition(edition_id)
            subject_by_origin = {
                origin.discovery_subject_id: origin.subject_id
                for origin in origins
                if origin.discovery_subject_id in identities.values()
            }
            invariants.require(
                len(subject_by_origin) == len(identities),
                self.scenario,
                f"{batch_label}.selection",
                "selection origins are missing from PostgreSQL",
            )
            expected_subjects = {
                label: subject_by_origin[discovery_subject_id]
                for label, discovery_subject_id in identities.items()
            }
            subject_ids = {subject.id for subject in subjects}
            invariants.require(
                selected_subjects == expected_subjects
                and set(selected_subjects.values()).issubset(subject_ids),
                self.scenario,
                f"{batch_label}.selection",
                "selected subjects are missing from PostgreSQL",
            )
            return tuple(
                f"selected-subject-{label}-read-from-postgresql" for label in selected_subjects
            )

        return await self._perform(
            f"{batch_label}.selection",
            action,
            via="http",
            action_name="POST /selection/decisions",
            identities={"edition": edition_id, "batch": batch.id},
            after=verify,
        )

    async def start_production(
        self,
        label: str,
        subject_id: UUID,
        *,
        editorial_enrichment_response: str | Callable[[str], str] | None = None,
    ) -> UUID:
        edition_id = self._edition_id()
        bundle = self.discovery_bundles[label]
        subject: Subject | None
        async with self.uow_factory() as uow:
            subject = await uow.subjects.get(subject_id)
            edition = await uow.editions.get(edition_id)
        if subject is None or edition is None:
            raise AssertionError(f"{self.scenario} / {label}.production_start: selection is absent")
        production = self.production_scenario_factory(bundle.sources)
        production.edition = edition
        production.subject = subject
        production.model.script.references(bundle.references_response)
        for source_url, response in bundle.extraction_responses.items():
            production.model.script.q2(source_url=source_url, response=response)
        if editorial_enrichment_response is not None:
            production.model.script.editorial_enrichment(editorial_enrichment_response)
        self.production_scenarios[label] = production
        self.production_subjects[label] = subject_id

        async def action() -> UUID:
            run, job_id = await production.start_run_idempotently(
                idempotency_key=f"workflow-production-{self.scenario}-{label}-{uuid4()}"
            )
            self.production_run_ids[label] = run.id
            if production.batch_id is not None:
                self.production_batch_ids[label] = production.batch_id
            return job_id or run.id

        async def verify(job_or_run_id: UUID) -> tuple[str, ...]:
            del job_or_run_id
            async with self.uow_factory() as uow:
                run = await uow.production_runs.get(self.production_run_ids[label])
                jobs = await uow.jobs.list_for_aggregate("subject", subject_id)
            invariants.require(
                run is not None and run.status.value in {"queued", "running"},
                self.scenario,
                f"{label}.production_start",
                "production run was not queued in PostgreSQL",
            )
            invariants.require(
                any(job.id in production.runner.enqueued for job in jobs),
                self.scenario,
                f"{label}.production_start",
                "production job was not persisted and queued by the deterministic runner",
            )
            return ("production-run-and-job-read-from-postgresql",)

        return await self._perform(
            f"{label}.production_start",
            action,
            via="service",
            action_name="ProductionBatchService.start_next",
            identities={"edition": edition_id, "subject": subject_id},
            after=verify,
        )

    async def advance_jobs_until_terminal(self, label: str) -> UUID:
        production = self.production_scenarios[label]
        run_id = self.production_run_ids[label]

        async def action() -> UUID:
            run = await production.run_until_terminal()
            return run.id

        async def verify(completed_run_id: UUID) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                run = await uow.production_runs.get(completed_run_id)
                publication = await uow.production_artifacts.get_current(
                    completed_run_id, ProductionArtifactStage.PUBLICATION.value
                )
                last_call = production.model.calls[-1] if production.model.calls else None
                model_run = (
                    await uow.model_runs.get(last_call.model_run_id)
                    if last_call is not None and last_call.model_run_id is not None
                    else None
                )
            recent_calls = ",".join(
                f"{call.stage}/{call.request.prompt_template_id}"
                for call in production.model.calls[-3:]
            )
            diagnostic_code = (
                str((model_run.error_details or {}).get("diagnostic_code", "unknown"))
                if model_run is not None
                else "no-model-run"
            )
            model_details = (
                repr(model_run.error_details) if model_run is not None else "no-model-run"
            )
            invariants.require(
                run is not None and run.status.value == "ready",
                self.scenario,
                f"{label}.production_terminal",
                (
                    "production runner did not reach READY; "
                    f"status={run.status.value}, "
                    f"stage={run.current_stage.value}, "
                    f"error_code={run.error_code}, "
                    f"model_diagnostic={diagnostic_code}, details={model_details}, "
                    f"calls={recent_calls}"
                    if run is not None
                    else "production run is absent from PostgreSQL"
                ),
            )
            invariants.require(
                publication is not None and publication.status is ProductionArtifactStatus.VERIFIED,
                self.scenario,
                f"{label}.production_terminal",
                "verified PUBLICATION artifact is missing",
            )
            return (
                "ready-run-read-from-postgresql",
                "verified-publication-artifact-read-from-postgresql",
            )

        return await self._perform(
            f"{label}.production_terminal",
            action,
            via="service",
            action_name="DeterministicProductionJobRunner.run_until_idle",
            identities={"run": run_id, "subject": self.production_subjects[label]},
            after=verify,
        )

    async def compose(
        self,
        subject_ids: Sequence[UUID],
        *,
        provenance_batch_ids: Mapping[UUID, UUID] | None = None,
    ) -> Any:
        edition_id = self._edition_id()
        jobs_before = await invariants.work_counts(self.uow_factory)
        async with self.uow_factory() as uow:
            current = await uow.edition_compositions.get_for_edition(edition_id)
            subjects = {
                subject.id: subject for subject in await uow.subjects.list_for_edition(edition_id)
            }
            prepared: list[EditionCompositionEntry] = []
            for position, subject_id in enumerate(subject_ids, start=1):
                subject = subjects.get(subject_id)
                runs = await uow.production_runs.list_for_subject(subject_id)
                ready_runs = [run for run in runs if run.status.value == "ready"]
                run = max(ready_runs, key=lambda item: (item.run_number, item.created_at))
                artifact = await uow.production_artifacts.get_current(
                    run.id, ProductionArtifactStage.PUBLICATION.value
                )
                if subject is None or artifact is None or artifact.canonical_blob_id is None:
                    raise AssertionError(
                        f"{self.scenario} / composition: selected subject lacks "
                        "a verified publication"
                    )
                blob = await uow.blobs.get(artifact.canonical_blob_id)
                if blob is None:
                    raise AssertionError(
                        f"{self.scenario} / composition: publication blob metadata is missing"
                    )
                batch_id = (provenance_batch_ids or {}).get(subject_id)
                if batch_id is None:
                    batch_id = next(
                        (
                            scenario.batch_id
                            for scenario in self.production_scenarios.values()
                            if scenario.subject.id == subject_id
                        ),
                        None,
                    )
                prepared.append(
                    EditionCompositionEntry(
                        subject_id=subject_id,
                        disposition=CompositionDisposition.INCLUDED,
                        position=position,
                        production_run_id=run.id,
                        pipeline_generation=run.pipeline_generation,
                        artifact_id=artifact.id,
                        artifact_version=artifact.version,
                        artifact_input_hash=artifact.input_hash,
                        document_sha256=blob.descriptor.sha256,
                        provenance_batch_id=batch_id,
                    )
                )
        payload = {
            "expected_version": current.version if current is not None else 0,
            "entries": [
                {
                    "subject_id": str(entry.subject_id),
                    "disposition": entry.disposition.value,
                    "position": entry.position,
                    "production_run_id": str(entry.production_run_id),
                    "pipeline_generation": entry.pipeline_generation,
                    "artifact_id": str(entry.artifact_id),
                    "artifact_version": entry.artifact_version,
                    "artifact_input_hash": entry.artifact_input_hash,
                    "provenance_batch_id": (
                        str(entry.provenance_batch_id) if entry.provenance_batch_id else None
                    ),
                    "document_sha256": entry.document_sha256,
                }
                for entry in prepared
            ],
        }

        async def action() -> Any:
            response = await self.client.put(
                f"/api/editions/{edition_id}/composition",
                headers={"Idempotency-Key": f"workflow-compose-{uuid4()}"},
                json=payload,
            )
            if response.status_code != 200:
                raise AssertionError(f"composition route returned HTTP {response.status_code}")
            return cast(dict[str, Any], response.json())

        async def verify(response_body: dict[str, Any]) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                composition = await uow.edition_compositions.get_for_edition(edition_id)
            invariants.require(
                composition is not None
                and response_body["version"] == composition.version
                and {entry.subject_id for entry in composition.entries} == set(subject_ids)
                and all(
                    entry.disposition is CompositionDisposition.INCLUDED
                    for entry in composition.entries
                ),
                self.scenario,
                "composition",
                "current composition differs from the requested selected subjects",
            )
            unchanged = await invariants.assert_no_jobs_or_model_runs_created(
                self.uow_factory,
                jobs_before,
                scenario=self.scenario,
                step="composition",
            )
            artifact_checks = await invariants.assert_manifest_entries_verified(
                self.uow_factory, edition_id, scenario=self.scenario, step="composition"
            )
            return (
                "composition-read-from-postgresql",
                "included-subject-identities-match",
                *unchanged,
                *artifact_checks,
            )

        return await self._perform(
            "composition",
            action,
            via="http",
            action_name="PUT /composition",
            identities={"edition": edition_id, "subject_count": len(subject_ids)},
            after=verify,
        )

    async def exclude_subject(self, subject_id: UUID, *, reason: str) -> UUID:
        edition_id = self._edition_id()
        jobs_before = await invariants.work_counts(self.uow_factory)
        current = await EditionCompositionService(self.uow_factory).get(edition_id)
        version = current.composition.version if current.composition else 0

        async def action() -> UUID:
            response = await self.client.post(
                f"/api/editions/{edition_id}/composition/exclusions/{subject_id}",
                headers={"Idempotency-Key": f"workflow-exclude-{uuid4()}"},
                json={"expected_version": version, "reason": reason},
            )
            if response.status_code != 200:
                raise AssertionError(f"exclusion route returned HTTP {response.status_code}")
            return UUID(response.json()["decision_id"])

        async def verify(decision_id: UUID) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                composition = await uow.edition_compositions.get_for_edition(edition_id)
                decisions = await uow.publication_review_decisions.list_for_edition(edition_id)
            excluded = (
                next(
                    (entry for entry in composition.entries if entry.subject_id == subject_id),
                    None,
                )
                if composition is not None
                else None
            )
            decision = next((item for item in decisions if item.id == decision_id), None)
            invariants.require(
                excluded is not None and excluded.disposition is CompositionDisposition.EXCLUDED,
                self.scenario,
                "exclude_subject",
                "excluded disposition is missing from current PostgreSQL composition",
            )
            invariants.require(
                decision is not None
                and decision.edition_id == edition_id
                and decision.subject_id == subject_id
                and decision.decision is PublicationDecision.EXCLUDE
                and decision.actor_id == "workflow-analyst"
                and decision.reason == reason.strip(),
                self.scenario,
                "exclude_subject",
                "exact exclusion decision audit is missing from PostgreSQL",
            )
            unchanged = await invariants.assert_no_jobs_or_model_runs_created(
                self.uow_factory,
                jobs_before,
                scenario=self.scenario,
                step="exclude_subject",
            )
            return (
                "exclusion-decision-read-from-postgresql",
                "composition-read-from-postgresql",
                *unchanged,
            )

        return await self._perform(
            "exclude_subject",
            action,
            via="http",
            action_name="POST /composition/exclusions/{subject_id}",
            identities={"edition": edition_id, "subject": subject_id},
            after=verify,
        )

    async def include_subject(self, subject_id: UUID) -> Any:
        edition_id = self._edition_id()
        composition_service = EditionCompositionService(self.uow_factory)
        current = await composition_service.get(edition_id)
        composition = current.composition
        if composition is None:
            raise AssertionError(f"{self.scenario} / include_subject: composition is missing")
        existing = next(
            (entry for entry in composition.entries if entry.subject_id == subject_id), None
        )
        if existing is None:
            raise AssertionError(
                f"{self.scenario} / include_subject: subject has no composition entry"
            )
        next_position = (
            max(
                (
                    entry.position or 0
                    for entry in composition.entries
                    if entry.disposition is CompositionDisposition.INCLUDED
                ),
                default=0,
            )
            + 1
        )
        included = replace(
            existing,
            disposition=CompositionDisposition.INCLUDED,
            position=next_position,
            review_decision_id=None,
        )
        version = composition.version
        jobs_before = await invariants.work_counts(self.uow_factory)

        async def action() -> Any:
            return await composition_service.include(
                edition_id,
                included,
                expected_version=version,
                idempotency_key=f"workflow-include-{uuid4()}",
                actor_id="workflow-analyst",
                correlation_id=f"workflow-{edition_id}",
            )

        async def verify(composition: Any) -> tuple[str, ...]:
            persisted = await composition_service.get(edition_id)
            invariants.require(
                persisted.composition is not None
                and any(
                    entry.subject_id == subject_id
                    and entry.disposition is CompositionDisposition.INCLUDED
                    for entry in persisted.composition.entries
                ),
                self.scenario,
                "include_subject",
                "included subject is missing from current PostgreSQL composition",
            )
            unchanged = await invariants.assert_no_jobs_or_model_runs_created(
                self.uow_factory,
                jobs_before,
                scenario=self.scenario,
                step="include_subject",
            )
            return ("included-composition-entry-read-from-postgresql", *unchanged)

        return await self._perform(
            "include_subject",
            action,
            via="service",
            action_name="EditionCompositionService.include",
            identities={"edition": edition_id, "subject": subject_id},
            after=verify,
        )

    async def review(
        self,
        *,
        required_exclusions: Mapping[UUID, tuple[str, str]] | None = None,
        expect_can_accept: bool | None = None,
        expected_deferred_subject_id: UUID | None = None,
        expected_superseded_artifacts: (
            Mapping[UUID, tuple[UUID, int, UUID, UUID, int]] | None
        ) = None,
        expected_inapplicable_exclusions: Mapping[UUID, UUID] | None = None,
    ) -> dict[str, Any]:
        edition_id = self._edition_id()

        async def action() -> dict[str, Any]:
            response = await self.client.get(f"/api/editions/{edition_id}/review")
            if response.status_code != 200:
                raise AssertionError(f"review route returned HTTP {response.status_code}")
            return cast(dict[str, Any], response.json())

        async def verify(body: dict[str, Any]) -> tuple[str, ...]:
            review = await EditionReviewService(self.uow_factory).get(edition_id)
            invariants.require(
                body["can_accept"] is review.can_accept,
                self.scenario,
                "review",
                "HTTP Review differs from the canonical PostgreSQL review projection",
            )
            if expect_can_accept is not None:
                invariants.require(
                    body["can_accept"] is expect_can_accept,
                    self.scenario,
                    "review",
                    f"Review can_accept must be {expect_can_accept}; "
                    f"observed {body['can_accept']!r}",
                )
            if expected_deferred_subject_id is not None:
                deferred_diagnostics = [
                    diagnostic
                    for diagnostic in body["diagnostics"]
                    if diagnostic["code"] == "subject_deferred"
                    and diagnostic["subject_id"] == str(expected_deferred_subject_id)
                ]
                invariants.require(
                    len(deferred_diagnostics) == 1,
                    self.scenario,
                    "review",
                    "Review must expose exactly one subject_deferred diagnostic for "
                    f"{expected_deferred_subject_id}; diagnostics={body['diagnostics']!r}",
                )
            for subject_id, expected in (expected_superseded_artifacts or {}).items():
                diagnostics = [
                    item
                    for item in body["diagnostics"]
                    if item["code"] == "pinned_artifact_superseded"
                    and item["subject_id"] == str(subject_id)
                ]
                pinned_id, pinned_version, available_run_id, available_id, available_version = (
                    expected
                )
                invariants.require(
                    len(diagnostics) == 1,
                    self.scenario,
                    "review",
                    "Review must expose exactly one pinned_artifact_superseded diagnostic for "
                    f"{subject_id}; diagnostics={body['diagnostics']!r}",
                )
                details = diagnostics[0].get("details") or {}
                invariants.require(
                    details.get("pinned_artifact_id") == str(pinned_id)
                    and details.get("pinned_artifact_version") == pinned_version
                    and details.get("available_run_id") == str(available_run_id)
                    and details.get("available_artifact_id") == str(available_id)
                    and details.get("available_artifact_version") == available_version,
                    self.scenario,
                    "review",
                    "pinned_artifact_superseded omits or changes pinned/current "
                    "artifact identities",
                )
            for subject_id, decision_id in (expected_inapplicable_exclusions or {}).items():
                exclusion = next(
                    (item for item in review.exclusions if item.subject_id == subject_id), None
                )
                api_exclusion = next(
                    (item for item in body["exclusions"] if item["subject_id"] == str(subject_id)),
                    None,
                )
                diagnostic = next(
                    (
                        item
                        for item in body["diagnostics"]
                        if item["code"] == "exclusion_decision_missing"
                        and item["subject_id"] == str(subject_id)
                    ),
                    None,
                )
                invariants.require(
                    exclusion is not None
                    and exclusion.decision_id == decision_id
                    and not exclusion.applicable
                    and api_exclusion is not None
                    and api_exclusion["decision_id"] == str(decision_id)
                    and api_exclusion["applicable"] is False
                    and diagnostic is not None,
                    self.scenario,
                    "review",
                    "the v1 exclusion decision is not applicable to the corrected artifact",
                )
            required = required_exclusions or {}
            for subject_id, (actor_id, reason) in required.items():
                exclusion = next(
                    (item for item in review.exclusions if item.subject_id == subject_id), None
                )
                invariants.require(
                    exclusion is not None
                    and exclusion.applicable
                    and exclusion.actor_id == actor_id
                    and exclusion.reason == reason,
                    self.scenario,
                    "review",
                    "required exclusion audit is absent or inapplicable in Review",
                )
                api_exclusion = next(
                    (item for item in body["exclusions"] if item["subject_id"] == str(subject_id)),
                    None,
                )
                invariants.require(
                    api_exclusion is not None
                    and api_exclusion["applicable"] is True
                    and api_exclusion["actor_id"] == actor_id
                    and api_exclusion["reason"] == reason,
                    self.scenario,
                    "review",
                    "HTTP Review omitted the required exclusion audit",
                )
            return (
                "review-read-from-postgresql-backed-service",
                "exclusion-projection-verified",
                *(("review-can-accept-verified",) if expect_can_accept is not None else ()),
                *(
                    ("subject-deferred-diagnostic-exposed",)
                    if expected_deferred_subject_id is not None
                    else ()
                ),
                *(
                    ("pinned-artifact-superseded-identities-exposed",)
                    if expected_superseded_artifacts
                    else ()
                ),
                *(
                    ("v1-exclusion-decision-not-applicable-to-current-artifact",)
                    if expected_inapplicable_exclusions
                    else ()
                ),
            )

        return await self._perform(
            "review",
            action,
            via="http",
            action_name="GET /review",
            identities={"edition": edition_id},
            assertions=(
                *(("review-can-accept-expected",) if expect_can_accept is not None else ()),
                *(
                    ("review-exposes-subject-deferred-for-subject",)
                    if expected_deferred_subject_id is not None
                    else ()
                ),
                *(
                    ("review-exposes-pinned-artifact-superseded-identities",)
                    if expected_superseded_artifacts
                    else ()
                ),
                *(
                    ("review-exposes-inapplicable-v1-exclusion",)
                    if expected_inapplicable_exclusions
                    else ()
                ),
            ),
            after=verify,
        )

    async def validate(self) -> Any:
        edition_id = self._edition_id()
        service = EditionCompositionService(self.uow_factory)

        async def action() -> Any:
            return await service.validate(edition_id)

        async def verify(result: Any) -> tuple[str, ...]:
            canonical = await service.get(edition_id)
            invariants.require(
                canonical.composition is not None,
                self.scenario,
                "validate",
                "composition is missing from PostgreSQL",
            )
            invariants.require(
                not result.reasons,
                self.scenario,
                "validate",
                "composition validation has blocking reasons",
            )
            return ("composition-read-from-postgresql", "validation-reasons-empty")

        return await self._perform(
            "validate",
            action,
            via="service",
            action_name="EditionCompositionService.validate",
            identities={"edition": edition_id},
            after=verify,
        )

    async def accept(self) -> Any:
        edition_id = self._edition_id()
        composition = await EditionCompositionService(self.uow_factory).get(edition_id)
        if composition.composition is None:
            raise AssertionError(f"{self.scenario} / accept: composition is missing")
        expected_version = composition.composition.version
        hashes_before = await invariants.manifest_hashes(self.uow_factory, edition_id)

        async def action() -> Any:
            response = await self.client.post(
                f"/api/editions/{edition_id}/publication/accept",
                json={"expected_composition_version": expected_version},
            )
            if response.status_code != 202:
                raise AssertionError(f"accept route returned HTTP {response.status_code}")
            body = response.json()
            service_result = await EditionPublicationService(
                self.uow_factory, self.artifact_store
            ).accept(
                edition_id,
                actor_id="workflow-analyst",
                expected_composition_version=expected_version,
                correlation_id=f"workflow-accept-{edition_id}",
            )
            invariants.require(
                str(service_result.manifest.id) == body["manifest_id"]
                and service_result.manifest.content_sha256 == body["manifest_sha256"],
                self.scenario,
                "accept",
                "official HTTP and service accept results identify different manifests",
            )
            return service_result

        async def verify(result: Any) -> tuple[str, ...]:
            manifest = result.manifest
            matching = await invariants.assert_persisted_manifest_matches_accept(
                self.uow_factory,
                manifest.id,
                manifest,
                scenario=self.scenario,
                step="accept",
            )
            entries = await invariants.assert_manifest_entries_verified(
                self.uow_factory, edition_id, scenario=self.scenario, step="accept"
            )
            exclusions = await invariants.assert_manifest_exclusions_applicable(
                self.uow_factory, edition_id, scenario=self.scenario, step="accept"
            )
            deferred = await invariants.assert_deferred_subjects_not_excluded(
                self.uow_factory, edition_id, scenario=self.scenario, step="accept"
            )
            unchanged = await invariants.assert_manifest_hashes_unchanged(
                self.uow_factory,
                edition_id,
                hashes_before,
                scenario=self.scenario,
                step="accept",
            )
            self.accepted_manifest = manifest
            return (*matching, *entries, *exclusions, *deferred, *unchanged)

        return await self._perform(
            "accept",
            action,
            via="http",
            action_name="POST /publication/accept + EditionPublicationService.accept",
            identities={"edition": edition_id},
            after=verify,
        )

    async def assemble(self) -> Any:
        if self.accepted_manifest is None:
            raise AssertionError(f"{self.scenario} / assemble: accept has not completed")
        edition_id = self._edition_id()
        manifest = self.accepted_manifest
        hashes_before = await invariants.manifest_hashes(self.uow_factory, edition_id)
        service = EditionAssemblyService(self.uow_factory, self.artifact_store)

        async def action() -> Any:
            return await service.assemble(manifest.id)

        async def verify(release: Any) -> tuple[str, ...]:
            async with self.uow_factory() as uow:
                persisted = await uow.edition_releases.get_by_manifest(manifest.id)
            invariants.require(
                persisted is not None and persisted.id == release.id,
                self.scenario,
                "assemble",
                "EditionRelease is missing or differs from PostgreSQL",
            )
            unchanged = await invariants.assert_manifest_hashes_unchanged(
                self.uow_factory,
                edition_id,
                hashes_before,
                scenario=self.scenario,
                step="assemble",
            )
            entries = await invariants.assert_manifest_entries_verified(
                self.uow_factory, edition_id, scenario=self.scenario, step="assemble"
            )
            exclusions = await invariants.assert_manifest_exclusions_applicable(
                self.uow_factory, edition_id, scenario=self.scenario, step="assemble"
            )
            return ("release-read-from-postgresql", *unchanged, *entries, *exclusions)

        return await self._perform(
            "assemble_release",
            action,
            via="service",
            action_name="EditionAssemblyService.assemble",
            identities={"edition": edition_id, "manifest": manifest.id},
            after=verify,
        )
