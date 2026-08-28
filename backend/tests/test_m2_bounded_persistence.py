from __future__ import annotations

from collections.abc import Iterable
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.code_features import CodeFeatureService
from cti_app.domain.code_features import CodeFunction, CodeInstruction
from cti_app.domain.goodware import GoodwareFeature
from cti_app.infrastructure.database.repositories.core import (
    SqlAlchemyCapabilitySetRepository,
    SqlAlchemyCodeFeatureSetRepository,
    SqlAlchemyGoodwareBaselineRepository,
    SqlAlchemySampleFeatureSetRepository,
)
from cti_app.infrastructure.smda import SmdaAdapterResult, SmdaExtraction


class _InsertResult:
    rowcount = 1


class _CopyDriver:
    def __init__(self, status: str | None = None) -> None:
        self.status = status
        self.columns: tuple[str, ...] | None = None
        self.records: object | None = None
        self.rows: list[tuple[object, ...]] = []

    async def copy_records_to_table(
        self,
        table: str,
        *,
        columns: tuple[str, ...],
        records: Iterable[tuple[object, ...]],
    ) -> str:
        assert table == "goodware_features"
        assert not isinstance(records, (list, tuple))
        self.columns = columns
        self.records = records
        self.rows = list(records)
        return self.status or f"COPY {len(self.rows)}"


class _Session:
    def __init__(self, copy_driver: object | None = None) -> None:
        self.scalar_calls = 0
        self.execute_calls = 0
        self.batches: list[list[dict[str, object]]] = []
        self.copy_driver = copy_driver

    async def connection(self) -> SimpleNamespace:
        if self.copy_driver is None:
            raise AssertionError("this session should not use a PostgreSQL COPY connection")

        async def get_raw_connection() -> SimpleNamespace:
            return SimpleNamespace(driver_connection=self.copy_driver)

        return SimpleNamespace(get_raw_connection=get_raw_connection)

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
async def test_goodware_features_use_lazy_postgresql_copy() -> None:
    copy_driver = _CopyDriver()
    session = _Session(copy_driver)
    repository = SqlAlchemyGoodwareBaselineRepository(session)
    baseline_id = uuid4()

    features = (
        GoodwareFeature(
            feature_kind="string",
            normalized_value=f"value-{index}",
            occurrence_count=1,
        )
        for index in range(2001)
    )
    copied = await repository.add_features(baseline_id, features)

    assert copied == 2001
    assert copy_driver.columns == (
        "id",
        "baseline_id",
        "feature_kind",
        "normalized_value",
        "occurrence_count",
    )
    assert copy_driver.records is not None
    assert len(copy_driver.rows) == 2001
    assert all(row[1] == baseline_id for row in copy_driver.rows)
    assert copy_driver.rows[0][2:] == ("string", "value-0", 1)
    assert copy_driver.rows[-1][2:] == ("string", "value-2000", 1)
    assert isinstance(copy_driver.rows[0][0], UUID)
    assert session.execute_calls == 0
    assert session.batches == []


@pytest.mark.asyncio
@pytest.mark.parametrize("status, expected", [("COPY 0", 0), ("COPY 2001", 2001)])
async def test_goodware_copy_status_is_strictly_parsed(status: str, expected: int) -> None:
    copy_driver = _CopyDriver(status)
    repository = SqlAlchemyGoodwareBaselineRepository(_Session(copy_driver))

    assert await repository.add_features(uuid4(), iter(())) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize("status", ["INSERT 0 2001", "COPY nope", "COPY -1", "unexpected"])
async def test_goodware_copy_rejects_unexpected_status(status: str) -> None:
    copy_driver = _CopyDriver(status)
    repository = SqlAlchemyGoodwareBaselineRepository(_Session(copy_driver))

    with pytest.raises(RuntimeError, match="unexpected PostgreSQL COPY status"):
        await repository.add_features(uuid4(), iter(()))


@pytest.mark.asyncio
async def test_goodware_copy_fails_closed_without_driver_capability() -> None:
    repository = SqlAlchemyGoodwareBaselineRepository(_Session(SimpleNamespace()))

    with pytest.raises(RuntimeError, match="COPY capability is unavailable"):
        await repository.add_features(uuid4(), iter(()))


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


class _ScoringGoodware:
    def __init__(self) -> None:
        self.chunks: list[tuple[str, ...]] = []

    async def get_feature_occurrences(
        self, baseline_id: UUID, feature_kind: str, normalized_values: tuple[str, ...]
    ) -> dict[str, int]:
        self.chunks.append(normalized_values)
        return {value: 1 for value in normalized_values}


class _ScoringReferences:
    def __init__(self) -> None:
        self.family_calls = 0
        self.member_chunks: list[tuple[str, ...]] = []
        self.benign_chunks: list[tuple[str, ...]] = []

    async def count_eligible_malware_samples_by_family(self) -> dict[str, int]:
        self.family_calls += 1
        return {"luna": 5}

    async def list_feature_members_bulk(
        self, feature_kind: str, normalized_values: tuple[str, ...]
    ) -> dict[str, tuple[tuple[UUID, str], ...]]:
        self.member_chunks.append(normalized_values)
        return {value: () for value in normalized_values}

    async def count_benign_feature_occurrences_bulk(
        self, feature_kind: str, normalized_values: tuple[str, ...]
    ) -> dict[str, int]:
        self.benign_chunks.append(normalized_values)
        return {value: 0 for value in normalized_values}


class _ScoringUow:
    def __init__(self, goodware: _ScoringGoodware, references: _ScoringReferences) -> None:
        self.goodware_baselines = goodware
        self.reference_members = references

    async def __aenter__(self) -> _ScoringUow:
        return self

    async def __aexit__(self, exc_type: object, exc_value: object, traceback: object) -> None:
        return None


def _functions(count: int) -> tuple[CodeFunction, ...]:
    functions = []
    for index in range(count):
        tokens = index.to_bytes(8, "big")
        functions.append(
            CodeFunction(
                offset=index * 16,
                instructions=tuple(
                    CodeInstruction(
                        offset=index * 16 + offset,
                        bytes=b"\x90",
                        mnemonic="nop",
                        escaped_bytes=(token,),
                    )
                    for offset, token in enumerate(tokens)
                ),
            )
        )
    return tuple(functions)


@pytest.mark.asyncio
async def test_code_scoring_uses_bulk_calls_per_500_ngram_chunk() -> None:
    goodware = _ScoringGoodware()
    references = _ScoringReferences()
    service = CodeFeatureService(
        blobs=SimpleNamespace(),
        uow_factory=lambda: _ScoringUow(goodware, references),
        smda=SimpleNamespace(),
    )
    result = SmdaAdapterResult(
        status="SUCCEEDED",
        extraction=SmdaExtraction(
            smda_version="4.5.0",
            escaper_compatibility_version="4.4.5",
            intel_pic_hash_escape_version="4.3.5",
            architecture="x64",
            functions=_functions(1001),
        ),
    )

    feature_set = await service._build_success(
        sample_id=uuid4(),
        blob_id=uuid4(),
        payload=b"",
        parameters_sha256="parameters",
        result=result,
        code_ngram_sizes=(8,),
        code_ngram_max_per_sample=2000,
        goodware_baseline_id=uuid4(),
        min_family_samples=5,
    )

    assert len(feature_set.ngrams) == 1001
    assert [len(chunk) for chunk in goodware.chunks] == [500, 500, 1]
    assert [len(chunk) for chunk in references.member_chunks] == [500, 500, 1]
    assert [len(chunk) for chunk in references.benign_chunks] == [500, 500, 1]
    assert references.family_calls == 1
    assert max(map(len, goodware.chunks)) <= 500
    assert max(map(len, references.member_chunks)) <= 500
    assert max(map(len, references.benign_chunks)) <= 500
