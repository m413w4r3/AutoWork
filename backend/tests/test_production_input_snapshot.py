"""Domain invariants of the immutable ProductionInputSnapshot (AW-009)."""

from __future__ import annotations

import dataclasses
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production import (
    ProductionInputSnapshot,
    ProductionInputSource,
    ProductionRun,
)

_CANDIDATE_A = UUID("00000000-0000-4000-8000-00000000000a")
_CANDIDATE_B = UUID("00000000-0000-4000-8000-00000000000b")


def _source(candidate_id: UUID, url: str, *, role: SourceRole = SourceRole.PRIMARY) -> Any:
    return ProductionInputSource(
        discovery_candidate_id=candidate_id,
        source_candidate_id=uuid4(),
        canonical_url=url,
        role=role,
        title=f"Report {url}",
        publisher="Vendor",
        published_at=date(2026, 8, 3),
        tlp=TLP.CLEAR,
        sensitivity="public",
        external_llm_allowed=True,
        discovery_batch_id=uuid4(),
    )


_SOURCES = (
    _source(_CANDIDATE_B, "https://b.example/report", role=SourceRole.RELAY),
    _source(_CANDIDATE_A, "https://a.example/report"),
)


def _snapshot(**overrides: Any) -> ProductionInputSnapshot:
    values: dict[str, Any] = {
        "production_run_id": uuid4(),
        "edition_id": UUID("00000000-0000-4000-8000-0000000000e1"),
        "subject_id": UUID("00000000-0000-4000-8000-0000000000f1"),
        "subject_version": 2,
        "subject_title": "MuddyWater activity",
        "subject_tlp": TLP.AMBER,
        "selection_decision_id": UUID("00000000-0000-4000-8000-0000000000d1"),
        "origin_discovery_subject_id": UUID("00000000-0000-4000-8000-0000000000c1"),
        "canonical_discovery_subject_id": UUID("00000000-0000-4000-8000-0000000000c2"),
        "discovery_snapshot_id": UUID("00000000-0000-4000-8000-0000000000b1"),
        "discovery_snapshot_version": 7,
        "member_candidate_ids": (_CANDIDATE_B, _CANDIDATE_A),
        "discovery_summary": "Résumé Discovery",
        "actor_or_campaign": "MuddyWater",
        "period_start": date(2026, 8, 1),
        "period_end": date(2026, 8, 31),
        "research_date": date(2026, 8, 20),
        "core_sources": _SOURCES,
    }
    values.update(overrides)
    return ProductionInputSnapshot(**values)


def test_snapshot_carries_no_editorial_group_identity() -> None:
    names = {field.name for field in dataclasses.fields(ProductionInputSnapshot)}

    assert not any("editorial" in name or "group" in name for name in names)
    assert {
        "origin_discovery_subject_id",
        "canonical_discovery_subject_id",
        "discovery_snapshot_id",
        "discovery_snapshot_version",
        "member_candidate_ids",
    } <= names


def test_snapshot_is_frozen() -> None:
    snapshot = _snapshot()

    with pytest.raises(dataclasses.FrozenInstanceError):
        snapshot.subject_title = "Renamed"  # type: ignore[misc]


def test_candidate_ids_and_sources_are_ordered_deterministically() -> None:
    forward = _snapshot()
    backward = _snapshot(
        member_candidate_ids=(_CANDIDATE_A, _CANDIDATE_B),
        core_sources=tuple(reversed(_SOURCES)),
    )

    assert forward.member_candidate_ids == (_CANDIDATE_A, _CANDIDATE_B)
    assert [source.canonical_url for source in forward.core_sources] == [
        "https://a.example/report",
        "https://b.example/report",
    ]
    assert forward.core_sources == backward.core_sources
    assert forward.input_hash == backward.input_hash
    assert forward.reuse_basis_hash == backward.reuse_basis_hash


def test_hashes_ignore_technical_identities() -> None:
    first = _snapshot(captured_at=datetime(2026, 8, 20, 8, tzinfo=UTC))
    second = _snapshot(
        id=uuid4(),
        production_run_id=uuid4(),
        captured_at=datetime(2026, 8, 21, 17, tzinfo=UTC),
        core_sources=tuple(
            dataclasses.replace(source, discovery_batch_id=uuid4()) for source in _SOURCES
        ),
    )

    assert len(first.input_hash) == len(first.reuse_basis_hash) == 64
    assert first.input_hash == second.input_hash
    assert first.reuse_basis_hash == second.reuse_basis_hash


@pytest.mark.parametrize(
    "overrides",
    [
        {"subject_title": "Campagne MuddyWater 2026"},
        {"subject_version": 3},
        {"subject_tlp": TLP.RED},
        {"discovery_snapshot_version": 8},
        {"canonical_discovery_subject_id": uuid4()},
        {"member_candidate_ids": (_CANDIDATE_A, _CANDIDATE_B, uuid4())},
        {"period_end": date(2026, 9, 30)},
        {"core_sources": (_SOURCES[0],)},
        {
            "core_sources": (
                dataclasses.replace(_SOURCES[0], external_llm_allowed=False),
                _SOURCES[1],
            )
        },
    ],
)
def test_functional_changes_change_both_hashes(overrides: dict[str, Any]) -> None:
    baseline = _snapshot()
    changed = _snapshot(**overrides)

    assert changed.input_hash != baseline.input_hash
    assert changed.reuse_basis_hash != baseline.reuse_basis_hash


def test_research_date_is_part_of_the_input_but_not_the_reuse_basis() -> None:
    baseline = _snapshot()
    later = _snapshot(research_date=date(2026, 8, 25))

    assert later.input_hash != baseline.input_hash
    assert later.reuse_basis_hash == baseline.reuse_basis_hash


@pytest.mark.parametrize(
    ("overrides", "message"),
    [
        ({"subject_version": 0}, "subject_version"),
        ({"discovery_snapshot_version": 0}, "discovery_snapshot_version"),
        ({"period_start": date(2026, 9, 1)}, "period must be ordered"),
        ({"subject_title": "  "}, "subject title"),
        ({"captured_at": datetime(2026, 8, 20)}, "timezone-aware"),
        ({"member_candidate_ids": (_CANDIDATE_A, _CANDIDATE_A)}, "duplicates"),
        ({"member_candidate_ids": (_CANDIDATE_A,)}, "belong to member_candidate_ids"),
        ({"input_hash": "0" * 64}, "input_hash does not match"),
        ({"reuse_basis_hash": "0" * 64}, "reuse_basis_hash does not match"),
    ],
)
def test_invalid_snapshots_are_rejected(overrides: dict[str, Any], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _snapshot(**overrides)


def test_duplicate_source_urls_are_rejected() -> None:
    duplicate = dataclasses.replace(_SOURCES[1], source_candidate_id=uuid4())

    with pytest.raises(ValueError, match="duplicate URLs"):
        _snapshot(core_sources=(*_SOURCES, duplicate))


def test_persisted_hashes_round_trip() -> None:
    snapshot = _snapshot()

    restored = _snapshot(
        production_run_id=snapshot.production_run_id,
        input_hash=snapshot.input_hash,
        reuse_basis_hash=snapshot.reuse_basis_hash,
        core_sources=tuple(
            ProductionInputSource.from_payload(source.payload()) for source in snapshot.core_sources
        ),
    )

    assert restored.input_hash == snapshot.input_hash


@pytest.mark.parametrize("field", ["created_at", "updated_at", "started_at", "finished_at"])
def test_production_run_timestamps_must_be_timezone_aware(field: str) -> None:
    with pytest.raises(ValueError, match=f"{field} must be timezone-aware"):
        ProductionRun(subject_id=uuid4(), edition_id=uuid4(), **{field: datetime(2026, 8, 1)})


def test_production_run_freezes_its_research_date_at_creation() -> None:
    run = ProductionRun(
        subject_id=uuid4(),
        edition_id=uuid4(),
        created_at=datetime(2026, 8, 20, 23, 59, tzinfo=UTC),
    )

    run.start_running(now=datetime(2026, 8, 21, 0, 1, tzinfo=UTC))

    assert run.research_date == date(2026, 8, 20)
