from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any, cast
from unittest.mock import AsyncMock
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import MAX_ARTIFACT_BYTES
from cti_app.application.production_state import (
    MAX_PRODUCTION_STATE_BYTES,
    ProductionStateError,
    ProductionStateService,
    ProductionStateSnapshotV1,
    _validate_snapshot,
    compute_production_state_checksum,
)
from cti_app.domain.production import (
    EditionProductionBatchItem,
    SubjectProductionRun,
    SubjectProductionStatus,
)
from tools.production_state_checksum import canonical_checksum


def _payload() -> dict[str, Any]:
    payload: dict[str, Any] = {
        "format": "autowork.production-state",
        "schema_version": 1,
        "exported_at": "2026-08-26T15:00:00Z",
        "origin": {
            "subject_title": "Titre original",
            "editorial_type": "brief",
            "profile": "brief_auto",
            "research_date": "2026-08-26",
        },
        "artifacts": {
            "references": {
                "input_hash": "a" * 64,
                "canonical_content": {
                    "sources": [
                        {
                            "id": "S1",
                            "title": "Source",
                            "url": "https://example.test/source",
                            "canonical_url": "https://example.test/source",
                        }
                    ],
                    "events": [],
                },
            },
            "extraction": {"input_hash": "b" * 64, "canonical_content": {"items": []}},
            "synthesis": {"input_hash": "c" * 64, "rendered_content": "Fait [S1]"},
        },
        "content_sha256": "0" * 64,
    }
    snapshot = ProductionStateSnapshotV1.model_validate(payload)
    payload["content_sha256"] = compute_production_state_checksum(snapshot)
    return payload


class _FailingFactory:
    def __call__(self) -> Any:
        raise AssertionError("UoW must not be opened for invalid input")


class _ImportUow:
    def __init__(self, current: SubjectProductionRun, item: Any | None) -> None:
        self.subject_production_runs = SimpleNamespace(
            lock_creation_for_subject=AsyncMock(),
            get_current_for_subject=AsyncMock(return_value=current),
            allocate_next_run_number=AsyncMock(return_value=current.run_number + 1),
            add=AsyncMock(),
        )
        self.edition_production_batch_items = SimpleNamespace(
            get_by_run=AsyncMock(return_value=item),
            save=AsyncMock(),
        )
        self.editorial_groups = SimpleNamespace(get_by_subject=AsyncMock(return_value=None))
        self.production_artifacts = SimpleNamespace(append=AsyncMock())
        self.production_input_snapshots = SimpleNamespace(add=AsyncMock())
        self.commit = AsyncMock()

    async def __aenter__(self) -> "_ImportUow":
        return self

    async def __aexit__(self, *_args: Any) -> None:
        return None


class _ImportFactory:
    def __init__(self, uow: _ImportUow) -> None:
        self.uow = uow

    def __call__(self) -> _ImportUow:
        return self.uow


class _ImportArtifactStore:
    async def store_stage_payloads(
        self,
        *,
        canonical: dict[str, Any] | None = None,
        rendered: str | None = None,
    ) -> tuple[Any, Any, Any]:
        if canonical is not None:
            return None, uuid4(), None
        assert rendered is not None
        return None, None, uuid4()


def _import_service(item: Any | None) -> tuple[ProductionStateService, _ImportUow, UUID, UUID]:
    subject_id = uuid4()
    edition_id = uuid4()
    current = SubjectProductionRun(
        subject_id=subject_id,
        edition_id=edition_id,
        status=SubjectProductionStatus.NEEDS_REVIEW,
    )
    uow = _ImportUow(current, item)
    service = ProductionStateService(_ImportFactory(uow), _ImportArtifactStore())
    return service, uow, subject_id, edition_id


@pytest.mark.asyncio
async def test_import_repoints_existing_batch_item_and_resets_auto_recovery() -> None:
    service, uow, subject_id, edition_id = _import_service(None)
    current_run_id = uow.subject_production_runs.get_current_for_subject.return_value.id
    item = EditionProductionBatchItem(
        batch_id=uuid4(),
        subject_id=subject_id,
        production_run_id=current_run_id,
        position=1,
        auto_recovery_count=1,
    )
    uow.edition_production_batch_items.get_by_run.return_value = item

    result = await service.import_state(
        subject_id=subject_id, edition_id=edition_id, payload=_payload()
    )

    assert item.production_run_id == result.run_id
    assert item.auto_recovery_count == 0
    uow.edition_production_batch_items.save.assert_awaited_once_with(item)
    uow.commit.assert_awaited_once()


@pytest.mark.asyncio
async def test_import_without_batch_item_succeeds() -> None:
    service, uow, subject_id, edition_id = _import_service(None)

    result = await service.import_state(
        subject_id=subject_id, edition_id=edition_id, payload=_payload()
    )

    assert result.status == "needs_review"
    uow.edition_production_batch_items.get_by_run.assert_awaited_once()
    uow.edition_production_batch_items.save.assert_not_awaited()


def test_checksum_tool_repairs_edited_snapshot() -> None:
    payload = _payload()
    payload["artifacts"]["synthesis"]["rendered_content"] = "Fait [S1] corrigé par l'analyste"
    payload["content_sha256"] = canonical_checksum(payload)

    snapshot = _validate_snapshot(payload)

    assert snapshot.content_sha256 == compute_production_state_checksum(snapshot)


@pytest.mark.asyncio
async def test_import_accepts_v1_checksum_and_rejects_unknown_fields() -> None:
    payload = _payload()
    snapshot = ProductionStateSnapshotV1.model_validate(payload)
    assert snapshot.content_sha256 == compute_production_state_checksum(snapshot)

    payload["unexpected"] = True
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_invalid"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("field", "value", "code"),
    [
        ("format", "other", "production_state_invalid_format"),
        ("schema_version", 2, "production_state_version_unsupported"),
    ],
)
async def test_import_rejects_format_and_version_before_uow(
    field: str, value: Any, code: str
) -> None:
    payload = _payload()
    payload[field] = value
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_import_rejects_bad_checksum_without_side_effects() -> None:
    payload = _payload()
    payload["content_sha256"] = "d" * 64
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_checksum_mismatch"


def test_checksum_is_deterministic_and_excludes_checksum_field() -> None:
    payload = _payload()
    snapshot = ProductionStateSnapshotV1.model_validate(payload)
    changed = snapshot.model_copy(update={"content_sha256": "e" * 64})
    assert compute_production_state_checksum(snapshot) == compute_production_state_checksum(changed)


def test_snapshot_limits_are_defined() -> None:
    assert MAX_ARTIFACT_BYTES < MAX_PRODUCTION_STATE_BYTES
    assert datetime.now(UTC).tzinfo is not None


@pytest.mark.asyncio
@pytest.mark.parametrize("artifact", ("references", "extraction", "synthesis"))
async def test_import_rejects_each_oversized_artifact_before_creating_a_run(artifact: str) -> None:
    payload = _payload()
    if artifact == "synthesis":
        payload["artifacts"][artifact]["rendered_content"] = "x" * (MAX_ARTIFACT_BYTES + 1)
    else:
        payload["artifacts"][artifact]["canonical_content"]["padding"] = "x" * (
            MAX_ARTIFACT_BYTES + 1
        )
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_too_large"


@pytest.mark.asyncio
async def test_import_rejects_oversized_snapshot_before_creating_a_run() -> None:
    payload = _payload()
    payload["origin"]["subject_title"] = "x" * (MAX_PRODUCTION_STATE_BYTES + 1)
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_too_large"
