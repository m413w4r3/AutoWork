from __future__ import annotations

import asyncio
import hashlib
from collections.abc import Callable
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import cast
from uuid import NAMESPACE_URL, UUID, uuid4, uuid5

import httpx
import pytest
from alembic import command
from fastapi import FastAPI, Request, Response
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from starlette.middleware.base import RequestResponseEndpoint

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_drafts import (
    EditorialDraftApplicationError,
    EditorialDraftService,
)
from cti_app.application.editorial_workspace.registry import WorkspaceOccurrenceResolution
from cti_app.application.persistence import UnitOfWork, UnitOfWorkFactory
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor
from cti_app.domain.editorial_workspace import EditorialWorkspaceError
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork
from tests.integration.conftest import _alembic_config
from tests.integration.editorial.test_proposal_adoption_postgres import (
    _MemoryBlobStore,
    _seed,
    _Seeded,
    _service,
)

pytestmark = pytest.mark.integration


class _SubjectScopedWorkspace:
    def __init__(self, seeded: _Seeded) -> None:
        self._seeded = seeded

    async def resolve_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        if subject_id != self._seeded.subject.id:
            raise EditorialWorkspaceError("subject_not_found")
        return await self._seeded.workspace.resolve_occurrence(subject_id, occurrence_id)

    async def resolve_occurrence_in_uow(
        self, uow: UnitOfWork, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        if subject_id != self._seeded.subject.id:
            raise EditorialWorkspaceError("subject_not_found")
        return await self._seeded.workspace.resolve_occurrence_in_uow(
            uow, subject_id, occurrence_id
        )


def _draft_service(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
    *,
    retention_days: int | None = 30,
    clock: Callable[[], datetime] | None = None,
) -> EditorialDraftService:
    return EditorialDraftService(
        uow_factory=uow_factory,
        workspace_service=_SubjectScopedWorkspace(seeded),
        blob_store=seeded.blob_store,
        retention_days=retention_days,
        clock=clock,
    )


def _app(
    seeded: _Seeded,
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> FastAPI:
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_draft_retention_days=30),
    )
    monkeypatch.setattr(
        editorial_workspace_api, "_service", lambda _request: _SubjectScopedWorkspace(seeded)
    )
    app = FastAPI()
    app.state.uow_factory = uow_factory
    app.state.production_artifact_store = seeded.artifact_store
    app.state.blob_store = seeded.blob_store
    app.include_router(editorial_workspace_api.router)

    @app.middleware("http")
    async def attach_global_identity(
        request: Request, call_next: RequestResponseEndpoint
    ) -> Response:
        # This non-UUID actor is mapped opaquely by the real API identity seam.
        request.state.identity = SimpleNamespace(actor_id="dev-analyst")
        return await call_next(request)

    return app


async def _put_draft(
    client: httpx.AsyncClient,
    seeded: _Seeded,
    *,
    text: str = "Texte éditorial synthétique.",
) -> httpx.Response:
    occurrence_id = seeded.snapshot.block_registry[0].block_id
    return await client.put(
        f"/api/subjects/{seeded.subject.id}/editor/blocks/{occurrence_id}/draft",
        headers={"If-Match": '"0"'},
        json={"snapshot_id": str(seeded.snapshot.snapshot_id), "text": text},
    )


@pytest.mark.asyncio
async def test_draft_subject_acl_and_version_fence(
    uow_factory: UnitOfWorkFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    service = _draft_service(seeded, uow_factory)
    occurrence_id = seeded.snapshot.block_registry[0].block_id
    saved = await service.save(
        subject_id=seeded.subject.id,
        occurrence_id=occurrence_id,
        actor_id=uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst"),
        snapshot_id=seeded.snapshot.snapshot_id,
        text="Brouillon non canonique.",
        expected_version=0,
    )
    assert saved.draft.version == 1
    assert saved.draft.actor_id == uuid5(NAMESPACE_URL, "autowork-actor:dev-analyst")

    with pytest.raises(EditorialDraftApplicationError) as denied:
        await service.get(subject_id=uuid4(), occurrence_id=occurrence_id)
    assert denied.value.code == "draft_not_found"

    # Actor is audit metadata only; the API-global subject boundary controls reads.
    read_back = await service.get(subject_id=seeded.subject.id, occurrence_id=occurrence_id)
    assert read_back.text == "Brouillon non canonique."
    app = _app(seeded, uow_factory, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        path = f"/api/subjects/{seeded.subject.id}/editor/blocks/{occurrence_id}/draft"
        fetched = await client.get(path)
        assert fetched.status_code == 200, fetched.json()
        assert fetched.headers["cache-control"] == "no-store"
        assert fetched.headers["etag"] == '"1"'
        stale_write = await client.put(
            path,
            headers={"If-Match": '"0"'},
            json={"snapshot_id": str(seeded.snapshot.snapshot_id), "text": "Conflit."},
        )
        assert stale_write.status_code == 409, stale_write.json()
        assert stale_write.headers["cache-control"] == "no-store"
        assert stale_write.json()["detail"]["code"] == "draft_version_conflict"
    with pytest.raises(EditorialDraftApplicationError) as conflict:
        await service.save(
            subject_id=seeded.subject.id,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=seeded.snapshot.snapshot_id,
            text="Conflit de version.",
            expected_version=0,
        )
    assert conflict.value.code == "draft_version_conflict"
    previous_resolution = seeded.workspace.resolution
    assert previous_resolution.artifact_version is not None
    seeded.workspace.resolution = replace(
        previous_resolution,
        record=replace(
            previous_resolution.record,
            snapshot_id=uuid4(),
        ),
        artifact_id=uuid4(),
        artifact_version=previous_resolution.artifact_version + 1,
        canonical_sha256="e" * 64,
    )
    stale_read = await service.get(subject_id=seeded.subject.id, occurrence_id=occurrence_id)
    assert stale_read.is_stale
    async with uow_factory() as uow:
        row = await uow.editorial_drafts.get_for_update(seeded.subject.id, occurrence_id)
        assert row is not None and row.version == 1
        assert row.text_sha256 == hashlib.sha256(b"Brouillon non canonique.").hexdigest()
        assert not hasattr(row, "text")


@pytest.mark.asyncio
async def test_draft_ttl_defaults_to_30_days_after_last_change_and_is_configurable(
    uow_factory: UnitOfWorkFactory,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    occurrence_id = seeded.snapshot.block_registry[0].block_id
    assert Settings(_env_file=None).editorial_draft_retention_days == 30

    default_draft = await _draft_service(seeded, uow_factory, retention_days=30).save(
        subject_id=seeded.subject.id,
        occurrence_id=occurrence_id,
        actor_id=uuid4(),
        snapshot_id=seeded.snapshot.snapshot_id,
        text="TTL par défaut.",
        expected_version=0,
    )
    assert default_draft.draft.expires_at == default_draft.draft.updated_at + timedelta(days=30)

    configured = await _draft_service(seeded, uow_factory, retention_days=7).save(
        subject_id=seeded.subject.id,
        occurrence_id=occurrence_id,
        actor_id=uuid4(),
        snapshot_id=seeded.snapshot.snapshot_id,
        text="TTL configuré.",
        expected_version=1,
    )
    assert configured.draft.version == 2
    assert configured.draft.expires_at == configured.draft.updated_at + timedelta(days=7)


@pytest.mark.asyncio
async def test_expired_draft_is_unavailable_and_purged(
    uow_factory: UnitOfWorkFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    occurrence_id = seeded.snapshot.block_registry[0].block_id
    service = _draft_service(seeded, uow_factory, retention_days=30)
    saved = await service.save(
        subject_id=seeded.subject.id,
        occurrence_id=occurrence_id,
        actor_id=uuid4(),
        snapshot_id=seeded.snapshot.snapshot_id,
        text="À expirer.",
        expected_version=0,
    )
    expired_at = datetime.now(UTC) - timedelta(seconds=1)
    async with uow_factory() as uow:
        blob = await uow.blobs.get(saved.draft.text_blob_ref)
        assert blob is not None
        await uow.editorial_drafts.save(
            replace(
                saved.draft,
                updated_at=expired_at - timedelta(seconds=1),
                expires_at=expired_at,
            )
        )
        await uow.commit()

    app = _app(seeded, uow_factory, monkeypatch)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.get(
            f"/api/subjects/{seeded.subject.id}/editor/blocks/{occurrence_id}/draft"
        )
    assert response.status_code == 410, response.json()
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["code"] == "draft_expired"
    assert response.json()["detail"]["expires_at"] == expired_at.isoformat()
    async with uow_factory() as uow:
        expired_row = await uow.editorial_drafts.get_for_update(seeded.subject.id, occurrence_id)
        assert expired_row is not None
        assert await uow.blobs.get(saved.draft.text_blob_ref) is not None
    assert await service.purge_expired() == 1
    assert await service.purge_expired() == 0
    async with uow_factory() as uow:
        assert await uow.editorial_drafts.get_for_update(seeded.subject.id, occurrence_id) is None
        assert await uow.blobs.get(saved.draft.text_blob_ref) is not None
    assert await seeded.blob_store.exists(blob.descriptor)


@pytest.mark.asyncio
async def test_adopt_reject_cancel_purge_server_draft(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Adoption and rejection purge via the decision API; DELETE is the explicit
    # cancellation path. Each fixture gets a fresh edition in the shared DB.
    accepted = await _seed(uow_factory, proposal_count=1)
    accept_app = _app(accepted, uow_factory, monkeypatch)
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            editorial_draft_retention_days=30,
            editorial_proposal_accept_enabled=True,
        ),
    )
    adoption = _service(accepted, uow_factory)
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: adoption
    )
    occurrence_id = accepted.snapshot.block_registry[0].block_id
    accept_path = (
        f"/api/subjects/{accepted.subject.id}/editor/threads/{occurrence_id}/"
        f"proposals/{accepted.proposals[0].id}/accept"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=accept_app), base_url="http://editorial.test"
    ) as client:
        accepted_save = await _put_draft(client, accepted)
        assert accepted_save.status_code == 200, accepted_save.json()
        assert accepted_save.headers["cache-control"] == "no-store"
        async with uow_factory() as uow:
            accepted_draft = await uow.editorial_drafts.get_for_update(
                accepted.subject.id, occurrence_id
            )
            assert accepted_draft is not None
            accepted_blob_id = accepted_draft.text_blob_ref
        response = await client.post(
            accept_path,
            headers={"Idempotency-Key": f"adopt-{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": accepted.patch_sha256},
        )
    assert response.status_code == 200, response.json()
    async with uow_factory() as uow:
        assert await uow.editorial_drafts.get_for_update(accepted.subject.id, occurrence_id) is None
        assert await uow.blobs.get(accepted_blob_id) is not None

    rejected = await _seed(uow_factory, proposal_count=1)
    reject_app = _app(rejected, uow_factory, monkeypatch)
    reject_occurrence = rejected.snapshot.block_registry[0].block_id
    reject_path = (
        f"/api/subjects/{rejected.subject.id}/editor/threads/{reject_occurrence}/"
        f"proposals/{rejected.proposals[0].id}/reject"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=reject_app), base_url="http://editorial.test"
    ) as client:
        rejected_save = await _put_draft(client, rejected)
        assert rejected_save.status_code == 200, rejected_save.json()
        async with uow_factory() as uow:
            rejected_draft = await uow.editorial_drafts.get_for_update(
                rejected.subject.id, reject_occurrence
            )
            assert rejected_draft is not None
            assert rejected_draft.text_blob_ref == accepted_blob_id
        response = await client.post(
            reject_path,
            headers={"Idempotency-Key": f"reject-{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": rejected.patch_sha256},
        )
    assert response.status_code == 200, response.json()
    async with uow_factory() as uow:
        assert (
            await uow.editorial_drafts.get_for_update(rejected.subject.id, reject_occurrence)
            is None
        )

    cancelled = await _seed(uow_factory, proposal_count=1)
    cancel_app = _app(cancelled, uow_factory, monkeypatch)
    cancel_occurrence = cancelled.snapshot.block_registry[0].block_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=cancel_app), base_url="http://editorial.test"
    ) as client:
        cancelled_save = await _put_draft(client, cancelled)
        assert cancelled_save.status_code == 200, cancelled_save.json()
        response = await client.delete(
            f"/api/subjects/{cancelled.subject.id}/editor/blocks/{cancel_occurrence}/draft",
            headers={"If-Match": '"1"'},
        )
    assert response.status_code == 204, response.text
    assert response.headers["cache-control"] == "no-store"
    async with uow_factory() as uow:
        assert (
            await uow.editorial_drafts.get_for_update(cancelled.subject.id, cancel_occurrence)
            is None
        )


@pytest.mark.asyncio
async def test_adoption_success_is_not_reported_as_failure_when_draft_purge_fails(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    app = _app(seeded, uow_factory, monkeypatch)
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(
            _env_file=None,
            editorial_draft_retention_days=30,
            editorial_proposal_accept_enabled=True,
        ),
    )
    adoption = _service(seeded, uow_factory)
    monkeypatch.setattr(
        editorial_workspace_api, "_proposal_adoption_service", lambda _request: adoption
    )
    occurrence_id = seeded.snapshot.block_registry[0].block_id
    accept_path = (
        f"/api/subjects/{seeded.subject.id}/editor/threads/{occurrence_id}/"
        f"proposals/{seeded.proposals[0].id}/accept"
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        saved = await _put_draft(client, seeded)
        assert saved.status_code == 200, saved.json()

        async def fail_purge(
            _service: EditorialDraftService, _subject_id: UUID, _occurrence_id: UUID
        ) -> None:
            raise RuntimeError("synthetic cleanup failure")

        monkeypatch.setattr(EditorialDraftService, "purge", fail_purge)
        response = await client.post(
            accept_path,
            headers={"Idempotency-Key": f"adopt-{uuid4().hex}"},
            json={"expected_state_version": 1, "patch_sha256": seeded.patch_sha256},
        )

    assert response.status_code == 200, response.json()
    async with uow_factory() as uow:
        remaining = await uow.editorial_drafts.get_for_update(seeded.subject.id, occurrence_id)
        assert remaining is not None


@pytest.mark.asyncio
async def test_editorial_prose_is_absent_from_api_errors_and_application_logs(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    secret_text = "private prose 9f47c8d1"

    class _FailingBlobStore(_MemoryBlobStore):
        async def put(
            self,
            source: BytesIO,
            *,
            logical_bucket: str,
            mime_type: str,
        ) -> BlobDescriptor:
            raise RuntimeError(f"test adapter echoed {secret_text}")

    failing_store = _FailingBlobStore()
    app = _app(seeded, uow_factory, monkeypatch)
    app.state.blob_store = failing_store
    occurrence_id = seeded.snapshot.block_registry[0].block_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        path = f"/api/subjects/{seeded.subject.id}/editor/blocks/{occurrence_id}/draft"
        response = await client.put(
            path,
            headers={"If-Match": '"0"'},
            json={"snapshot_id": str(seeded.snapshot.snapshot_id), "text": secret_text},
        )
        invalid = await client.put(
            path,
            headers={"If-Match": '"0"'},
            json={
                "snapshot_id": str(seeded.snapshot.snapshot_id),
                "text": {"untrusted": secret_text},
            },
        )
    assert response.status_code == 503, response.json()
    assert response.headers["cache-control"] == "no-store"
    assert secret_text not in response.text
    assert invalid.status_code == 422, invalid.json()
    assert invalid.headers["cache-control"] == "no-store"
    assert invalid.json()["detail"]["code"] == "draft_request_invalid"
    assert secret_text not in invalid.text
    assert secret_text not in caplog.text


@pytest.mark.asyncio
async def test_unconfigured_retention_fails_closed(
    uow_factory: UnitOfWorkFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seeded = await _seed(uow_factory, proposal_count=1)
    app = _app(seeded, uow_factory, monkeypatch)
    monkeypatch.setattr(
        editorial_workspace_api,
        "get_settings",
        lambda: Settings(_env_file=None, editorial_draft_retention_days=0),
    )
    occurrence_id = seeded.snapshot.block_registry[0].block_id
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await _put_draft(client, seeded)
    assert response.status_code == 503, response.json()
    assert response.json()["detail"]["code"] == "draft_retention_unconfigured"
    assert seeded.blob_store.put_calls == 0
    async with uow_factory() as uow:
        assert await uow.editorial_drafts.get_for_update(seeded.subject.id, occurrence_id) is None


def test_editorial_draft_downgrade_refuses_used_rows(temporary_postgres_url: str) -> None:
    command.upgrade(_alembic_config(temporary_postgres_url), "head")
    engine = create_async_engine(temporary_postgres_url)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)

    class _TemporaryUowFactory:
        def __call__(self) -> UnitOfWork:
            return cast(UnitOfWork, SqlAlchemyUnitOfWork(session_factory))

    uow_factory = cast(UnitOfWorkFactory, _TemporaryUowFactory())

    async def _save_then_attempt_downgrade() -> None:
        try:
            seeded = await _seed(uow_factory, proposal_count=0)
            occurrence_id = seeded.snapshot.block_registry[0].block_id
            service = _draft_service(seeded, uow_factory)
            await service.save(
                subject_id=seeded.subject.id,
                occurrence_id=occurrence_id,
                actor_id=uuid4(),
                snapshot_id=seeded.snapshot.snapshot_id,
                text="Brouillon synthétique utilisé.",
                expected_version=0,
            )
            config = _alembic_config(temporary_postgres_url)

            try:
                await asyncio.to_thread(
                    command.downgrade, config, "0015_editorial_proposal_impacts"
                )
            except RuntimeError as error:
                assert "refusing to downgrade editorial_drafts" in str(error)
            else:
                # If the downgrade unexpectedly succeeds, restore head so the
                # temporary database remains usable after this test.
                await asyncio.to_thread(command.upgrade, config, "head")
                pytest.fail("downgrade unexpectedly removed populated editorial_drafts")
        finally:
            await engine.dispose()

    asyncio.run(_save_then_attempt_downgrade())


def test_editorial_draft_preflight_accepts_exact_existing_table(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "0016_editorial_drafts")

    async def _demote_revision() -> None:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE alembic_version SET version_num = '0015_editorial_proposal_impacts'"
                    )
                )
        finally:
            await engine.dispose()

    asyncio.run(_demote_revision())
    command.upgrade(config, "0016_editorial_drafts")
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == "0016_editorial_drafts"


def test_editorial_draft_preflight_rejects_partial_table_before_schema_changes(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def _demote_and_break_column() -> None:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("ALTER TABLE editorial_drafts ALTER COLUMN actor_id DROP NOT NULL")
                )
                await connection.execute(
                    text(
                        "UPDATE alembic_version SET version_num = '0015_editorial_proposal_impacts'"
                    )
                )
        finally:
            await engine.dispose()

    async def _column_is_nullable() -> bool:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT is_nullable = 'YES' FROM information_schema.columns "
                        "WHERE table_name = 'editorial_drafts' AND column_name = 'actor_id'"
                    )
                )
                return bool(result.scalar_one())
        finally:
            await engine.dispose()

    asyncio.run(_demote_and_break_column())
    with pytest.raises(RuntimeError, match=r"column actor_id: expected .* actual .*"):
        command.upgrade(config, "head")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == (
        "0015_editorial_proposal_impacts"
    )
    assert asyncio.run(_column_is_nullable())


def test_editorial_draft_preflight_rejects_preexisting_trigger(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def _install_trigger_and_demote_revision() -> None:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "CREATE FUNCTION editorial_draft_preflight_probe() "
                        "RETURNS trigger LANGUAGE plpgsql AS $$ "
                        "BEGIN RETURN NEW; END $$"
                    )
                )
                await connection.execute(
                    text(
                        "CREATE TRIGGER editorial_draft_preflight_probe "
                        "BEFORE INSERT ON editorial_drafts FOR EACH ROW "
                        "EXECUTE FUNCTION editorial_draft_preflight_probe()"
                    )
                )
                await connection.execute(
                    text(
                        "UPDATE alembic_version SET version_num = '0015_editorial_proposal_impacts'"
                    )
                )
        finally:
            await engine.dispose()

    async def _trigger_still_exists() -> bool:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_catalog.pg_trigger "
                        "WHERE tgname = 'editorial_draft_preflight_probe' "
                        "AND NOT tgisinternal)"
                    )
                )
                return bool(result.scalar_one())
        finally:
            await engine.dispose()

    asyncio.run(_install_trigger_and_demote_revision())
    with pytest.raises(
        RuntimeError,
        match=(
            r"object triggers: expected absent, actual "
            r"\['editorial_draft_preflight_probe'\]"
        ),
    ):
        command.upgrade(config, "head")

    assert asyncio.run(_alembic_version(temporary_postgres_url)) == (
        "0015_editorial_proposal_impacts"
    )
    assert asyncio.run(_trigger_still_exists())


def test_editorial_draft_preflight_rejects_foreign_key_onupdate_drift(
    temporary_postgres_url: str,
) -> None:
    config = _alembic_config(temporary_postgres_url)
    command.upgrade(config, "head")

    async def _change_onupdate_and_demote_revision() -> None:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.begin() as connection:
                await connection.execute(
                    text("ALTER TABLE editorial_drafts DROP CONSTRAINT fk_editorial_drafts_subject")
                )
                await connection.execute(
                    text(
                        "ALTER TABLE editorial_drafts "
                        "ADD CONSTRAINT fk_editorial_drafts_subject "
                        "FOREIGN KEY (subject_id) REFERENCES subjects (id) "
                        "ON DELETE RESTRICT ON UPDATE CASCADE"
                    )
                )
                await connection.execute(
                    text(
                        "UPDATE alembic_version SET version_num = '0015_editorial_proposal_impacts'"
                    )
                )
        finally:
            await engine.dispose()

    async def _foreign_key_update_action() -> str:
        engine = create_async_engine(temporary_postgres_url)
        try:
            async with engine.connect() as connection:
                result = await connection.execute(
                    text(
                        "SELECT confupdtype::text FROM pg_catalog.pg_constraint "
                        "WHERE conname = 'fk_editorial_drafts_subject'"
                    )
                )
                return str(result.scalar_one())
        finally:
            await engine.dispose()

    asyncio.run(_change_onupdate_and_demote_revision())
    with pytest.raises(
        RuntimeError, match=r"constraint fk_editorial_drafts_subject: expected"
    ) as error:
        command.upgrade(config, "head")

    assert "CASCADE" in str(error.value)
    assert asyncio.run(_alembic_version(temporary_postgres_url)) == (
        "0015_editorial_proposal_impacts"
    )
    assert asyncio.run(_foreign_key_update_action()) == "c"


async def _alembic_version(database_url: str) -> str:
    engine = create_async_engine(database_url)
    try:
        async with engine.connect() as connection:
            result = await connection.execute(text("SELECT version_num FROM alembic_version"))
            return str(result.scalar_one())
    finally:
        await engine.dispose()
