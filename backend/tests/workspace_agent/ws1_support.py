"""In-memory investigation chain used by the M24-WS1 workspace oracles.

The canonical ingest repositories are the real in-memory fakes from
``cti_app.application.investigation_persistence``; the remaining ports are
small dict-backed adapters so the real workspace service can run against the
real host agent sidecar without a database.
"""

from __future__ import annotations

import calendar
import hashlib
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

from cti_app.application.investigation_bootstrap import (
    BootstrapVtCapabilities,
    InvestigationBootstrapService,
)
from cti_app.application.investigation_contracts import ArticleContextV1
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryInvestigationPlanRepository,
    InMemoryInvestigationTaskRepository,
    InMemoryWorkspaceBindingRepository,
    InMemoryWorkspaceFileEventRepository,
    InMemoryWorkspaceFileRevisionRepository,
    InMemoryWorkspaceSyncCursorRepository,
)
from cti_app.application.investigation_workspace import InvestigationWorkspaceService
from cti_app.application.research_context_builder import ArticleSnapshot
from cti_app.application.workspace_agent import WorkspaceAgentPort
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.editorial import AnalystDecision
from cti_app.domain.entities import Subject
from cti_app.domain.production import AnalystInvestigation, LoopBudget, ProductionRun

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
SYNTHESIS_SHA256 = "a" * 64


@dataclass
class FakeAnalystInvestigations:
    rows: dict[UUID, AnalystInvestigation] = field(default_factory=dict)

    async def get(self, investigation_id: UUID) -> AnalystInvestigation | None:
        return self.rows.get(investigation_id)

    async def get_for_run(self, run_id: UUID) -> AnalystInvestigation | None:
        return next(
            (row for row in self.rows.values() if row.production_run_id == run_id),
            None,
        )


@dataclass
class FakeProductionRuns:
    rows: dict[UUID, ProductionRun] = field(default_factory=dict)

    async def get(self, run_id: UUID) -> ProductionRun | None:
        return self.rows.get(run_id)


@dataclass
class FakeSubjects:
    rows: dict[UUID, Subject] = field(default_factory=dict)

    async def get(self, subject_id: UUID) -> Subject | None:
        return self.rows.get(subject_id)

    async def list_for_edition(self, edition_id: UUID) -> Sequence[Subject]:
        return tuple(
            sorted(
                (row for row in self.rows.values() if row.edition_id == edition_id),
                key=lambda item: (item.created_at, str(item.id)),
            )
        )


@dataclass
class FakeEditions:
    rows: dict[UUID, Edition] = field(default_factory=dict)

    async def get(self, edition_id: UUID) -> Edition | None:
        return self.rows.get(edition_id)


@dataclass
class FakeDecisions:
    rows: list[AnalystDecision] = field(default_factory=list)

    async def append(self, decision: AnalystDecision) -> None:
        self.rows.append(decision)

    async def list_for_investigation(self, investigation_id: UUID) -> Sequence[AnalystDecision]:
        return tuple(row for row in self.rows if row.investigation_id == investigation_id)


class FakeArtifactStore:
    """Content-addressed JSON payloads read by the bootstrap snapshot service."""

    def __init__(self) -> None:
        self.payloads: dict[UUID, dict[str, Any]] = {}

    def put_json(self, payload: dict[str, Any]) -> tuple[UUID, str]:
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        blob_id = uuid4()
        self.payloads[blob_id] = payload
        return blob_id, hashlib.sha256(encoded).hexdigest()

    async def read_json(self, blob_id: UUID, *, max_bytes: int | None = None) -> dict[str, Any]:
        del max_bytes
        return dict(self.payloads[blob_id])


@dataclass
class FakeArticleSnapshots:
    snapshot: ArticleSnapshot

    async def resolve(self, synthesis_artifact_id: UUID) -> ArticleSnapshot:
        if synthesis_artifact_id != self.snapshot.synthesis_artifact_id:
            raise KeyError("unknown synthesis artifact")
        return self.snapshot


@dataclass
class ChainStore:
    """One investigation chain plus the canonical workspace repositories."""

    edition: Edition
    subject: Subject
    run: ProductionRun
    investigation: AnalystInvestigation
    article_snapshot: ArticleSnapshot
    investigations: FakeAnalystInvestigations
    runs: FakeProductionRuns
    subjects: FakeSubjects
    editions: FakeEditions
    bindings: InMemoryWorkspaceBindingRepository
    revisions: InMemoryWorkspaceFileRevisionRepository
    events: InMemoryWorkspaceFileEventRepository
    cursors: InMemoryWorkspaceSyncCursorRepository
    decisions: FakeDecisions
    plans: InMemoryInvestigationPlanRepository
    tasks: InMemoryInvestigationTaskRepository
    findings: InMemoryFindingRepository
    observations: InMemoryEvidenceObservationRepository
    artifacts: FakeArtifactStore = field(default_factory=FakeArtifactStore)

    def uow(self) -> ChainUnitOfWork:
        return ChainUnitOfWork(self)

    def service(self, agent: WorkspaceAgentPort) -> InvestigationWorkspaceService:
        snapshots = FakeArticleSnapshots(self.article_snapshot)
        bootstrap = InvestigationBootstrapService(
            uow_factory=self.uow,
            article_snapshots=snapshots,
            artifact_store=self.artifacts,
            capabilities=BootstrapVtCapabilities(),
        )
        return InvestigationWorkspaceService(
            uow_factory=self.uow,
            workspace_agent=agent,
            article_snapshots=snapshots,
            bootstrap=bootstrap,
        )


class ChainUnitOfWork:
    def __init__(self, store: ChainStore) -> None:
        self.analyst_investigations = store.investigations
        self.production_runs = store.runs
        self.subjects = store.subjects
        self.editions = store.editions
        self.workspace_bindings = store.bindings
        self.workspace_file_revisions = store.revisions
        self.workspace_file_events = store.events
        self.workspace_sync_cursors = store.cursors
        self.analyst_decisions = store.decisions
        self.investigation_plans = store.plans
        self.investigation_tasks = store.tasks
        self.investigation_findings = store.findings
        self.evidence_observations = store.observations

    async def __aenter__(self) -> ChainUnitOfWork:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        return None

    async def rollback(self) -> None:
        return None


def make_chain(
    *,
    subject_slug: str = "bluemoon-cn",
    period_start: date = date(2026, 11, 1),
    country_code: str = "FR",
    created_at: datetime = NOW,
    extra_subjects: Sequence[Subject] = (),
) -> ChainStore:
    edition = Edition(
        country="France",
        country_code=country_code,
        period_start=period_start,
        period_end=date(
            period_start.year,
            period_start.month,
            calendar.monthrange(period_start.year, period_start.month)[1],
        ),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    subject = Subject(
        edition_id=edition.id,
        title="Synthetic article",
        slug=subject_slug,
        tlp=TLP.AMBER,
        created_at=created_at,
        updated_at=created_at,
    )
    run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
    investigation = AnalystInvestigation(
        production_run_id=run.id,
        subject_id=subject.id,
        synthesis_artifact_id=uuid4(),
        budget=LoopBudget(),
    )
    article_snapshot = ArticleSnapshot(
        subject_id=subject.id,
        production_run_id=run.id,
        synthesis_artifact_id=investigation.synthesis_artifact_id,
        synthesis_sha256=SYNTHESIS_SHA256,
        article=ArticleContextV1(
            short_summary="Synthetic article summary",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
    )
    subjects = {subject.id: subject, **{item.id: item for item in extra_subjects}}
    return ChainStore(
        edition=edition,
        subject=subject,
        run=run,
        investigation=investigation,
        article_snapshot=article_snapshot,
        investigations=FakeAnalystInvestigations({investigation.id: investigation}),
        runs=FakeProductionRuns({run.id: run}),
        subjects=FakeSubjects(subjects),
        editions=FakeEditions({edition.id: edition}),
        bindings=InMemoryWorkspaceBindingRepository(),
        revisions=InMemoryWorkspaceFileRevisionRepository(),
        events=InMemoryWorkspaceFileEventRepository(),
        cursors=InMemoryWorkspaceSyncCursorRepository(),
        decisions=FakeDecisions(),
        plans=InMemoryInvestigationPlanRepository(),
        tasks=InMemoryInvestigationTaskRepository(clock=lambda: NOW),
        findings=InMemoryFindingRepository(),
        observations=InMemoryEvidenceObservationRepository(),
    )


def workspace_dir(agent_root: Path, delivery_code: str, article_slug: str) -> Path:
    return Path(agent_root) / delivery_code / article_slug
