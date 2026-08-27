from uuid import uuid4

import pytest

from cti_app.domain.reference_corpus import (
    ReferenceCorpusVerdict,
    ReferenceLabelSource,
    ReferenceMember,
    assess_reference_feature,
)


def test_member_is_immutable_and_source_is_explicit() -> None:
    member = ReferenceMember(
        sample_id=uuid4(),
        sample_sha256="a" * 64,
        family_label=" Luna ",
        actor_id="analyst",
        label_source=ReferenceLabelSource.ANALYST,
        origin_investigation_id=None,
    )
    assert member.family_label == "luna"
    with pytest.raises((AttributeError, TypeError)):
        member.family_label = "other"  # type: ignore[misc]


def test_specificity_counts_distinct_samples() -> None:
    sample = uuid4()
    result = assess_reference_feature(
        feature_kind="string",
        normalized_value="x",
        malware_members=[(sample, "luna"), (sample, "luna"), (uuid4(), "luna")],
        benign_sample_occurrences=0,
        min_family_samples=2,
    )
    assert result.verdict is ReferenceCorpusVerdict.FAMILY_SPECIFIC
    assert result.malware_sample_count == 2


def test_two_families_win_when_corpus_is_small() -> None:
    result = assess_reference_feature(
        feature_kind="import",
        normalized_value="x",
        malware_members=[(uuid4(), "luna"), (uuid4(), "other")],
        benign_sample_occurrences=1,
    )
    assert result.verdict is ReferenceCorpusVerdict.MULTI_FAMILY
