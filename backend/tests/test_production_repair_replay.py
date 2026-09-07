"""LOT 31: one pure repair replay before the downstream chain."""

from __future__ import annotations

import hashlib
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_parsers import (
    DisplayPolicy,
    ExtractionItem,
    IndicatorProvenance,
    IndicatorStatus,
    TechnicalExtraction,
    technical_extraction_to_json,
)
from cti_app.application.production_repairs import (
    EffectiveExtractionProjector,
    reconcile_effective_repairs_in_uow,
    repair_key_for_rejection,
)
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionEvidenceBasis,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    SubjectProductionRun,
)
from cti_app.domain.publication import ArtifactType

EDITION_ID = UUID("00000000-0000-0000-0000-000000000031")
SUBJECT_ID = UUID("00000000-0000-0000-0000-000000000032")
SOURCE_URL = "https://source.example/report"


def _key(
    value: str, *, kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR
) -> str:
    return repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        kind=kind,
        source_url=SOURCE_URL,
        artifact_type="domain" if kind is ProductionRepairIssueKind.REJECTED_INDICATOR else "yara",
        value=value,
    )


def _entry(
    value: str,
    *,
    repair_key: str | None = None,
    artifact_type: str = "domain",
    proposal_kind: str = "artifact",
) -> dict[str, object]:
    return {
        "repair_key": repair_key or _key(value),
        "source_id": "S1",
        "source_url": SOURCE_URL,
        "artifact_type": artifact_type,
        "proposal_kind": proposal_kind,
        "value": value,
        "value_sha256": hashlib.sha256(value.encode()).hexdigest(),
    }


def _decision(
    repair_key: str,
    *,
    action: ProductionRepairAction = ProductionRepairAction.INCLUDE,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
    run_id: UUID | None = None,
) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=run_id or uuid4(),
        observed_artifact_id=uuid4(),
        observed_pipeline_generation=0,
        repair_key=repair_key,
        issue_kind=kind,
        action=action,
        actor_id="analyst",
    )


def _source_item(value: str, *, source_verified: bool = False) -> ExtractionItem:
    return ExtractionItem(
        local_id="I1",
        category="network_artifacts",
        value=value,
        context="",
        artifact_type=ArtifactType.DOMAIN,
        attack_id=None,
        reference_ids=(),
        source_ids=("S1",),
        supported=True,
        indicator_status=IndicatorStatus.CONFIRMED_IOC,
        provenance=IndicatorProvenance.SOURCE,
        display_policy=DisplayPolicy.IOC_SECTION,
        normalized_value=value,
        evidence_basis=(
            ProductionEvidenceBasis.SOURCE_VERIFIED
            if source_verified
            else ProductionEvidenceBasis.ANALYST_OVERRIDE
        ),
    )


def test_projector_is_shared_for_ioc_rule_and_narrative_replays() -> None:
    ioc = _entry("accepted.security-lab.io")
    rule_value = 'rule Override { strings: $a = "marker" condition: $a }'
    rule = _entry(
        rule_value,
        repair_key=_key(rule_value, kind=ProductionRepairIssueKind.REJECTED_RULE),
        artifact_type="yara_rule",
        proposal_kind="rule",
    )
    narrative = _entry("tool.exe", artifact_type="filename")
    decisions = (
        _decision(str(ioc["repair_key"])),
        _decision(
            str(rule["repair_key"]),
            kind=ProductionRepairIssueKind.REJECTED_RULE,
        ),
        _decision(str(narrative["repair_key"])),
    )

    projected = EffectiveExtractionProjector().project(
        base=TechnicalExtraction(items=()),
        repair_entries=(ioc, rule, narrative),
        effective_decisions=decisions,
        resolved_payloads={
            str(ioc["repair_key"]): "accepted.security-lab.io",
            str(rule["repair_key"]): rule_value,
            str(narrative["repair_key"]): "tool.exe",
        },
    )

    assert projected.included_repair_keys == tuple(
        sorted(str(item["repair_key"]) for item in (ioc, rule, narrative))
    )
    assert len(projected.extraction.items) == 2
    assert len(projected.extraction.rules) == 1
    assert projected.extraction.items[0].evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
    assert projected.extraction.items[1].evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE


def test_source_verified_value_wins_without_duplicate_analyst_item() -> None:
    value = "canonical.security-lab.io"
    entry = _entry(value)
    decision = _decision(str(entry["repair_key"]))

    projected = EffectiveExtractionProjector().project(
        base=TechnicalExtraction(items=(_source_item(value, source_verified=True),)),
        repair_entries=(entry,),
        effective_decisions=(decision,),
        resolved_payloads={str(entry["repair_key"]): value},
    )

    assert projected.extraction.items == (_source_item(value, source_verified=True),)
    assert projected.applied_decisions[0]["decision_id"] == str(decision.id)


def test_missing_or_changed_repair_key_never_injects_a_ghost_value() -> None:
    old_value = "old.example"
    new_value = "new.example"
    old_key = _key(old_value)
    new_entry = _entry(new_value, repair_key=_key(new_value))

    projected = EffectiveExtractionProjector().project(
        base=TechnicalExtraction(items=()),
        repair_entries=(new_entry,),
        effective_decisions=(_decision(old_key),),
        resolved_payloads={str(new_entry["repair_key"]): new_value},
    )

    assert projected.extraction.items == ()
    assert projected.included_repair_keys == ()
    assert projected.unresolved_repair_keys == (str(new_entry["repair_key"]),)


class _Store:
    def __init__(self) -> None:
        self.json: dict[UUID, dict[str, object]] = {}
        self.evidence: dict[UUID, dict[str, object]] = {}

    async def read_json(self, blob_id: UUID) -> dict[str, object]:
        return self.json[blob_id]

    async def read_repair_evidence(self, blob_id: UUID) -> dict[str, object]:
        return self.evidence[blob_id]

    async def put_json(self, payload: dict[str, object], *, bucket: str) -> UUID:
        del bucket
        blob_id = uuid4()
        self.json[blob_id] = payload
        return blob_id


class _Artifacts:
    def __init__(self, artifact: ProductionArtifact) -> None:
        self.items = [artifact]

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        return next(
            (
                item
                for item in reversed(self.items)
                if item.production_run_id == run_id
                and item.stage.value == stage
                and item.status.value != "stale"
            ),
            None,
        )

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)


class _Decisions:
    def __init__(self, decisions: tuple[ProductionRepairDecision, ...]) -> None:
        self.decisions = decisions

    async def effective_decisions(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        return tuple(
            decision
            for decision in self.decisions
            if decision.edition_id == edition_id
            and (subject_id is None or decision.subject_id == subject_id)
        )


class _Audit:
    def __init__(self) -> None:
        self.events: list[object] = []

    async def append(self, event: object) -> None:
        self.events.append(event)


class _Uow:
    def __init__(self, artifacts: _Artifacts, decisions: _Decisions, audit: _Audit) -> None:
        self.production_artifacts = artifacts
        self.production_repair_decisions = decisions
        self.edition_audit = audit


@pytest.mark.asyncio
async def test_post_q2_replay_is_one_derived_artifact_with_sidecar_and_provenance() -> None:
    run = SubjectProductionRun(subject_id=SUBJECT_ID, edition_id=EDITION_ID)
    run.start_running()
    value = "replayed.security-lab.io"
    entry = _entry(value)
    evidence_blob_id = uuid4()
    canonical_blob_id = uuid4()
    store = _Store()
    store.evidence[evidence_blob_id] = {"schema_version": "1", "entries": [entry]}
    store.json[canonical_blob_id] = technical_extraction_to_json(TechnicalExtraction(items=()))
    base = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="a" * 64,
        canonical_blob_id=canonical_blob_id,
        metadata={"repair_evidence": {"blob_id": str(evidence_blob_id)}},
    )
    audit = _Audit()
    uow = _Uow(
        _Artifacts(base), _Decisions((_decision(str(entry["repair_key"]), run_id=run.id),)), audit
    )

    derived = await reconcile_effective_repairs_in_uow(
        uow,
        run=run,
        base_extraction_artifact=base,
        artifact_store=store,  # type: ignore[arg-type]
    )

    assert derived is not None
    assert derived.metadata["derived_repair"] is True
    assert derived.metadata["base_extraction_artifact_id"] == str(base.id)
    assert derived.metadata["replay_origin"] == "post_q2_reconciliation"
    assert derived.metadata["repair_evidence"] == base.metadata["repair_evidence"]
    assert len(uow.production_artifacts.items) == 2


@pytest.mark.asyncio
async def test_post_q2_replay_audits_a_disappeared_decision_without_blocking_debt() -> None:
    run = SubjectProductionRun(subject_id=SUBJECT_ID, edition_id=EDITION_ID)
    run.start_running()
    canonical_blob_id = uuid4()
    store = _Store()
    store.json[canonical_blob_id] = technical_extraction_to_json(TechnicalExtraction(items=()))
    base = ProductionArtifact(
        production_run_id=run.id,
        subject_id=run.subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="b" * 64,
        canonical_blob_id=canonical_blob_id,
    )
    audit = _Audit()
    uow = _Uow(
        _Artifacts(base),
        _Decisions((_decision(_key("no-longer-rejected"), run_id=run.id),)),
        audit,
    )

    derived = await reconcile_effective_repairs_in_uow(
        uow,
        run=run,
        base_extraction_artifact=base,
        artifact_store=store,  # type: ignore[arg-type]
    )

    assert derived is None
    assert len(uow.production_artifacts.items) == 1
    assert len(audit.events) == 1
    assert audit.events[0].action == ("production.repair_decision_superseded_by_new_extraction")
