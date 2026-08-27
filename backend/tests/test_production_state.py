from datetime import UTC, datetime
from typing import Any, cast
from uuid import uuid4

import pytest

from cti_app.application.production_artifact_store import MAX_ARTIFACT_BYTES
from cti_app.application.production_state import (
    MAX_PRODUCTION_STATE_BYTES,
    ProductionStateError,
    ProductionStateService,
    ProductionStateSnapshotV1,
    compute_production_state_checksum,
)
from cti_app.domain.production import ProductionProfile


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


@pytest.mark.asyncio
async def test_import_accepts_v1_checksum_and_rejects_unknown_fields() -> None:
    payload = _payload()
    snapshot = ProductionStateSnapshotV1.model_validate(payload)
    assert snapshot.content_sha256 == compute_production_state_checksum(snapshot)

    payload["unexpected"] = True
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(
            _FailingFactory(), cast(Any, object())
        ).import_state(
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
        await ProductionStateService(
            _FailingFactory(), cast(Any, object())
        ).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == code


@pytest.mark.asyncio
async def test_import_rejects_bad_checksum_without_side_effects() -> None:
    payload = _payload()
    payload["content_sha256"] = "d" * 64
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(
            _FailingFactory(), cast(Any, object())
        ).import_state(
            subject_id=uuid4(), edition_id=uuid4(), payload=payload
        )
    assert exc_info.value.code == "production_state_checksum_mismatch"


@pytest.mark.asyncio
async def test_major_import_requires_frozen_research_date_before_side_effects() -> None:
    payload = _payload()
    payload["origin"]["research_date"] = None
    snapshot = ProductionStateSnapshotV1.model_validate(payload)
    payload["content_sha256"] = compute_production_state_checksum(snapshot)
    with pytest.raises(ProductionStateError) as exc_info:
        await ProductionStateService(_FailingFactory(), cast(Any, object())).import_state(
            subject_id=uuid4(),
            edition_id=uuid4(),
            payload=payload,
            profile=ProductionProfile.MAJOR_ASSISTED,
        )
    assert exc_info.value.code == "production_state_research_date_required"


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
