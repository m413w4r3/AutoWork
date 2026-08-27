"""PostgreSQL validation for the M2 bulk persistence paths."""

from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from cti_app.domain.goodware import GoodwareFeature
from cti_app.infrastructure.database.models.base import Base
from cti_app.infrastructure.database.models.core import (
    BlobRow,
    CapabilitySetRow,
    CodeFeatureSetRow,
    GoodwareBaselineRow,
    SampleFeatureIndexRow,
    SampleFeatureSetRow,
    SampleRow,
    SubjectRow,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_m2_bulk_persistence_on_postgresql(uow_factory: object) -> None:
    async with uow_factory() as uow:  # type: ignore[operator]
        session = uow._require_session()  # type: ignore[attr-defined]
        now = datetime.now(UTC)
        subject_id = uuid4()
        sample_id = uuid4()
        blob_id = uuid4()
        baseline_id = uuid4()
        sample_feature_set_id = uuid4()
        code_feature_set_id = uuid4()
        capability_set_id = uuid4()

        session.add_all(
            [
                SubjectRow(
                    id=subject_id,
                    external_id=f"synthetic-{subject_id}",
                    slug=f"synthetic-{subject_id.hex}",
                    tlp="CLEAR",
                    created_at=now,
                ),
                BlobRow(
                    id=blob_id,
                    sha256="a" * 64,
                    size=1,
                    mime_type="application/octet-stream",
                    logical_bucket="test",
                    object_key=f"synthetic/{blob_id}",
                    created_at=now,
                ),
                GoodwareBaselineRow(
                    id=baseline_id,
                    source_set_sha256="b" * 64,
                    records_sha256="c" * 64,
                    record_count=1001,
                    occurrence_sum=1001,
                    pattern_version="synthetic-v1",
                    created_at=now,
                ),
            ]
        )
        await session.flush()

        session.add(

                SampleRow(
                    id=sample_id,
                    subject_id=subject_id,
                    blob_id=blob_id,
                    original_name="synthetic.bin",
                    origin="integration-test",
                    acquired_at=now,
                    license_restriction=None,
                    tlp="CLEAR",
                    do_not_submit=True,
                    external_llm_allowed=False,
                    created_at=now,
                ),
        )
        await session.flush()

        session.add_all(
        [
                SampleFeatureSetRow(
                    id=sample_feature_set_id,
                    sample_id=sample_id,
                    blob_id=blob_id,
                    feature_blob_id=blob_id,
                    extractor_version="synthetic-static-v1",
                    parameters_sha256="d" * 64,
                    payload={},
                    created_at=now,
                ),
                CodeFeatureSetRow(
                    id=code_feature_set_id,
                    sample_id=sample_id,
                    blob_id=blob_id,
                    feature_blob_id=blob_id,
                    tool_version="synthetic-code-v1",
                    escaper_compatibility_version="synthetic-escape-v1",
                    intel_pic_hash_escape_version="synthetic-pic-v1",
                    parameters_sha256="e" * 64,
                    architecture="x64",
                    status="SUCCEEDED",
                    payload={},
                    errors=[],
                    created_at=now,
                ),
                CapabilitySetRow(
                    id=capability_set_id,
                    sample_id=sample_id,
                    blob_id=blob_id,
                    tool_name="synthetic-capa",
                    tool_version="synthetic-capa-v1",
                    ruleset_sha256="f" * 64,
                    parameters_sha256="0" * 64,
                    status="SUCCEEDED",
                    capabilities=[],
                    errors=[],
                ),
            ]
        )
        await session.flush()

        await uow.goodware_baselines.add_features(  # type: ignore[attr-defined]
            baseline_id,
            (
                GoodwareFeature(
                    feature_kind="string",
                    normalized_value=f"value-{index:04d}",
                    occurrence_count=index + 1,
                )
                for index in range(1001)
            ),
        )
        await uow.sample_feature_sets.index(  # type: ignore[attr-defined]
            SimpleNamespace(
                sample_id=sample_id,
                extractor_version="synthetic-static-v1",
                parameters_sha256="d" * 64,
                strings=(
                    {"value": "Alpha", "occurrence_count": 7},
                    {"value": "alpha", "occurrence_count": 3},
                    {"value": "Omega", "occurrence_count": 1},
                ),
                imports=(),
                exports=(),
                sections=(),
                imphash=None,
                opcode_fragment16=(),
            )
        )
        await uow.code_feature_sets.index(  # type: ignore[attr-defined]
            SimpleNamespace(
                sample_id=sample_id,
                tool_version="synthetic-code-v1",
                escaper_compatibility_version="synthetic-escape-v1",
                intel_pic_hash_escape_version="synthetic-pic-v1",
                parameters_sha256="e" * 64,
                ngrams=(
                    SimpleNamespace(pattern="AB CD", occurrence_count=4),
                    SimpleNamespace(pattern="ef 01", occurrence_count=9),
                ),
            )
        )
        await uow.capability_sets.index(  # type: ignore[attr-defined]
            SimpleNamespace(
                sample_id=sample_id,
                tool_version="synthetic-capa-v1",
                ruleset_sha256="f" * 64,
                parameters_sha256="0" * 64,
                capabilities=(
                    SimpleNamespace(rule_id="CAP_ALPHA"),
                    SimpleNamespace(rule_id="cap_alpha"),
                    SimpleNamespace(rule_id="CAP_OMEGA"),
                ),
            )
        )
        await uow.commit()  # type: ignore[attr-defined]

        goodware_count = await session.scalar(
            select(func.count())
            .select_from(Base.metadata.tables["goodware_features"])
            .where(Base.metadata.tables["goodware_features"].c.baseline_id == baseline_id)
        )
        assert goodware_count == 1001
        goodware_values = (
            await session.execute(
                select(
                    Base.metadata.tables["goodware_features"].c.normalized_value,
                    Base.metadata.tables["goodware_features"].c.occurrence_count,
                )
                .where(Base.metadata.tables["goodware_features"].c.baseline_id == baseline_id)
                .order_by(Base.metadata.tables["goodware_features"].c.normalized_value)
            )
        ).all()
        assert goodware_values[0] == ("value-0000", 1)
        assert goodware_values[-1] == ("value-1000", 1001)

        index_rows = (
            await session.scalars(
                select(SampleFeatureIndexRow).where(SampleFeatureIndexRow.sample_id == sample_id)
            )
        ).all()
        by_kind = {(row.feature_kind, row.normalized_value): row for row in index_rows}
        assert len([row for row in index_rows if row.feature_set_id == sample_feature_set_id]) == 2
        assert by_kind[("string", "alpha")].occurrence_count == 7
        assert by_kind[("string", "alpha")].feature_set_id == sample_feature_set_id
        assert (
            len([row for row in index_rows if row.code_feature_set_id == code_feature_set_id]) == 2
        )
        assert by_kind[("code_ngram", "ab cd")].occurrence_count == 4
        assert by_kind[("code_ngram", "ab cd")].code_feature_set_id == code_feature_set_id
        assert len([row for row in index_rows if row.capability_set_id == capability_set_id]) == 2
        assert by_kind[("capability", "cap_alpha")].capability_set_id == capability_set_id
