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
        total_eligible_samples_by_family={"luna": 2},
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
        total_eligible_samples_by_family={"luna": 1, "other": 1},
    )
    assert result.verdict is ReferenceCorpusVerdict.MULTI_FAMILY


def test_motif_occurrences_do_not_supply_family_maturity() -> None:
    result = assess_reference_feature(
        feature_kind="string",
        normalized_value="x",
        malware_members=[(uuid4(), "luna") for _ in range(5)],
        benign_sample_occurrences=0,
        total_eligible_samples_by_family={"luna": 1},
        min_family_samples=5,
    )
    assert result.verdict is ReferenceCorpusVerdict.CORPUS_TOO_SMALL


def test_unknown_uses_total_eligible_corpus_without_feature_matches() -> None:
    result = assess_reference_feature(
        feature_kind="string",
        normalized_value="x",
        malware_members=[],
        benign_sample_occurrences=0,
        total_eligible_samples_by_family={"luna": 3, "other": 2},
        min_family_samples=5,
    )
    assert result.verdict is ReferenceCorpusVerdict.UNKNOWN
