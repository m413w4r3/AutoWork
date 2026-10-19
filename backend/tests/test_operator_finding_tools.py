from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from cti_app.application.investigation_contracts import ArticleClaimV1
from cti_app.application.investigation_findings import (
    OPERATOR_FINDING_SCHEMA_VERSION,
    register_operator_finding_tools,
)
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
)
from cti_app.application.investigation_tools import (
    InMemoryToolRegistry,
    ToolExecutionContext,
)
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    InvestigationBudget,
)


class SnapshotPort:
    async def resolve(self, synthesis_artifact_id):
        raise AssertionError("finding tools validate claims by investigation id")

    async def get_claims(self, investigation_id, claim_ids=()):
        return tuple(
            ArticleClaimV1(id=item, statement="claim", source_refs=()) for item in claim_ids
        )


class InvestigationRepository:
    async def get(self, investigation_id):
        return object()


def make_observation(investigation_id, object_id="sample-a"):
    digest = hashlib.sha256(object_id.encode()).hexdigest()
    return EvidenceObservation(
        investigation_id=investigation_id,
        kind=EvidenceKind.BINARY_ANALYSIS,
        blob_id=uuid4(),
        sha256=digest,
        source="sample.analyze",
        source_version="analysis-v1",
        observed_at=datetime.now(UTC),
        object_type="sample",
        object_id=object_id,
    )


def make_adapter(investigation_id):
    observations = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()
    registry = InMemoryToolRegistry()
    register_operator_finding_tools(
        registry,
        findings=findings,
        observations=observations,
        investigations=InvestigationRepository(),
        article_snapshots=SnapshotPort(),
    )
    registered = registry.get("finding.propose", OPERATOR_FINDING_SCHEMA_VERSION)
    assert registered is not None
    return findings, observations, registered[1]


def context(investigation_id):
    return ToolExecutionContext(
        investigation_id=investigation_id,
        envelope=None,
        budget=InvestigationBudget(limits={}),
        idempotency_key="operator-write:test",
    )


def proposal(**overrides):
    return {
        "action": "propose",
        "kind": "capability",
        "assessment": "supported",
        "statement": "The measured sample exposes capability A.",
        "subject_ref": "sample-a",
        "observation_refs": [],
        "claim_refs": [],
        "novelty": None,
        "scope": "static analysis only",
        "limitations": [],
        **overrides,
    }


@pytest.mark.asyncio
async def test_supported_operator_finding_without_real_observation_is_downgraded() -> None:
    investigation_id = uuid4()
    findings, _observations, adapter = make_adapter(investigation_id)

    result = await adapter.execute(proposal(), context(investigation_id))

    persisted = await findings.list_for_investigation(investigation_id)
    assert result.status.value == "succeeded"
    assert len(persisted) == 1
    assert persisted[0].assessment is FindingAssessment.INCONCLUSIVE
    assert "Subject reference: sample-a" in persisted[0].scope


@pytest.mark.asyncio
async def test_operator_finding_rejects_absent_and_cross_investigation_evidence() -> None:
    investigation_id = uuid4()
    foreign_id = uuid4()
    _findings, observations, adapter = make_adapter(investigation_id)
    absent = uuid4()
    missing = await adapter.execute(
        proposal(observation_refs=[str(absent)]), context(investigation_id)
    )
    foreign = make_observation(foreign_id)
    await observations.append_if_absent(foreign)
    mismatched = await adapter.execute(
        proposal(observation_refs=[str(foreign.id)]), context(investigation_id)
    )

    assert missing.error_code == "finding_observation_not_found"
    assert mismatched.error_code == "finding_observation_investigation_mismatch"


@pytest.mark.asyncio
async def test_supported_finding_requires_matching_observation_and_deduplicates() -> None:
    investigation_id = uuid4()
    findings, observations, adapter = make_adapter(investigation_id)
    observation = make_observation(investigation_id)
    await observations.append_if_absent(observation)

    result = await adapter.execute(
        proposal(observation_refs=[str(observation.id)]), context(investigation_id)
    )
    duplicate = await adapter.execute(
        proposal(observation_refs=[str(observation.id)]), context(investigation_id)
    )
    downgraded = await adapter.execute(
        proposal(subject_ref="different-object", observation_refs=[str(observation.id)]),
        context(investigation_id),
    )

    persisted = await findings.list_for_investigation(investigation_id)
    assert result.status.value == "succeeded"
    assert duplicate.output["finding_id"] == result.output["finding_id"]
    assert len(persisted) == 2
    current = next(item for item in persisted if "different-object" in item.scope)
    assert downgraded.status.value == "succeeded"
    assert downgraded.output["assessment"] == "qualified"
    assert current.assessment is FindingAssessment.QUALIFIED
