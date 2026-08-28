from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from cti_app.infrastructure.database.repositories.core import (
    SqlAlchemyCapabilitySetRepository,
    SqlAlchemyCodeFeatureSetRepository,
    SqlAlchemySampleFeatureSetRepository,
)


class _InsertResult:
    rowcount = 1


class _Session:
    def __init__(self, copy_driver: object | None = None) -> None:
        self.scalar_calls = 0
        self.execute_calls = 0
        self.batches: list[list[dict[str, object]]] = []
        self.copy_driver = copy_driver

    async def scalar(self, statement: object) -> SimpleNamespace:
        self.scalar_calls += 1
        return SimpleNamespace(id=uuid4())

    async def execute(
        self, statement: object, parameters: list[dict[str, object]] | None = None
    ) -> _InsertResult:
        self.execute_calls += 1
        if parameters is not None:
            self.batches.append(list(parameters))
        return _InsertResult()

    async def flush(self) -> None:
        return None

    def add(self, value: object) -> None:
        raise AssertionError("indexing must use Core execute/insert")


@pytest.mark.asyncio
async def test_index_paths_deduplicate_and_do_one_owner_lookup() -> None:
    sample_id = uuid4()

    static_session = _Session()
    static_repository = SqlAlchemySampleFeatureSetRepository(static_session)
    static_set = SimpleNamespace(
        sample_id=sample_id,
        extractor_version="static-v1",
        parameters_sha256="parameters",
        strings=[
            {"value": "Alpha", "occurrence_count": 7},
            {"value": "alpha", "occurrence_count": 3},
            *[
                {"value": f"value-{index}", "occurrence_count": 1}
                for index in range(1000)
            ],
        ],
        imports=(),
        exports=(),
        sections=(),
        imphash=None,
        opcode_fragment16=(),
    )
    await static_repository.index(static_set)

    assert static_session.scalar_calls == 1
    assert [len(batch) for batch in static_session.batches] == [1000, 1]
    static_rows = [row for batch in static_session.batches for row in batch]
    assert len(static_rows) == 1001
    alpha = next(row for row in static_rows if row["normalized_value"] == "alpha")
    assert alpha["occurrence_count"] == 7

    capability_session = _Session()
    capability_repository = SqlAlchemyCapabilitySetRepository(capability_session)
    capability_set = SimpleNamespace(
        sample_id=sample_id,
        tool_version="capa",
        ruleset_sha256="ruleset",
        parameters_sha256="parameters",
        capabilities=[SimpleNamespace(rule_id=f"CAP-{index}") for index in range(1001)],
    )
    await capability_repository.index(capability_set)

    assert capability_session.scalar_calls == 1
    assert [len(batch) for batch in capability_session.batches] == [1000, 1]

    code_session = _Session()
    code_repository = SqlAlchemyCodeFeatureSetRepository(code_session)
    code_set = SimpleNamespace(
        sample_id=sample_id,
        tool_version="4.5.0",
        escaper_compatibility_version="4.4.5",
        intel_pic_hash_escape_version="4.3.5",
        parameters_sha256="parameters",
        ngrams=[
            SimpleNamespace(pattern=f"{index:08x}", occurrence_count=index + 1)
            for index in range(1001)
        ],
    )
    await code_repository.index(code_set)

    assert code_session.scalar_calls == 1
    assert [len(batch) for batch in code_session.batches] == [1000, 1]
    assert max(len(batch) for batch in code_session.batches) <= 1000
