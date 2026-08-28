"""PostgreSQL coverage for invariant measurement batching."""

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.infrastructure.database.models.core import (
    BlobRow,
    ReferenceMemberRow,
    SampleFeatureIndexRow,
    SampleRow,
    SubjectRow,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_measure_features_bulk_matches_single_measurements_and_chunks(
    uow_factory: UnitOfWorkFactory,
) -> None:
    now = datetime.now(UTC)
    subject_id, sample_id, blob_id = uuid4(), uuid4(), uuid4()
    async with uow_factory() as uow:
        session = uow._require_session()
        session.add_all(
            [
                SubjectRow(
                    id=subject_id,
                    external_id=f"bulk-{subject_id}",
                    slug=f"bulk-{subject_id.hex}",
                    tlp="CLEAR",
                    created_at=now,
                ),
                BlobRow(
                    id=blob_id,
                    sha256="a" * 64,
                    size=1,
                    mime_type="application/octet-stream",
                    logical_bucket="test",
                    object_key=f"bulk/{blob_id}",
                    created_at=now,
                ),
                SampleRow(
                    id=sample_id,
                    subject_id=subject_id,
                    blob_id=blob_id,
                    original_name="bulk.bin",
                    origin="integration-test",
                    acquired_at=now,
                    license_restriction=None,
                    tlp="CLEAR",
                    do_not_submit=True,
                    external_llm_allowed=False,
                    imphash="hash-known",
                    imphash_source="local",
                    created_at=now,
                ),
                ReferenceMemberRow(
                    id=uuid4(),
                    sample_id=sample_id,
                    sample_sha256="a" * 64,
                    family_label="family-a",
                    origin_investigation_id=None,
                    promoted_at=now,
                    actor_id="integration-test",
                    label_source="ANALYST",
                ),
                SampleFeatureIndexRow(
                    id=uuid4(),
                    sample_id=sample_id,
                    feature_set_id=None,
                    capability_set_id=None,
                    code_feature_set_id=None,
                    feature_kind="code_ngram",
                    normalized_value="90 90",
                    occurrence_count=3,
                ),
            ]
        )
        await session.flush()

        descriptors = (
            ("code_ngram", "90 90"),
            *(("code_ngram", f"missing-{index:04d}") for index in range(600)),
            ("imphash", "hash-known"),
            ("imphash", "hash-missing"),
        )
        bulk = await uow.invariants.measure_features_bulk(descriptors, (sample_id,))
        repeated = {
            descriptor: await uow.invariants.measure_feature(
                feature_kind=descriptor[0],
                normalized_value=descriptor[1],
                snapshot_sample_ids=(sample_id,),
            )
            for descriptor in descriptors
        }

        assert bulk == repeated
        assert bulk[("code_ngram", "90 90")].positive_support == 1
        assert bulk[("imphash", "hash-known")].reference_members == ((sample_id, "family-a"),)
