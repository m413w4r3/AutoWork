from uuid import uuid4

import pytest

from cti_app.application.production_ioc_candidates import (
    DiscoveryIocProvenance,
    IocCandidate,
    IocCandidateBatch,
)
from cti_app.application.production_ioc_qualification import (
    IocQualification,
    QualificationStatus,
    merge_qualified_candidates,
    parse_ioc_qualifications,
)
from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorStatus,
    TechnicalExtraction,
)
from cti_app.application.production_workflow import ProductionWorkflowOrchestrator
from cti_app.domain.publication import ArtifactType


def _candidate(identifier: str = "c1") -> IocCandidate:
    return IocCandidate(
        candidate_id=identifier,
        artifact_type=ArtifactType.DOMAIN,
        preferred_original_value="evil[.]example",
        normalized_value="evil.example",
        source_ids=("S1",),
        evidence=(),
        indicator_ids=(uuid4(),),
    )


def _batch(*candidates: IocCandidate) -> IocCandidateBatch:
    return IocCandidateBatch("b1", 1, candidates, 0)


def test_qualification_requires_exact_candidate_coverage_and_repairs_cleanly():
    candidates = (_candidate("c1"), _candidate("c2"))
    missing = parse_ioc_qualifications(
        "candidate-id: c1\nstatus: confirmed_ioc\nreason: C2 publié", _batch(*candidates)
    )
    assert not missing.usable
    assert missing.missing_candidate_ids == ("c2",)
    repaired = parse_ioc_qualifications(
        "candidate-id: c1\nstatus: confirmed_ioc\nreason: C2 publié\n\n"
        "candidate-id: c2\nstatus: excluded\nreason: exemple",
        _batch(*candidates),
    )
    assert repaired.usable
    assert len(repaired.qualifications) == 2


@pytest.mark.parametrize(
    "text,error",
    [
        ("candidate-id: other\nstatus: contextual\nreason: x", "ioc_candidate_unknown"),
        ("candidate-id: c1\nstatus: no\nreason: x", "ioc_qualification_status_invalid"),
        (
            "candidate-id: c1\nstatus: contextual\nreason: x\n\n"
            "candidate-id: c1\nstatus: contextual\nreason: x",
            "ioc_candidate_duplicate",
        ),
    ],
)
def test_qualification_rejects_unknown_duplicate_and_invalid_status(text: str, error: str):
    result = parse_ioc_qualifications(text, _batch(_candidate()))
    assert not result.usable
    assert error in result.errors


@pytest.mark.parametrize(
    ("status", "indicator_status", "policy"),
    [
        (
            QualificationStatus.CONFIRMED_IOC,
            IndicatorStatus.CONFIRMED_IOC,
            DisplayPolicy.IOC_SECTION,
        ),
        (QualificationStatus.CONTEXTUAL, IndicatorStatus.CONTEXTUAL, DisplayPolicy.BODY_ONLY),
        (QualificationStatus.EXCLUDED, IndicatorStatus.EXCLUDED, DisplayPolicy.HIDDEN),
    ],
)
def test_merge_uses_pack_facts_and_required_display_mapping(status, indicator_status, policy):
    candidate = _candidate()
    parsed = parse_ioc_qualifications(
        f"candidate-id: c1\nstatus: {status.value}\nreason:  justification  ", _batch(candidate)
    )
    result = merge_qualified_candidates(
        TechnicalExtraction(()), parsed.qualifications, (candidate,)
    )
    item = result.items[0]
    assert (item.value, item.normalized_value, item.artifact_type, item.source_ids) == (
        "evil[.]example",
        "evil.example",
        ArtifactType.DOMAIN,
        ("S1",),
    )
    assert (item.indicator_status, item.display_policy) == (indicator_status, policy)


def test_q2_literal_absent_from_pack_can_never_remain_confirmed():
    q2_item = ExtractionItem(
        local_id="N1",
        category="network_artifacts",
        value="invented.example",
        context="C2",
        artifact_type=ArtifactType.DOMAIN,
        attack_id=None,
        reference_ids=("R1",),
        source_ids=("S1",),
        supported=True,
        indicator_status=IndicatorStatus.CONFIRMED_IOC,
        display_policy=DisplayPolicy.IOC_SECTION,
    )
    result = ProductionWorkflowOrchestrator._suppress_unbacked_q2_literals(
        TechnicalExtraction((q2_item,)), ()
    )
    assert result.items[0].indicator_status is IndicatorStatus.EXCLUDED
    assert result.items[0].display_policy is DisplayPolicy.HIDDEN
    assert result.items[0].context.startswith("unbacked_ioc_literal:")


def test_discovery_only_confirmed_by_model_is_downgraded_to_contextual():
    candidate = IocCandidate(
        candidate_id="discovery-only",
        artifact_type=ArtifactType.DOMAIN,
        preferred_original_value="only.example",
        normalized_value="only.example",
        source_ids=(),
        evidence=(),
        indicator_ids=(),
        discovery_provenance=(DiscoveryIocProvenance(uuid4(), (), ("P1",), "only.example"),),
    )
    result = merge_qualified_candidates(
        TechnicalExtraction(()),
        (IocQualification("discovery-only", QualificationStatus.CONFIRMED_IOC, "model says yes"),),
        (candidate,),
    )
    item = result.items[0]
    assert item.indicator_status is IndicatorStatus.CONTEXTUAL
    assert item.display_policy is DisplayPolicy.BODY_ONLY
