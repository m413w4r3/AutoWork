from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from cti_app.domain.production import SubjectProductionStage, SubjectProductionStatus
from cti_app.infrastructure.database.repositories.production import (
    SqlAlchemyBatchStatusReadRepository,
)


class _MappingResult:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self._rows = rows

    def mappings(self) -> list[dict[str, object]]:
        return self._rows


class _Session:
    def __init__(self, rows: list[dict[str, object]]) -> None:
        self.rows = rows
        self.statements: list[object] = []

    async def execute(self, statement: object) -> _MappingResult:
        self.statements.append(statement)
        return _MappingResult(self.rows)


@pytest.mark.asyncio
async def test_batch_status_sql_read_model_uses_one_query_for_many_items() -> None:
    subject_ids = [uuid4() for _ in range(3)]
    run_ids = [uuid4() for _ in range(3)]
    rows = [
        {
            "position": position,
            "subject_id": subject_id,
            "title": f"Subject {position}",
            "run_id": run_id,
            "status": "running",
            "current_stage": "sources",
            "pipeline_generation": position,
            "auto_recovery_count": 0,
            "error_code": None,
            "error_message": None,
        }
        for position, (subject_id, run_id) in enumerate(zip(subject_ids, run_ids, strict=True), 1)
    ]
    session = _Session(cast(list[dict[str, object]], rows))
    repository = SqlAlchemyBatchStatusReadRepository(cast(AsyncSession, session))

    result = await repository.list_for_batch(uuid4())

    assert len(session.statements) == 1
    assert [item.position for item in result] == [1, 2, 3]
    assert [item.subject_id for item in result] == subject_ids
    assert [item.pipeline_generation for item in result] == [1, 2, 3]
    assert [item.status for item in result] == [SubjectProductionStatus.RUNNING] * 3
    assert [item.current_stage for item in result] == [SubjectProductionStage.SOURCES] * 3
