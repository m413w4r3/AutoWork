from __future__ import annotations

import asyncio
import hashlib
from contextlib import asynccontextmanager
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from types import SimpleNamespace
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from fastapi import FastAPI

from cti_app.api import editorial_workspace as editorial_workspace_api
from cti_app.application.editorial_workspace.registry import WorkspaceOccurrenceResolution
from cti_app.config import Settings
from cti_app.domain.blobs import BlobDescriptor, BlobRecord
from cti_app.domain.editorial_draft import EditorialDraft
from cti_app.domain.editorial_workspace import EditorialWorkspaceError


class _MemoryBlobStore:
    def __init__(self) -> None:
        self.data: dict[str, bytes] = {}
        self.fail_put: Exception | None = None

    async def put(self, source: BytesIO, *, logical_bucket: str, mime_type: str) -> BlobDescriptor:
        if self.fail_put is not None:
            raise self.fail_put
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        self.data[digest] = content
        return BlobDescriptor(
            sha256=digest,
            size=len(content),
            mime_type=mime_type,
            logical_bucket=logical_bucket,
        )

    async def read(self, descriptor: BlobDescriptor, *, max_bytes: int) -> bytes:
        return self.data[descriptor.sha256]

    async def delete(self, descriptor: BlobDescriptor) -> None:
        self.data.pop(descriptor.sha256, None)


class _MemoryBlobRepository:
    def __init__(self, factory: _MemoryUowFactory) -> None:
        self.factory = factory

    async def lock_address(self, logical_bucket: str, sha256: str) -> None:
        return None

    async def add_if_absent(self, blob: BlobRecord) -> BlobRecord:
        key = (blob.descriptor.logical_bucket, blob.descriptor.sha256)
        canonical = next(
            (
                existing
                for existing in self.factory.blobs_by_address.values()
                if existing.descriptor.sha256 == blob.descriptor.sha256
            ),
            None,
        )
        return canonical or self.factory.blobs_by_address.setdefault(key, blob)

    async def get(self, blob_id: UUID) -> BlobRecord | None:
        return next(
            (blob for blob in self.factory.blobs_by_address.values() if blob.id == blob_id), None
        )

    async def get_by_address(self, logical_bucket: str, sha256: str) -> BlobRecord | None:
        return self.factory.blobs_by_address.get((logical_bucket, sha256))

    async def count_references(self, blob_id: UUID) -> int:
        return sum(draft.text_blob_ref == blob_id for draft in self.factory.drafts.values())

    async def delete(self, blob_id: UUID) -> None:
        for key, blob in tuple(self.factory.blobs_by_address.items()):
            if blob.id == blob_id:
                del self.factory.blobs_by_address[key]


class _MemoryDraftRepository:
    def __init__(self, factory: _MemoryUowFactory) -> None:
        self.factory = factory

    async def lock_for_key(self, subject_id: UUID, occurrence_id: UUID) -> None:
        return None

    async def get_for_update(self, subject_id: UUID, occurrence_id: UUID) -> EditorialDraft | None:
        return self.factory.drafts.get((subject_id, occurrence_id))

    async def save(self, draft: EditorialDraft) -> EditorialDraft:
        if self.factory.draft_save_error is not None:
            raise self.factory.draft_save_error
        key = (draft.subject_id, draft.occurrence_id)
        self.factory.drafts[key] = draft
        return draft

    async def delete(self, draft_id: UUID) -> EditorialDraft | None:
        for key, draft in tuple(self.factory.drafts.items()):
            if draft.id == draft_id:
                del self.factory.drafts[key]
                return draft
        return None

    async def delete_for_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> tuple[EditorialDraft, ...]:
        key = (subject_id, occurrence_id)
        draft = self.factory.drafts.pop(key, None)
        return (draft,) if draft is not None else ()

    async def purge_expired(self, now: datetime, *, limit: int) -> int:
        expired = [key for key, draft in self.factory.drafts.items() if draft.expires_at <= now][
            :limit
        ]
        for key in expired:
            del self.factory.drafts[key]
        return len(expired)


class _MemoryAdoptionLocks:
    def __init__(self, factory: _MemoryUowFactory) -> None:
        self.factory = factory

    async def lock_adoption_for_run(self, _run_id: UUID) -> None:
        self.factory.adoption_locks += 1


class _MemoryProductionRuns:
    def __init__(self, factory: _MemoryUowFactory) -> None:
        self.factory = factory

    async def get_for_update(self, _run_id: UUID) -> object:
        self.factory.run_row_locks += 1
        return object()


class _MemoryUow:
    def __init__(self, factory: _MemoryUowFactory) -> None:
        self.blobs = _MemoryBlobRepository(factory)
        self.editorial_drafts = _MemoryDraftRepository(factory)
        self.editorial_proposals = _MemoryAdoptionLocks(factory)
        self.production_runs = _MemoryProductionRuns(factory)

    async def commit(self) -> None:
        return None


class _MemoryUowFactory:
    def __init__(self) -> None:
        self.drafts: dict[tuple[UUID, UUID], EditorialDraft] = {}
        self.blobs_by_address: dict[tuple[str, str], BlobRecord] = {}
        self.adoption_locks = 0
        self.run_row_locks = 0
        self.draft_save_error: Exception | None = None

    @asynccontextmanager
    async def __call__(self):
        yield _MemoryUow(self)


class _MemoryWorkspace:
    def __init__(self, subject_id: UUID) -> None:
        self.subject_id = subject_id
        self.production_run_id = uuid4()
        self.snapshot_id = uuid4()
        self.artifact_id = uuid4()
        self.artifact_version = 3
        self.canonical_sha256 = "a" * 64

    async def resolve_occurrence(
        self, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        if subject_id != self.subject_id:
            raise EditorialWorkspaceError("subject_not_found")
        return cast(
            WorkspaceOccurrenceResolution,
            SimpleNamespace(
                record=SimpleNamespace(
                    snapshot_id=self.snapshot_id,
                    subject_id=subject_id,
                    production_run_id=self.production_run_id,
                ),
                entry=SimpleNamespace(block_id=occurrence_id),
                binding_state="valid",
                artifact_id=self.artifact_id,
                artifact_version=self.artifact_version,
                canonical_sha256=self.canonical_sha256,
            ),
        )

    async def resolve_occurrence_in_uow(
        self, _uow: object, subject_id: UUID, occurrence_id: UUID
    ) -> WorkspaceOccurrenceResolution:
        return await self.resolve_occurrence(subject_id, occurrence_id)


def _run(awaitable):
    return asyncio.run(awaitable)


def test_draft_subject_acl_and_version_fence() -> None:
    from cti_app.application.editorial_drafts import (
        EditorialDraftApplicationError,
        EditorialDraftService,
    )

    subject_id = uuid4()
    occurrence_id = uuid4()
    actor_id = uuid4()
    uow_factory = _MemoryUowFactory()
    blob_store = _MemoryBlobStore()
    workspace = _MemoryWorkspace(subject_id)
    service = EditorialDraftService(
        uow_factory=uow_factory,
        workspace_service=workspace,
        blob_store=blob_store,
        retention_days=30,
    )
    saved = _run(
        service.save(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=actor_id,
            snapshot_id=workspace.snapshot_id,
            text="texte réservé",
            expected_version=0,
        )
    )
    assert saved.draft.version == 1
    assert uow_factory.adoption_locks == 1
    assert uow_factory.run_row_locks == 1
    with pytest.raises(EditorialDraftApplicationError, match="draft_version_conflict"):
        _run(
            service.save(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                actor_id=actor_id,
                snapshot_id=workspace.snapshot_id,
                text="écrasement interdit",
                expected_version=0,
            )
        )
    workspace.snapshot_id = uuid4()
    workspace.production_run_id = uuid4()
    workspace.artifact_version += 1
    stale_read = _run(service.get(subject_id=subject_id, occurrence_id=occurrence_id))
    assert stale_read.is_stale
    with pytest.raises(EditorialDraftApplicationError, match="draft_base_stale"):
        _run(
            service.save(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                actor_id=actor_id,
                snapshot_id=workspace.snapshot_id,
                text="base dépassée",
                expected_version=1,
            )
        )
    with pytest.raises(EditorialDraftApplicationError, match="draft_not_found"):
        _run(service.get(subject_id=uuid4(), occurrence_id=occurrence_id))


def test_save_storage_error_keeps_safe_cause_and_logs_only_ids_and_class(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cti_app.application.editorial_drafts import (
        EditorialDraftApplicationError,
        EditorialDraftService,
    )

    subject_id, occurrence_id = uuid4(), uuid4()
    snapshot_id = uuid4()
    private_text = "brouillon privé 9f47c8d1"
    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    workspace = _MemoryWorkspace(subject_id)
    workspace.snapshot_id = snapshot_id
    factory.draft_save_error = RuntimeError(f"adapter leaked {private_text}")
    service = EditorialDraftService(
        uow_factory=factory,
        workspace_service=workspace,
        blob_store=blobs,
        retention_days=30,
    )

    with pytest.raises(EditorialDraftApplicationError) as raised:
        _run(
            service.save(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                actor_id=uuid4(),
                snapshot_id=snapshot_id,
                text=private_text,
                expected_version=0,
            )
        )

    assert isinstance(raised.value.__cause__, RuntimeError)
    assert "RuntimeError" in caplog.text
    assert str(subject_id) in caplog.text
    assert str(occurrence_id) in caplog.text
    assert str(snapshot_id) in caplog.text
    assert private_text not in caplog.text
    assert "adapter leaked" not in caplog.text


def test_draft_ttl_defaults_to_30_days_after_last_change_and_is_configurable() -> None:
    from cti_app.domain.editorial_draft import draft_expiry

    updated_at = datetime(2026, 1, 1, tzinfo=UTC)
    assert Settings(_env_file=None).editorial_draft_retention_days == 30
    invalid_setting = Settings(
        _env_file=None,
        editorial_draft_retention_days=cast(int | None, "invalid"),
    )
    assert invalid_setting.editorial_draft_retention_days is None
    assert draft_expiry(updated_at, 30) == updated_at + timedelta(days=30)
    assert draft_expiry(updated_at, 7) == updated_at + timedelta(days=7)


def test_expired_draft_is_unavailable_and_purged() -> None:
    from cti_app.application.editorial_drafts import (
        EditorialDraftApplicationError,
        EditorialDraftService,
    )

    subject_id, occurrence_id = uuid4(), uuid4()
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    workspace = _MemoryWorkspace(subject_id)
    service = EditorialDraftService(
        uow_factory=factory,
        workspace_service=workspace,
        blob_store=blobs,
        retention_days=1,
        clock=lambda: clock[0],
    )
    _run(
        service.save(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=workspace.snapshot_id,
            text="brouillon expiré",
            expected_version=0,
        )
    )
    clock[0] += timedelta(days=2)
    with pytest.raises(EditorialDraftApplicationError, match="draft_expired"):
        _run(service.get(subject_id=subject_id, occurrence_id=occurrence_id))
    assert factory.drafts[(subject_id, occurrence_id)].expires_at < clock[0]
    assert blobs.data
    assert _run(service.purge_expired()) == 1
    assert factory.drafts == {}
    assert blobs.data

    saved = _run(
        service.save(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=workspace.snapshot_id,
            text="expiration pendant un PUT",
            expected_version=0,
        )
    )
    key = (subject_id, occurrence_id)
    factory.drafts[key] = replace(
        saved.draft,
        updated_at=clock[0] - timedelta(days=2),
        expires_at=clock[0] - timedelta(days=1),
    )
    with pytest.raises(EditorialDraftApplicationError, match="draft_expired"):
        _run(
            service.save(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                actor_id=uuid4(),
                snapshot_id=workspace.snapshot_id,
                text="ne pas recréer après expiration",
                expected_version=1,
            )
        )
    assert factory.drafts == {}
    assert blobs.data


def test_adopt_reject_cancel_purge_server_draft() -> None:
    from cti_app.application.editorial_drafts import EditorialDraftService

    subject_id, occurrence_id = uuid4(), uuid4()
    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    workspace = _MemoryWorkspace(subject_id)
    service = EditorialDraftService(
        uow_factory=factory,
        workspace_service=workspace,
        blob_store=blobs,
        retention_days=30,
    )
    _run(
        service.save(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=workspace.snapshot_id,
            text="brouillon à purger",
            expected_version=0,
        )
    )
    _run(service.purge(subject_id, occurrence_id))
    assert factory.drafts == {}
    assert blobs.data


def test_purge_expired_removes_sql_row_and_keeps_blob_bytes_for_offline_gc() -> None:
    from cti_app.application.editorial_drafts import EditorialDraftService

    subject_id, occurrence_id = uuid4(), uuid4()
    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    workspace = _MemoryWorkspace(subject_id)
    clock = [datetime(2026, 1, 1, tzinfo=UTC)]
    service = EditorialDraftService(
        uow_factory=factory,
        workspace_service=workspace,
        blob_store=blobs,
        retention_days=1,
        clock=lambda: clock[0],
    )
    saved = _run(
        service.save(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=workspace.snapshot_id,
            text="contenu partagé",
            expected_version=0,
        )
    )
    clock[0] += timedelta(days=2)

    assert _run(service.purge_expired()) == 1
    assert _run(service.purge_expired()) == 0
    # "Purged" removes the SQL draft reference; blob bytes remain for offline GC.
    assert (subject_id, occurrence_id) not in factory.drafts
    assert blobs.data[saved.draft.text_sha256] == "contenu partagé".encode()


def test_save_reuses_the_database_canonical_blob_id_for_a_shared_address() -> None:
    from cti_app.application.editorial_drafts import EditorialDraftService

    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    first_subject, second_subject = uuid4(), uuid4()
    occurrence_id = uuid4()
    first_workspace = _MemoryWorkspace(first_subject)
    second_workspace = _MemoryWorkspace(second_subject)
    first = EditorialDraftService(
        uow_factory=factory,
        workspace_service=first_workspace,
        blob_store=blobs,
        retention_days=30,
    )
    second = EditorialDraftService(
        uow_factory=factory,
        workspace_service=second_workspace,
        blob_store=blobs,
        retention_days=30,
    )
    first_saved = _run(
        first.save(
            subject_id=first_subject,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=first_workspace.snapshot_id,
            text="same content address",
            expected_version=0,
        )
    )
    second_saved = _run(
        second.save(
            subject_id=second_subject,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=second_workspace.snapshot_id,
            text="same content address",
            expected_version=0,
        )
    )

    assert second_saved.draft.text_blob_ref == first_saved.draft.text_blob_ref
    assert _run(second.get(subject_id=second_subject, occurrence_id=occurrence_id)).text == (
        "same content address"
    )


def test_save_reuses_content_canonical_blob_across_bucket_and_mime() -> None:
    from cti_app.application.editorial_drafts import EditorialDraftService

    subject_id, occurrence_id = uuid4(), uuid4()
    text = "canonical bytes already catalogued elsewhere"
    encoded = text.encode("utf-8")
    sha256 = hashlib.sha256(encoded).hexdigest()
    canonical = BlobRecord(
        descriptor=BlobDescriptor(
            sha256=sha256,
            size=len(encoded),
            mime_type="application/octet-stream",
            logical_bucket="shared-content",
        )
    )
    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    factory.blobs_by_address[("shared-content", sha256)] = canonical
    workspace = _MemoryWorkspace(subject_id)
    service = EditorialDraftService(
        uow_factory=factory,
        workspace_service=workspace,
        blob_store=blobs,
        retention_days=30,
    )

    saved = _run(
        service.save(
            subject_id=subject_id,
            occurrence_id=occurrence_id,
            actor_id=uuid4(),
            snapshot_id=workspace.snapshot_id,
            text=text,
            expected_version=0,
        )
    )

    assert saved.draft.text_blob_ref == canonical.id
    assert _run(service.get(subject_id=subject_id, occurrence_id=occurrence_id)).text == text


def test_editorial_prose_and_block_text_never_enter_logs_traces_or_error_telemetry(
    caplog: pytest.LogCaptureFixture,
) -> None:
    from cti_app.application.editorial_drafts import (
        EditorialDraftApplicationError,
        EditorialDraftService,
    )

    secret_text = "bloc privé 4ff6a19d"
    subject_id, occurrence_id = uuid4(), uuid4()
    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    blobs.fail_put = RuntimeError(f"backend echoed {secret_text}")
    workspace = _MemoryWorkspace(subject_id)
    service = EditorialDraftService(
        uow_factory=factory,
        workspace_service=workspace,
        blob_store=blobs,
        retention_days=30,
    )
    with pytest.raises(EditorialDraftApplicationError) as error:
        _run(
            service.save(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                actor_id=uuid4(),
                snapshot_id=workspace.snapshot_id,
                text=secret_text,
                expected_version=0,
            )
        )
    assert isinstance(error.value.__cause__, RuntimeError)
    assert "RuntimeError" in caplog.text
    assert str(subject_id) in caplog.text
    assert str(occurrence_id) in caplog.text
    assert str(workspace.snapshot_id) in caplog.text
    assert secret_text not in str(error.value)
    assert secret_text not in caplog.text
    assert "backend echoed" not in caplog.text


def test_unconfigured_retention_fails_closed() -> None:
    from cti_app.application.editorial_drafts import (
        EditorialDraftApplicationError,
        EditorialDraftService,
    )

    subject_id, occurrence_id = uuid4(), uuid4()
    factory, blobs = _MemoryUowFactory(), _MemoryBlobStore()
    workspace = _MemoryWorkspace(subject_id)
    service = EditorialDraftService(
        uow_factory=factory,
        workspace_service=workspace,
        blob_store=blobs,
        retention_days=0,
    )
    with pytest.raises(EditorialDraftApplicationError, match="draft_retention_unconfigured"):
        _run(
            service.save(
                subject_id=subject_id,
                occurrence_id=occurrence_id,
                actor_id=uuid4(),
                snapshot_id=workspace.snapshot_id,
                text="ne doit pas être persisté",
                expected_version=0,
            )
        )
    assert blobs.data == {}
    assert (
        Settings(
            _env_file=None, editorial_draft_retention_days="invalid"
        ).editorial_draft_retention_days
        is None
    )


@pytest.mark.asyncio
async def test_decision_draft_purge_failure_does_not_escape_after_commit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    class _FailingDraftService:
        async def purge(self, _subject_id: UUID, _occurrence_id: UUID) -> None:
            raise RuntimeError("private cleanup failure 4d8f")

    app = FastAPI()
    app.state.blob_store = object()
    from starlette.requests import Request

    scope = {"type": "http", "app": app, "headers": [], "method": "POST", "path": "/"}
    http_request = Request(scope)
    monkeypatch.setattr(
        editorial_workspace_api, "_draft_service", lambda _request: _FailingDraftService()
    )

    subject_id, occurrence_id = uuid4(), uuid4()
    caplog.set_level("WARNING", logger=editorial_workspace_api.__name__)
    await editorial_workspace_api._purge_decided_server_draft(
        http_request, subject_id, occurrence_id
    )

    assert "RuntimeError" in caplog.text
    assert str(subject_id) in caplog.text
    assert str(occurrence_id) in caplog.text
    assert "private cleanup failure" not in caplog.text
    assert "4d8f" not in caplog.text


@pytest.mark.asyncio
async def test_draft_route_logs_unexpected_exception_without_exception_message(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:

    secret_message = "private draft route failure 3a7e"

    class _FailingDraftService:
        async def get(self, **_kwargs: object) -> None:
            raise RuntimeError(secret_message)

    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    monkeypatch.setattr(
        editorial_workspace_api, "_draft_service", lambda _request: _FailingDraftService()
    )
    subject_id, occurrence_id = uuid4(), uuid4()
    caplog.set_level("WARNING", logger=editorial_workspace_api.__name__)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        response = await client.get(
            f"/api/subjects/{subject_id}/editor/threads/{occurrence_id}/draft"
        )

    assert response.status_code == 503
    assert response.headers["cache-control"] == "no-store"
    assert response.json()["detail"]["code"] == "draft_storage_unavailable"
    assert "RuntimeError" in caplog.text
    assert str(subject_id) in caplog.text
    assert str(occurrence_id) in caplog.text
    assert secret_message not in caplog.text


@pytest.mark.asyncio
async def test_oversized_stream_chunk_is_rejected_before_body_extension(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from fastapi import HTTPException
    from starlette.requests import Request

    class _TrackedBody:
        def __len__(self) -> int:
            return 0

        def extend(self, _chunk: bytes) -> None:
            raise AssertionError("oversized stream chunk was copied before its size check")

    async def receive() -> dict[str, object]:
        return {
            "type": "http.request",
            "body": b"x" * (editorial_workspace_api._MAX_DRAFT_REQUEST_BYTES + 1),
            "more_body": False,
        }

    scope = {
        "type": "http",
        "method": "PUT",
        "path": "/api/subjects/test/editor/blocks/test/draft",
        "headers": [],
        "query_string": b"",
        "path_params": {},
    }
    request = Request(scope, receive=receive)
    monkeypatch.setattr(editorial_workspace_api, "bytearray", _TrackedBody, raising=False)

    with pytest.raises(HTTPException) as error:
        await editorial_workspace_api._read_draft_put_payload(request)

    assert error.value.status_code == 413


@pytest.mark.asyncio
async def test_draft_errors_are_not_cacheable_and_body_limit_precedes_json_parsing() -> None:
    app = FastAPI()
    app.include_router(editorial_workspace_api.router)
    path = f"/api/subjects/{uuid4()}/editor/blocks/{uuid4()}/draft"
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://editorial.test"
    ) as client:
        oversized = await client.put(
            path,
            headers={
                "If-Match": '"0"',
                "Content-Length": str(editorial_workspace_api._MAX_DRAFT_REQUEST_BYTES + 1),
            },
            content=b"{}",
        )
        invalid_precondition = await client.put(path, headers={"If-Match": '"0"'}, json={})

    assert oversized.status_code == 413
    assert oversized.headers["cache-control"] == "no-store"
    assert oversized.json()["detail"]["code"] == "draft_text_too_large"
    assert invalid_precondition.status_code == 422
    assert invalid_precondition.headers["cache-control"] == "no-store"
