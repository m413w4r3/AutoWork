"""PostgreSQL oracle for the real contribution import chain (M24-07).

One disposable migrated database: a human Markdown note becomes a
``work_contribution`` (never citable), a structured result becomes a canonical
``observation`` through ``ProductionEvidenceRecorder``, and a finding citing it
is persisted ``inconclusive`` even when the import requests ``supported``.
Replays return the same row, the blob is readable by its recorded SHA-256, and
the append-only trigger refuses an UPDATE.
"""

from __future__ import annotations

import hashlib
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.work_contributions import (
    IMPORTED_SUPPORTED_LIMITATION,
    ContributionAuthority,
    WorkContributionService,
)
from cti_app.application.workspace_agent import (
    WorkspaceBindingV1,
    WorkspaceContentV1,
    WorkspaceFileRevisionV1,
    workspace_revision_id,
)
from cti_app.domain.classification import TLP
from cti_app.domain.editions import Edition
from cti_app.domain.entities import Subject
from cti_app.domain.investigation import EvidenceKind, FindingAssessment, FindingKind
from cti_app.domain.production import (
    AnalystInvestigation,
    LoopBudget,
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionRun,
)
from cti_app.domain.work_contribution import (
    ContributionDeclarationV1,
    FindingDeclarationV1,
    ObservationDeclarationV1,
    WorkContributionCategory,
    WorkContributionImportStatus,
    WorkContributionMethod,
    WorkContributionObjectType,
    WorkContributionOrigin,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.session import (
    create_postgres_engine,
    create_session_factory,
)
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=UTC)
OBJECT_SHA = "e" * 64


class _WorkspaceAgent:
    def __init__(self) -> None:
        self.contents: dict[tuple[str, str], WorkspaceContentV1] = {}

    async def read_content(self, workspace_id: str, *, path: str) -> WorkspaceContentV1:
        return self.contents[(workspace_id, path)]


class _ArticleSnapshots:
    async def resolve(self, synthesis_artifact_id: UUID) -> Any:
        raise NotImplementedError("contribution imports never resolve the synthesis")

    async def get_claims(
        self, investigation_id: UUID, claim_ids: tuple[str, ...] = ()
    ) -> tuple[Any, ...]:
        return ()


async def _make_investigation(
    session_factory: async_sessionmaker[AsyncSession],
) -> AnalystInvestigation:
    suffix = uuid4().hex
    edition = Edition(
        country=f"Contribution fixture {suffix}",
        country_code=reserve_edition_code(),
        period_start=date(2026, 11, 1),
        period_end=date(2026, 11, 30),
        tlp=TLP.AMBER,
        languages=("en",),
    )
    async with SqlAlchemyUnitOfWork(session_factory) as uow:
        assert await uow.editions.add_if_absent(edition)
        subject = Subject(
            edition_id=edition.id,
            title="Contribution fixture",
            slug=f"contribution-fixture-{suffix}",
            tlp=TLP.AMBER,
        )
        run = ProductionRun(subject_id=subject.id, edition_id=edition.id)
        artifact = ProductionArtifact(
            production_run_id=run.id,
            subject_id=subject.id,
            stage=ProductionArtifactStage.SYNTHESIS,
            version=1,
            input_hash="d" * 64,
        )
        investigation = AnalystInvestigation(
            production_run_id=run.id,
            subject_id=subject.id,
            synthesis_artifact_id=artifact.id,
            budget=LoopBudget(),
        )
        await uow.subjects.add(subject)
        await uow.production_runs.add(run)
        await uow.production_artifacts.append(artifact)
        await uow.analyst_investigations.add(investigation)
        await uow.commit()
    return investigation


def _revision(workspace_id: str, path: str, content: bytes) -> WorkspaceFileRevisionV1:
    sha256 = hashlib.sha256(content).hexdigest()
    return WorkspaceFileRevisionV1(
        workspace_id=workspace_id,
        revision_id=workspace_revision_id(workspace_id=workspace_id, path=path, sha256=sha256),
        path=path,
        sha256=sha256,
        size=len(content),
        mode="0640",
        mtime_ns=1_700_000_000_000_000_000,
        observed_at=NOW,
        origin="filesystem",
    )


async def test_work_contribution_import_chain_persists_real_rows(
    migrated_postgres_url: str, tmp_path: Path
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    try:
        investigation = await _make_investigation(session_factory)
        workspace_id = "ws-" + uuid4().hex
        binding = WorkspaceBindingV1(
            workspace_id=workspace_id,
            investigation_id=investigation.id,
            subject_id=investigation.subject_id,
            delivery_code="IR202611",
            article_slug="bluemoon-cn",
            synthesis_sha256="a" * 64,
            created_at=NOW,
        )
        note_path = "notes/humaines/note.md"
        note_bytes = (
            b"# Note humaine\n\nHypothese: le chargeur utilise InternetOpenA.\nNon verifie.\n"
        )
        result_path = "results/strings.txt"
        result_bytes = b"GetProcAddress\nInternetOpenA\n"
        finding_path = "results/finding.md"
        finding_bytes = b"# Finding\n\nThe loader opens a URL.\n"
        note = _revision(workspace_id, note_path, note_bytes)
        copy = _revision(workspace_id, "notes/humaines/copy.txt", note_bytes)
        result = _revision(workspace_id, result_path, result_bytes)
        finding_source = _revision(workspace_id, finding_path, finding_bytes)
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            await uow.workspace_bindings.add_if_absent(binding)
            for revision in (note, copy, result, finding_source):
                await uow.workspace_file_revisions.append_if_absent(revision)
            await uow.commit()

        agent = _WorkspaceAgent()
        for revision, content in (
            (note, note_bytes),
            (copy, note_bytes),
            (result, result_bytes),
            (finding_source, finding_bytes),
        ):
            agent.contents[(workspace_id, revision.path)] = WorkspaceContentV1(
                content=content,
                sha256=revision.sha256,
                size=len(content),
                mtime_ns=revision.mtime_ns,
            )
        artifact_store = ProductionArtifactStore(
            BlobCatalogService(
                FilesystemBlobStore(tmp_path / "contributions-blobs"),
                lambda: SqlAlchemyUnitOfWork(session_factory),
            )
        )
        service = WorkContributionService(
            uow_factory=lambda: SqlAlchemyUnitOfWork(session_factory),
            workspace_agent=agent,
            artifact_store=artifact_store,
            article_snapshots=_ArticleSnapshots(),
            max_contribution_bytes=16 * 1024 * 1024,
        )

        # 1. A plain human Markdown note: row + blob, never citable evidence.
        note_row = await service.import_revision(
            investigation.id,
            workspace_id=workspace_id,
            workspace_revision_id=note.revision_id,
            declaration=ContributionDeclarationV1(
                category=WorkContributionCategory.WORK_CONTRIBUTION
            ),
            authority=ContributionAuthority(origin=WorkContributionOrigin.HUMAN),
            idempotency_key="pg-note-import-0001",
        )
        assert note_row.import_status is WorkContributionImportStatus.IMPORTED
        assert note_row.category is WorkContributionCategory.WORK_CONTRIBUTION
        assert note_row.citable_as_evidence is False
        assert note_row.origin_actor_id is None
        assert note_row.observation_id is None
        assert note_row.content_blob_id is not None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            blob_record = await uow.blobs.get(note_row.content_blob_id)
        assert blob_record is not None and blob_record.descriptor.sha256 == note.sha256
        assert (
            await artifact_store.read_bytes(note_row.content_blob_id, max_bytes=len(note_bytes))
            == note_bytes
        )

        # The same bytes under another path reuse the content address, even
        # though the suffix differs (the blob MIME is content-derived only).
        copy_row = await service.import_revision(
            investigation.id,
            workspace_id=workspace_id,
            workspace_revision_id=copy.revision_id,
            declaration=ContributionDeclarationV1(
                category=WorkContributionCategory.WORK_CONTRIBUTION
            ),
            authority=ContributionAuthority(origin=WorkContributionOrigin.HOST),
            idempotency_key="pg-note-copy-0001",
        )
        assert copy_row.import_status is WorkContributionImportStatus.IMPORTED
        assert copy_row.content_blob_id == note_row.content_blob_id

        # The reverse CHECK is enforced by PostgreSQL, not only the domain.
        async with session_factory() as session:
            with pytest.raises(DBAPIError) as exc_info:
                await session.execute(
                    sa.text(
                        "INSERT INTO work_contributions ("
                        "id, investigation_id, workspace_id, category, schema_version, "
                        "workspace_revision_id, file_relpath, file_sha256, observed_at, "
                        "origin, import_status, validation_error_code, idempotency_key, "
                        "created_at, content_blob_id, content_sha256"
                        ") VALUES ("
                        ":id, :investigation_id, :workspace_id, 'work_contribution', "
                        "'work-contribution.v1', :revision_id, :path, :sha256, now(), "
                        "'host', 'imported', 'forbidden_file_type', "
                        "'reverse-check-row-0001', now(), :blob_id, :sha256"
                        ")"
                    ),
                    {
                        "id": uuid4(),
                        "investigation_id": investigation.id,
                        "workspace_id": workspace_id,
                        "revision_id": note.revision_id,
                        "path": note.path,
                        "sha256": note.sha256,
                        "blob_id": note_row.content_blob_id,
                    },
                )
            assert str(getattr(exc_info.value.orig, "sqlstate", "")) == "23514"
            await session.rollback()

        # 2. A structured result becomes a canonical observation.
        observation_row = await service.import_revision(
            investigation.id,
            workspace_id=workspace_id,
            workspace_revision_id=result.revision_id,
            declaration=ContributionDeclarationV1(
                category=WorkContributionCategory.OBSERVATION,
                observation=ObservationDeclarationV1(
                    object_type=WorkContributionObjectType.FILE,
                    method=WorkContributionMethod.STRING_EXTRACTION,
                    object_id=OBJECT_SHA,
                    tool="strings",
                    tool_version="2.42",
                    parameters={"min_length": 6},
                    limitations=("Sortie bornée à cette exécution.",),
                ),
            ),
            authority=ContributionAuthority(origin=WorkContributionOrigin.HOST),
            idempotency_key="pg-observation-import-0001",
        )
        assert observation_row.import_status is WorkContributionImportStatus.IMPORTED
        assert observation_row.citable_as_evidence is True
        assert observation_row.observation_id is not None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            observation = await uow.evidence_observations.get(observation_row.observation_id)
        assert observation is not None
        assert observation.investigation_id == investigation.id
        assert observation.source == "workspace_import"
        assert observation.source_version == "work-contribution.v1"
        assert observation.kind is EvidenceKind.BINARY_STRING
        assert observation.object_type == "file"
        assert observation.object_id == OBJECT_SHA
        assert observation.sha256 == result.sha256
        assert observation.blob_id == observation_row.content_blob_id

        # 3. Imported findings are never automatically `supported`.
        finding_row = await service.import_revision(
            investigation.id,
            workspace_id=workspace_id,
            workspace_revision_id=finding_source.revision_id,
            declaration=ContributionDeclarationV1(
                category=WorkContributionCategory.FINDING,
                finding=FindingDeclarationV1(
                    kind=FindingKind.CAPABILITY,
                    assessment=FindingAssessment.SUPPORTED,
                    statement="The loader imports InternetOpenA.",
                    observation_refs=(observation.id,),
                ),
            ),
            authority=ContributionAuthority(origin=WorkContributionOrigin.HOST),
            idempotency_key="pg-finding-import-0001",
        )
        assert finding_row.import_status is WorkContributionImportStatus.IMPORTED
        assert finding_row.finding_id is not None
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            finding = await uow.investigation_findings.get(finding_row.finding_id)
        assert finding is not None
        assert finding.assessment is FindingAssessment.INCONCLUSIVE
        assert finding.assessment is not FindingAssessment.SUPPORTED
        assert IMPORTED_SUPPORTED_LIMITATION in finding.limitations
        assert finding.observation_refs == (observation.id,)

        # 4. Replaying the same revision returns the same row, no second blob.
        replay = await service.import_revision(
            investigation.id,
            workspace_id=workspace_id,
            workspace_revision_id=result.revision_id,
            declaration=ContributionDeclarationV1(
                category=WorkContributionCategory.OBSERVATION,
                observation=ObservationDeclarationV1(
                    object_type=WorkContributionObjectType.FILE,
                    method=WorkContributionMethod.STRING_EXTRACTION,
                    object_id=OBJECT_SHA,
                    tool="strings",
                    tool_version="2.42",
                    parameters={"min_length": 6},
                    limitations=("Sortie bornée à cette exécution.",),
                ),
            ),
            authority=ContributionAuthority(origin=WorkContributionOrigin.HOST),
            idempotency_key="pg-observation-import-0002",
        )
        assert replay.id == observation_row.id
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            rows = await uow.work_contributions.list_for_investigation(investigation.id)
        assert len(rows) == 4

        # 5. Append-only: an UPDATE is refused by the trigger.
        async with session_factory() as session:
            with pytest.raises(DBAPIError) as exc_info:
                await session.execute(
                    sa.text("UPDATE work_contributions SET origin = 'human' WHERE id = :id"),
                    {"id": note_row.id},
                )
            assert str(getattr(exc_info.value.orig, "sqlstate", "")) == "55000"
            await session.rollback()
    finally:
        await engine.dispose()
