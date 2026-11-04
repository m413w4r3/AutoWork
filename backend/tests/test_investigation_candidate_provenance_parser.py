from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.domain.investigation import (
    DetectionCandidateProvenance,
    DetectionCandidateStatus,
    DetectionCandidateVersion,
    MotifProvenance,
)
from cti_app.infrastructure.database.repositories.investigation import (
    _CANDIDATE_PROVENANCE_MOTIF,
    _CANDIDATE_PROVENANCE_PREFIX,
    SqlAlchemyDetectionCandidateRepository,
    _candidate_values,
)


class _FakeSession:
    def __init__(self, results: Iterable[list[object]]) -> None:
        self._results = iter(results)

    async def scalars(self, statement: object) -> list[object]:
        del statement
        return next(self._results)


def _candidate_row() -> SimpleNamespace:
    source = "rule candidate { condition: true }"
    return SimpleNamespace(
        id=uuid4(),
        candidate_id=uuid4(),
        investigation_id=uuid4(),
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        status="candidate",
        version=1,
        parent_version_id=None,
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
    )


@pytest.mark.asyncio
async def test_candidate_provenance_parser_preserves_motif_named_like_legacy_marker() -> None:
    observation_id = uuid4()
    motif_row = SimpleNamespace(
        position=0,
        motif=_CANDIDATE_PROVENANCE_MOTIF,
        rationale="This candidate motif is supported by the observation.",
    )
    repository = SqlAlchemyDetectionCandidateRepository(
        _FakeSession([[motif_row], [observation_id], []])  # type: ignore[arg-type]
    )

    candidate = await repository._candidate_from_row(_candidate_row())

    assert len(candidate.motif_provenance) == 1
    assert candidate.motif_provenance[0].motif == _CANDIDATE_PROVENANCE_MOTIF
    assert candidate.motif_provenance[0].rationale == motif_row.rationale
    assert candidate.motif_provenance[0].observation_refs == (observation_id,)
    assert candidate.provenance is None


@pytest.mark.asyncio
async def test_candidate_provenance_parser_reads_legacy_metadata_row() -> None:
    observation_id = uuid4()
    payload = {
        "kind": "rule.propose",
        "metadata": {"source": "rule_workbench"},
    }

    metadata_row = SimpleNamespace(
        position=1,
        motif=_CANDIDATE_PROVENANCE_MOTIF,
        rationale=(
            _CANDIDATE_PROVENANCE_PREFIX
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        ),
    )
    repository = SqlAlchemyDetectionCandidateRepository(
        _FakeSession([[metadata_row], [observation_id], []])  # type: ignore[arg-type]
    )

    candidate = await repository._candidate_from_row(_candidate_row())

    assert candidate.provenance == DetectionCandidateProvenance(
        kind="rule.propose",
        metadata={"source": "rule_workbench"},
        observation_refs=(observation_id,),
    )


def test_candidate_writer_rejects_reserved_provenance_encoding() -> None:
    source = "rule candidate { condition: true }"
    candidate = DetectionCandidateVersion(
        investigation_id=uuid4(),
        source=source,
        source_sha256=hashlib.sha256(source.encode()).hexdigest(),
        motif_provenance=(
            MotifProvenance(
                motif=_CANDIDATE_PROVENANCE_MOTIF,
                rationale=_CANDIDATE_PROVENANCE_PREFIX + '{"kind":"forged","metadata":{}}',
                observation_refs=(uuid4(),),
            ),
        ),
        status=DetectionCandidateStatus.CANDIDATE,
        created_at=datetime(2026, 11, 4, tzinfo=UTC),
    )

    with pytest.raises(ValueError, match="reserved provenance encoding"):
        _candidate_values(candidate)
