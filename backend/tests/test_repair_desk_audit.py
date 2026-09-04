"""LOT 23 — cross-cutting Repair Desk invariants.

These tests deliberately span storage, decisions, projection, review, workspace
materialization and production-state export/import: the gaps this lot hunts for
live between those components, not inside any one of them.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import UUID, uuid4

import pytest

from cti_app.application.edition_review import (
    EditionRepairReadService,
    EditionReviewReadItem,
    EditionReviewService,
)
from cti_app.application.edition_workspace import EditionWorkspaceMaterializer
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.application.production_parsers import (
    TechnicalExtraction,
    technical_extraction_from_json,
    technical_extraction_to_json,
)
from cti_app.application.production_repairs import (
    MAX_REPAIR_PREVIEW_CHARS,
    ProductionRepairIssueService,
    ProductionRepairProjectionService,
    build_repair_evidence_pack,
    repair_include_is_buildable,
    repair_key_for_rejection,
    repair_key_for_supplemental_source,
)
from cti_app.application.production_state import (
    PRODUCTION_STATE_SCHEMA_VERSION,
    ProductionStateService,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import CollectionState
from cti_app.domain.editions import Edition, EditionStatus
from cti_app.domain.production import (
    ProductionArtifact,
    ProductionArtifactStage,
    ProductionArtifactStatus,
    ProductionEvidenceBasis,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
    SubjectProductionStatus,
)
from cti_app.domain.publication_review import PublicationDecision

EDITION_ID = UUID("aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa")
SUBJECT_A = UUID("bbbbbbbb-2222-4222-8222-bbbbbbbbbbbb")
SUBJECT_B = UUID("cccccccc-3333-4333-8333-cccccccccccc")
RUN_A = UUID("dddddddd-4444-4444-8444-dddddddddddd")

SOURCE_ONE = "https://one.example/report"
SOURCE_TWO = "https://two.example/report"
SOURCE_THREE = "https://three.example/report"

# Deliberately past MAX_REPAIR_PREVIEW_CHARS so a truncating layer shows up.
YARA_BODY = (
    "rule Lot23_Override\n{\n    meta:\n"
    + "".join(f'        note_{index} = "evidence line {index:03d}"\n' for index in range(24))
    + '    strings:\n        $marker = "lot23-marker"\n'
    "    condition:\n        $marker\n}\n"
)
SIGMA_BODY = (
    "title: Lot23 Sigma\n"
    "logsource:\n  product: windows\n"
    "detection:\n  selection:\n    Image|endswith: '\\lot23.exe'\n  condition: selection\n"
)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class _BlobCatalog:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}
        self.addresses: dict[tuple[str, str], UUID] = {}

    async def ingest(self, source: Any, *, logical_bucket: str, mime_type: str) -> Any:
        del mime_type
        content = source.read()
        digest = hashlib.sha256(content).hexdigest()
        blob_id = self.addresses.setdefault((logical_bucket, digest), uuid4())
        self.contents[blob_id] = content
        return SimpleNamespace(id=blob_id, descriptor=SimpleNamespace(sha256=digest))

    async def read(self, blob_id: UUID, *, max_bytes: int | None = None) -> bytes:
        del max_bytes
        return self.contents[blob_id]


class _Artifacts:
    def __init__(self, items: list[ProductionArtifact] | None = None) -> None:
        self.items: list[ProductionArtifact] = list(items or [])
        self.stale_calls: list[tuple[UUID, str]] = []

    async def get_current(self, run_id: UUID, stage: str) -> ProductionArtifact | None:
        values = [
            item
            for item in self.items
            if item.production_run_id == run_id
            and item.stage.value == stage
            and item.status is not ProductionArtifactStatus.STALE
        ]
        return max(values, key=lambda item: item.version) if values else None

    async def get(self, artifact_id: UUID) -> ProductionArtifact | None:
        return next((item for item in self.items if item.id == artifact_id), None)

    async def list_for_run(self, run_id: UUID) -> list[ProductionArtifact]:
        return [item for item in self.items if item.production_run_id == run_id]

    async def list_current_for_edition(
        self, _edition_id: UUID, stage: str
    ) -> list[ProductionArtifact]:
        by_run: dict[UUID, ProductionArtifact] = {}
        for item in self.items:
            if item.stage.value != stage or item.status is ProductionArtifactStatus.STALE:
                continue
            current = by_run.get(item.production_run_id)
            if current is None or item.version > current.version:
                by_run[item.production_run_id] = item
        return list(by_run.values())

    async def append(self, artifact: ProductionArtifact) -> None:
        self.items.append(artifact)

    async def mark_downstream_stale(self, run_id: UUID, stage: str) -> None:
        self.stale_calls.append((run_id, stage))
        order = ["references", "extraction", "synthesis", "publication"]
        if stage not in order:
            return
        downstream = set(order[order.index(stage) + 1 :])
        for index, item in enumerate(self.items):
            if item.production_run_id == run_id and item.stage.value in downstream:
                self.items[index] = _with_status(item, ProductionArtifactStatus.STALE)


def _with_status(
    artifact: ProductionArtifact, status: ProductionArtifactStatus
) -> ProductionArtifact:
    from dataclasses import replace

    return replace(artifact, status=status)


class _Decisions:
    """In-memory append-only log with the production effective-read semantics."""

    def __init__(self, decisions: list[ProductionRepairDecision] | None = None) -> None:
        self.history: list[ProductionRepairDecision] = list(decisions or [])

    async def append(self, decision: ProductionRepairDecision) -> None:
        self.history.append(decision)

    async def list_for_edition(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> list[ProductionRepairDecision]:
        return [
            decision
            for decision in self.history
            if decision.edition_id == edition_id
            and (subject_id is None or decision.subject_id == subject_id)
        ]

    async def effective_decisions(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        latest: dict[tuple[UUID, str], ProductionRepairDecision] = {}
        for decision in sorted(
            await self.list_for_edition(edition_id, subject_id),
            key=lambda item: (item.created_at, item.id),
        ):
            latest[(decision.subject_id, decision.repair_key)] = decision
        return tuple(latest.values())


class _Runs:
    def __init__(self, runs: list[Any]) -> None:
        self.runs = runs

    async def get(self, run_id: UUID) -> Any | None:
        return next((run for run in self.runs if run.id == run_id), None)

    async def get_for_update(self, run_id: UUID) -> Any | None:
        return await self.get(run_id)

    async def add(self, run: Any) -> None:
        self.runs.append(run)

    async def lock_creation_for_subject(self, _subject_id: UUID) -> None:
        return None

    async def allocate_next_run_number(self, subject_id: UUID) -> int:
        return 1 + sum(1 for run in self.runs if run.subject_id == subject_id)

    async def get_current_for_subject(self, subject_id: UUID) -> Any | None:
        matches = [run for run in self.runs if run.subject_id == subject_id]
        return matches[-1] if matches else None

    async def list_for_edition(self, edition_id: UUID) -> list[Any]:
        return [run for run in self.runs if run.edition_id == edition_id]


class _Uow:
    def __init__(
        self,
        *,
        runs: list[Any],
        artifacts: list[ProductionArtifact],
        decisions: list[ProductionRepairDecision] | None = None,
        collections: list[Any] | None = None,
        rows: list[EditionReviewReadItem] | None = None,
    ) -> None:
        self.subject_production_runs = _Runs(runs)
        self.production_artifacts = _Artifacts(artifacts)
        self.production_repair_decisions = _Decisions(decisions)
        self._collections = list(collections or [])
        self.source_collections = SimpleNamespace(
            list_for_subject=self._list_collections,
            list_for_subjects=self._list_collections_bulk,
        )
        self.editions = SimpleNamespace(
            get=lambda _id: _value(_edition()),
            get_for_update=lambda _id: _value(_edition()),
        )
        self.edition_review_read_model = SimpleNamespace(
            list_for_edition=lambda _id: _value(list(rows or []))
        )
        self.committed = 0

    async def _list_collections(self, subject_id: UUID) -> list[Any]:
        return [item for item in self._collections if item.subject_id == subject_id]

    async def _list_collections_bulk(self, subject_ids: Any) -> list[Any]:
        wanted = set(subject_ids)
        return [item for item in self._collections if item.subject_id in wanted]

    async def __aenter__(self) -> _Uow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed += 1


async def _value(value: Any) -> Any:
    return value


def _factory(uow: _Uow) -> Any:
    return lambda: uow


def _edition() -> Edition:
    return Edition(
        id=EDITION_ID,
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.GREEN,
        languages=("fr",),
        target_articles=2,
        source_profile="test",
        status=EditionStatus.REVIEW,
    )


def _run(
    run_id: UUID = RUN_A,
    subject_id: UUID = SUBJECT_A,
    *,
    generation: int = 0,
) -> SimpleNamespace:
    return SimpleNamespace(
        id=run_id,
        edition_id=EDITION_ID,
        subject_id=subject_id,
        status=SubjectProductionStatus.READY,
        requires_reconciliation=False,
        pipeline_generation=generation,
        research_date=date(2026, 8, 15),
    )


def _row(
    subject_id: UUID = SUBJECT_A,
    run_id: UUID = RUN_A,
    *,
    position: int = 1,
    decision: PublicationDecision | None = None,
    generation: int = 0,
) -> EditionReviewReadItem:
    return EditionReviewReadItem(
        position=position,
        subject_id=subject_id,
        title=f"Article {position}",
        run_id=run_id,
        pipeline_generation=generation,
        run_status=SubjectProductionStatus.READY,
        document_artifact_id=uuid4(),
        document_artifact_version=1,
        document_input_hash="a" * 64,
        document_artifact_status=ProductionArtifactStatus.VERIFIED,
        error_code=None,
        error_message=None,
        effective_decision=decision,
    )


def _ioc_entry(
    index: int, *, source_url: str = SOURCE_ONE, source_id: str = "S1"
) -> dict[str, Any]:
    value = f"host-{index:04d}.evil-lot23.com"
    return {
        "source_id": source_id,
        "source_title": f"Report {source_id}",
        "source_url": source_url,
        "proposal_kind": "artifact",
        "artifact_type": "domain",
        "reason_code": "source_evidence_not_text_verifiable",
        "value": value,
        "value_sha256": _sha256(value),
        "model_run_id": f"model-run-{index % 3}",
    }


def _rule_entry(
    body: str, *, artifact_type: str, source_id: str, source_url: str
) -> dict[str, Any]:
    return {
        "source_id": source_id,
        "source_title": f"Report {source_id}",
        "source_url": source_url,
        "proposal_kind": "rule",
        "artifact_type": artifact_type,
        "name": f"Lot23 {artifact_type}",
        "reason_code": "source_rule_evidence_missing",
        "value": body,
        "value_sha256": _sha256(body),
        "model_run_id": "model-run-rules",
    }


async def _extraction_artifact(
    store: ProductionArtifactStore,
    entries: list[dict[str, Any]],
    *,
    base_extraction: TechnicalExtraction | None = None,
    run_id: UUID = RUN_A,
    subject_id: UUID = SUBJECT_A,
) -> ProductionArtifact:
    pack = build_repair_evidence_pack(entries)
    evidence_id = await store.put_repair_evidence(pack)
    canonical_id = await store.put_json(
        technical_extraction_to_json(base_extraction or TechnicalExtraction(items=(), rules=())),
        bucket="production-artifacts-canonical",
    )
    return ProductionArtifact(
        production_run_id=run_id,
        subject_id=subject_id,
        stage=ProductionArtifactStage.EXTRACTION,
        version=1,
        input_hash="b" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=canonical_id,
        metadata={
            "repair_evidence": {
                "schema_version": "1",
                "blob_id": str(evidence_id),
                "entry_count": len(entries),
                "index": [
                    {
                        key: entry[key]
                        for key in (
                            "source_id",
                            "source_title",
                            "source_url",
                            "proposal_kind",
                            "artifact_type",
                            "reason_code",
                            "value_sha256",
                        )
                        if key in entry
                    }
                    | {"preview": entry["value"][:MAX_REPAIR_PREVIEW_CHARS]}
                    for entry in entries
                ],
            },
        },
    )


def _decision(
    repair_key: str,
    kind: ProductionRepairIssueKind,
    action: ProductionRepairAction,
    *,
    subject_id: UUID = SUBJECT_A,
    run_id: UUID = RUN_A,
    artifact_id: UUID | None = None,
    generation: int = 0,
) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=subject_id,
        production_run_id=run_id,
        observed_artifact_id=artifact_id or uuid4(),
        observed_pipeline_generation=generation,
        repair_key=repair_key,
        issue_kind=kind,
        action=action,
        actor_id="analyst",
        reason="LOT 23 audit",
    )


# --------------------------------------------------------------------------
# AUDIT 1 — no repair content is lost at scale
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit1_every_rejection_stays_reachable_with_intact_bodies() -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    ioc_entries = [
        _ioc_entry(
            index,
            source_url=(SOURCE_ONE if index % 2 else SOURCE_TWO),
            source_id=("S1" if index % 2 else "S2"),
        )
        for index in range(210)
    ]
    entries = [
        *ioc_entries,
        _rule_entry(YARA_BODY, artifact_type="yara", source_id="S1", source_url=SOURCE_ONE),
        _rule_entry(SIGMA_BODY, artifact_type="sigma", source_id="S2", source_url=SOURCE_TWO),
    ]
    artifact = await _extraction_artifact(store, entries)
    # A third source was proposed by Q1 but never archived.
    collection = SimpleNamespace(
        id=uuid4(),
        subject_id=SUBJECT_A,
        canonical_url=SOURCE_THREE,
        state=CollectionState.FAILED_TERMINAL,
        error_reason="collection_failed",
        attempt_count=2,
    )
    references = ProductionArtifact(
        production_run_id=RUN_A,
        subject_id=SUBJECT_A,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        raw_blob_id=uuid4(),
        canonical_blob_id=uuid4(),
        metadata={
            "repair_source_index": {
                "proposed": [
                    {"source_id": "S1", "source_url": SOURCE_ONE, "source_title": "One"},
                    {"source_id": "S2", "source_url": SOURCE_TWO, "source_title": "Two"},
                    {"source_id": "S3", "source_url": SOURCE_THREE, "source_title": "Three"},
                ],
                "canonical": [
                    {"source_id": "S1", "source_url": SOURCE_ONE},
                    {"source_id": "S2", "source_url": SOURCE_TWO},
                ],
            }
        },
    )
    uow = _Uow(
        runs=[_run()],
        artifacts=[artifact, references],
        collections=[collection],
        rows=[_row()],
    )
    issues = ProductionRepairIssueService(_factory(uow), store)  # type: ignore[arg-type]

    views = await issues.list_issues(EDITION_ID, SUBJECT_A)
    sources = await issues.list_supplemental_source_issues(EDITION_ID, SUBJECT_A)

    assert len(views) == 212
    assert len(sources) == 1
    assert sources[0].source_url == SOURCE_THREE
    assert len({view.repair_key for view in views}) == 212

    # Rule bodies and hashes survive intact through the detail endpoint.
    rule_views = [view for view in views if view.kind is ProductionRepairIssueKind.REJECTED_RULE]
    assert len(rule_views) == 2
    for view, expected in ((rule_views[0], None), (rule_views[1], None)):
        del expected
        detail = await issues.get_issue(EDITION_ID, view.repair_key, SUBJECT_A)
        assert detail is not None and detail.value is not None
        assert _sha256(detail.value) == view.value_sha256
        assert detail.value in {YARA_BODY, SIGMA_BODY}
    assert any(len(body) > 512 for body in (YARA_BODY,))

    # The cross-subject read model reaches every issue through its cursor.
    read_service = EditionRepairReadService(_factory(uow), issues)  # type: ignore[arg-type]
    collected: list[str] = []
    page = await read_service.list(EDITION_ID, status="all", limit=100)
    collected.extend(item.repair_key for item in page.items)
    while page.next_cursor is not None:
        page = await read_service.list(EDITION_ID, status="all", limit=100, cursor=page.next_cursor)
        collected.extend(item.repair_key for item in page.items)
    assert len(collected) == 213
    assert len(set(collected)) == 213


# --------------------------------------------------------------------------
# AUDIT 2 — immutability
# --------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_audit2_projection_adds_a_version_and_never_touches_the_base() -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    entry = _ioc_entry(1)
    key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=SOURCE_ONE,
        artifact_type="domain",
        value=entry["value"],
    )
    base = await _extraction_artifact(store, [entry])
    synthesis = ProductionArtifact(
        production_run_id=RUN_A,
        subject_id=SUBJECT_A,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="d" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        rendered_blob_id=uuid4(),
    )
    uow = _Uow(
        runs=[_run()],
        artifacts=[base, synthesis],
        decisions=[
            _decision(
                key,
                ProductionRepairIssueKind.REJECTED_INDICATOR,
                ProductionRepairAction.INCLUDE,
                artifact_id=base.id,
            )
        ],
    )
    base_before = json.dumps(
        await store.read_json(base.canonical_blob_id),
        sort_keys=True,  # type: ignore[arg-type]
    )
    base_metadata_before = json.dumps(base.metadata, sort_keys=True)

    result = await ProductionRepairProjectionService(
        _factory(uow),
        store,  # type: ignore[arg-type]
    ).project_effective_extraction(RUN_A, actor_id="analyst")

    assert result.changed
    assert result.artifact.id != base.id
    assert result.artifact.version == base.version + 1
    # The base is still present, non-stale and byte-identical.
    stored_base = await uow.production_artifacts.get(base.id)
    assert stored_base is not None
    assert stored_base.status is not ProductionArtifactStatus.STALE
    assert (
        json.dumps(
            await store.read_json(stored_base.canonical_blob_id),
            sort_keys=True,  # type: ignore[arg-type]
        )
        == base_before
    )
    assert json.dumps(stored_base.metadata, sort_keys=True) == base_metadata_before
    # Downstream artifacts are staled, upstream ones are not.
    assert (RUN_A, "extraction") in uow.production_artifacts.stale_calls
    staled = await uow.production_artifacts.get(synthesis.id)
    assert staled is not None and staled.status is ProductionArtifactStatus.STALE
    # And the old artifact remains auditable as a distinct version.
    versions = {
        item.version
        for item in await uow.production_artifacts.list_for_run(RUN_A)
        if item.stage is ProductionArtifactStage.EXTRACTION
    }
    assert versions == {1, 2}


@pytest.mark.asyncio
async def test_audit2_decision_log_is_append_only_and_last_write_wins() -> None:
    decisions = _Decisions()
    key = "e" * 64
    first = _decision(key, ProductionRepairIssueKind.REJECTED_RULE, ProductionRepairAction.EXCLUDE)
    second = ProductionRepairDecision(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        production_run_id=RUN_A,
        observed_artifact_id=first.observed_artifact_id,
        observed_pipeline_generation=0,
        repair_key=key,
        issue_kind=ProductionRepairIssueKind.REJECTED_RULE,
        action=ProductionRepairAction.INCLUDE,
        actor_id="reviewer",
        created_at=first.created_at + timedelta(seconds=1),
    )
    await decisions.append(first)
    await decisions.append(second)

    assert len(await decisions.list_for_edition(EDITION_ID)) == 2
    effective = await decisions.effective_decisions(EDITION_ID)
    assert len(effective) == 1
    assert effective[0].action is ProductionRepairAction.INCLUDE
    # The superseded decision is never removed from the history.
    assert first in decisions.history


# --------------------------------------------------------------------------
# AUDIT 3 — decisions survive replays
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("kind", "artifact_type", "value"),
    [
        (ProductionRepairIssueKind.REJECTED_INDICATOR, "domain", "evil.test-lot23.com"),
        (ProductionRepairIssueKind.REJECTED_RULE, "yara", YARA_BODY),
    ],
)
def test_audit3_repair_key_ignores_replay_identity_and_follows_content(
    kind: ProductionRepairIssueKind, artifact_type: str, value: str
) -> None:
    generation_zero = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=kind,
        source_url=SOURCE_THREE,
        artifact_type=artifact_type,
        value=value,
    )
    # Generation 1: same source URL, type and value; different run identity.
    generation_one = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=kind,
        source_url=SOURCE_THREE + "?utm_source=replay",
        artifact_type=artifact_type,
        value=value,
    )
    assert generation_one == generation_zero

    # Generation 2: a different value is a different problem.
    generation_two = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=kind,
        source_url=SOURCE_THREE,
        artifact_type=artifact_type,
        value=value.replace("evil", "evil2").replace("Lot23", "Lot24"),
    )
    assert generation_two != generation_zero


@pytest.mark.asyncio
async def test_audit3_include_stays_effective_after_a_replay() -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    value = "evil.lot23-audit.com"
    entry = {
        "source_id": "S3",
        "source_title": "Three",
        "source_url": SOURCE_THREE,
        "proposal_kind": "artifact",
        "artifact_type": "domain",
        "reason_code": "source_evidence_not_text_verifiable",
        "value": value,
        "value_sha256": _sha256(value),
        "model_run_id": "run-generation-0",
    }
    key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=SOURCE_THREE,
        artifact_type="domain",
        value=value,
    )
    decision = _decision(
        key,
        ProductionRepairIssueKind.REJECTED_INDICATOR,
        ProductionRepairAction.INCLUDE,
        generation=0,
    )

    # Generation 1 replays the same rejection under a fresh model run.
    replayed = dict(entry) | {"model_run_id": "run-generation-1", "proposal_index": 7}
    base = await _extraction_artifact(store, [replayed])
    uow = _Uow(runs=[_run(generation=1)], artifacts=[base], decisions=[decision])

    result = await ProductionRepairProjectionService(
        _factory(uow),
        store,  # type: ignore[arg-type]
    ).project_effective_extraction(RUN_A, actor_id="analyst")

    assert result.included_repair_keys == (key,)
    assert result.unresolved_count == 0

    # Generation 2 proposes a different value: no decision applies to it.
    next_value = "evil2.lot23-audit.com"
    changed = dict(entry) | {"value": next_value, "value_sha256": _sha256(next_value)}
    base_two = await _extraction_artifact(store, [changed], run_id=uuid4())
    run_two = _run(base_two.production_run_id, generation=2)
    uow_two = _Uow(runs=[run_two], artifacts=[base_two], decisions=[decision])

    result_two = await ProductionRepairProjectionService(
        _factory(uow_two),
        store,  # type: ignore[arg-type]
    ).project_effective_extraction(run_two.id, actor_id="analyst")

    assert result_two.included_repair_keys == ()
    assert result_two.unresolved_count == 1
    assert result_two.unresolved_repair_keys[0] != key


# --------------------------------------------------------------------------
# AUDIT 5/6/7 — analyst overrides, exclusions and waivers
# --------------------------------------------------------------------------


async def _projected(
    store: ProductionArtifactStore,
    entries: list[dict[str, Any]],
    decisions: list[ProductionRepairDecision],
) -> TechnicalExtraction:
    base = await _extraction_artifact(store, entries)
    uow = _Uow(runs=[_run()], artifacts=[base], decisions=decisions)
    result = await ProductionRepairProjectionService(
        _factory(uow),
        store,  # type: ignore[arg-type]
    ).project_effective_extraction(RUN_A, actor_id="analyst")
    return technical_extraction_from_json(
        await store.read_json(result.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )


@pytest.mark.asyncio
async def test_audit5_included_ioc_is_analyst_override_and_never_source_verified() -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    entry = _ioc_entry(42)
    key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=SOURCE_TWO,
        artifact_type="domain",
        value=entry["value"],
    )
    entry["source_url"] = SOURCE_TWO
    entry["source_id"] = "S2"
    projected = await _projected(
        store,
        [entry],
        [
            _decision(
                key,
                ProductionRepairIssueKind.REJECTED_INDICATOR,
                ProductionRepairAction.INCLUDE,
            )
        ],
    )

    assert len(projected.items) == 1
    item = projected.items[0]
    assert item.value == entry["value"]
    assert item.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
    assert item.evidence_basis is not ProductionEvidenceBasis.SOURCE_VERIFIED
    assert item.provenance.value == "analyst"
    assert item.source_ids == ("S2",)
    # The claim never travels as a source quote.
    assert item.evidence_quote == ""
    serialized = technical_extraction_to_json(projected)
    assert serialized["items"][0]["evidence_basis"] == "analyst_override"


@pytest.mark.asyncio
async def test_audit6_included_rule_materializes_byte_for_byte(tmp_path: Path) -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    entry = _rule_entry(YARA_BODY, artifact_type="yara", source_id="S1", source_url=SOURCE_ONE)
    key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        source_url=SOURCE_ONE,
        artifact_type="yara",
        value=YARA_BODY,
    )
    projected = await _projected(
        store,
        [entry],
        [_decision(key, ProductionRepairIssueKind.REJECTED_RULE, ProductionRepairAction.INCLUDE)],
    )
    assert len(projected.rules) == 1
    rule = projected.rules[0]
    assert rule.body == YARA_BODY
    assert rule.sha256 == _sha256(YARA_BODY)
    assert rule.evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE

    materialization = await EditionWorkspaceMaterializer(tmp_path / "editions").materialize(
        edition_id=EDITION_ID,
        period=date(2026, 8, 1),
        country_code="FR",
        position=1,
        subject_id=SUBJECT_A,
        subject_title="Article 1",
        production_state=_snapshot(projected),
        rules=projected.rules,
    )

    assert materialization.rule_sidecar_error is None
    sidecars = [path for path in materialization.files if path.suffix == ".yar"]
    assert len(sidecars) == 1
    assert sidecars[0].read_bytes() == YARA_BODY.encode("utf-8")
    manifest = json.loads((sidecars[0].parent / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["rules"][0]["sha256"] == _sha256(YARA_BODY)
    assert manifest["rules"][0]["filename"] == sidecars[0].name


@pytest.mark.asyncio
async def test_audit7_exclude_keeps_the_value_out_and_writes_no_sidecar(tmp_path: Path) -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    ioc = _ioc_entry(7)
    rule = _rule_entry(SIGMA_BODY, artifact_type="sigma", source_id="S1", source_url=SOURCE_ONE)
    ioc_key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=ioc["source_url"],
        artifact_type="domain",
        value=ioc["value"],
    )
    rule_key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        source_url=SOURCE_ONE,
        artifact_type="sigma",
        value=SIGMA_BODY,
    )
    base = await _extraction_artifact(store, [ioc, rule])
    uow = _Uow(
        runs=[_run()],
        artifacts=[base],
        decisions=[
            _decision(
                ioc_key,
                ProductionRepairIssueKind.REJECTED_INDICATOR,
                ProductionRepairAction.EXCLUDE,
            ),
            _decision(
                rule_key,
                ProductionRepairIssueKind.REJECTED_RULE,
                ProductionRepairAction.EXCLUDE,
            ),
        ],
    )
    result = await ProductionRepairProjectionService(
        _factory(uow),
        store,  # type: ignore[arg-type]
    ).project_effective_extraction(RUN_A, actor_id="analyst")

    # Nothing changed: rejections were already absent from the base extraction.
    assert not result.changed
    assert sorted(result.excluded_repair_keys) == sorted((ioc_key, rule_key))
    assert result.unresolved_count == 0
    projected = technical_extraction_from_json(
        await store.read_json(result.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )
    assert projected.items == ()
    assert projected.rules == ()

    materialization = await EditionWorkspaceMaterializer(tmp_path / "editions").materialize(
        edition_id=EDITION_ID,
        period=date(2026, 8, 1),
        country_code="FR",
        position=1,
        subject_id=SUBJECT_A,
        subject_title="Article 1",
        production_state=_snapshot(projected),
        rules=projected.rules,
    )
    rules_dir = materialization.item_path / "article" / "rules"
    assert not any(path.suffix in {".yar", ".yml"} for path in materialization.files)
    assert not rules_dir.exists() or not list(rules_dir.glob("*.yml"))


@pytest.mark.asyncio
async def test_audit7_waived_source_stays_unarchived_but_signed_off() -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    collection = SimpleNamespace(
        id=uuid4(),
        subject_id=SUBJECT_A,
        canonical_url=SOURCE_THREE,
        state=CollectionState.FAILED_TERMINAL,
        error_reason="collection_failed",
        attempt_count=3,
    )
    references = ProductionArtifact(
        production_run_id=RUN_A,
        subject_id=SUBJECT_A,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        metadata={
            "repair_source_index": {
                "proposed": [
                    {"source_id": "S1", "source_url": SOURCE_ONE, "source_title": "One"},
                    {"source_id": "S3", "source_url": SOURCE_THREE, "source_title": "Three"},
                ],
                "canonical": [{"source_id": "S1", "source_url": SOURCE_ONE}],
            }
        },
    )
    key = repair_key_for_supplemental_source(
        edition_id=EDITION_ID, subject_id=SUBJECT_A, source_url=SOURCE_THREE
    )
    uow = _Uow(
        runs=[_run()],
        artifacts=[references],
        collections=[collection],
        rows=[_row()],
        decisions=[
            _decision(
                key,
                ProductionRepairIssueKind.SUPPLEMENTAL_SOURCE_UNARCHIVED,
                ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
            )
        ],
    )
    issues = ProductionRepairIssueService(_factory(uow), store)  # type: ignore[arg-type]
    sources = await issues.list_supplemental_source_issues(EDITION_ID, SUBJECT_A)

    assert len(sources) == 1
    issue = sources[0]
    # The source is still not archived; only the review was signed.
    assert issue.collection_state == CollectionState.FAILED_TERMINAL.value
    assert issue.effective_decision is not None
    assert issue.effective_decision.action is ProductionRepairAction.CONTINUE_WITHOUT_SOURCE
    assert issue.recommended_action == "continue_without_source"
    # The canonical reference report was never rewritten by the waiver.
    canonical_index = references.metadata["repair_source_index"]["canonical"]
    assert canonical_index == [{"source_id": "S1", "source_url": SOURCE_ONE}]

    review = EditionReviewService.from_rows(EDITION_ID, [_row()], repair_issues=list(sources))
    assert review.unresolved_repair_count == 0
    assert review.repair_review_complete
    assert review.can_accept


# --------------------------------------------------------------------------
# AUDIT 9 — production state export/import
# --------------------------------------------------------------------------


def _snapshot(extraction: TechnicalExtraction) -> Any:
    """Minimal V3 snapshot used where only the extraction matters."""
    from cti_app.application.production_state import (
        ProductionStateSnapshotV3,
        compute_production_state_checksum,
    )

    payload = {
        "format": "autowork.production-state",
        "schema_version": PRODUCTION_STATE_SCHEMA_VERSION,
        "exported_at": "2026-08-29T10:00:00Z",
        "origin": {"subject_title": "Article 1", "research_date": "2026-08-15"},
        "artifacts": {
            "references": {"input_hash": "a" * 64, "canonical_content": {"sources": []}},
            "extraction": {
                "input_hash": "b" * 64,
                "canonical_content": technical_extraction_to_json(extraction),
            },
            "synthesis": {"input_hash": "c" * 64, "rendered_content": "Article"},
        },
        "repair": None,
        "content_sha256": "0" * 64,
    }
    snapshot = ProductionStateSnapshotV3.model_validate(payload)
    return snapshot.model_copy(
        update={"content_sha256": compute_production_state_checksum(snapshot)}
    )


class _StateUow(_Uow):
    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.editorial_groups = SimpleNamespace(get_by_subject=lambda _id: _value(None))
        self.production_input_snapshots = SimpleNamespace(add=lambda _item: _value(None))
        self.edition_production_batch_items = SimpleNamespace(
            get_by_run=lambda _run_id: _value(None)
        )

    async def _repoint(self, *_args: Any) -> None:
        return None


@pytest.mark.asyncio
async def test_audit9_repaired_state_round_trips_with_its_decision_audit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    entry = _ioc_entry(11)
    key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=entry["source_url"],
        artifact_type="domain",
        value=entry["value"],
    )
    base = await _extraction_artifact(store, [entry])
    decision = _decision(
        key,
        ProductionRepairIssueKind.REJECTED_INDICATOR,
        ProductionRepairAction.INCLUDE,
        artifact_id=base.id,
    )
    uow = _Uow(runs=[_run()], artifacts=[base], decisions=[decision])
    projection = await ProductionRepairProjectionService(
        _factory(uow),
        store,  # type: ignore[arg-type]
    ).project_effective_extraction(RUN_A, actor_id="analyst")
    assert projection.changed

    references = ProductionArtifact(
        production_run_id=RUN_A,
        subject_id=SUBJECT_A,
        stage=ProductionArtifactStage.REFERENCES,
        version=1,
        input_hash="a" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        canonical_blob_id=await store.put_json(
            {"parser_version": "1", "sources": [], "events": []},
            bucket="production-artifacts-canonical",
        ),
    )
    synthesis_blob, _, rendered = await store.store_stage_payloads(rendered="Article de test")
    del synthesis_blob
    synthesis = ProductionArtifact(
        production_run_id=RUN_A,
        subject_id=SUBJECT_A,
        stage=ProductionArtifactStage.SYNTHESIS,
        version=1,
        input_hash="c" * 64,
        status=ProductionArtifactStatus.VERIFIED,
        rendered_blob_id=rendered,
    )
    await uow.production_artifacts.append(references)
    await uow.production_artifacts.append(synthesis)

    # Synthesis validation is out of scope here; the audit is about the
    # repair block travelling with the artifacts.
    monkeypatch.setattr(
        "cti_app.application.production_state._validate_parsers",
        lambda snapshot: (None, None),
    )

    service = ProductionStateService(_factory(uow), store)  # type: ignore[arg-type]
    snapshot = await service.export_run_state(RUN_A, subject_title="Article 1")

    assert snapshot.schema_version == PRODUCTION_STATE_SCHEMA_VERSION == 3
    assert snapshot.repair is not None
    assert snapshot.repair.included_repair_keys == (key,)
    assert snapshot.repair.base_extraction_artifact_id == str(base.id)
    assert [item.repair_key for item in snapshot.repair.decisions] == [key]
    assert snapshot.repair.decisions[0].action == "include"
    assert snapshot.repair.decisions[0].actor_id == "analyst"

    # The exported extraction already is the effective one.
    exported = technical_extraction_from_json(snapshot.artifacts.extraction.canonical_content)
    projected = technical_extraction_from_json(
        await store.read_json(projection.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )
    assert [item.value for item in exported.items] == [item.value for item in projected.items]
    assert exported.items[0].evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE

    # Importing reproduces the same deliverable and keeps the audit readable.
    target = _StateUow(runs=[], artifacts=[])
    monkeypatch.setattr(
        "cti_app.application.production_state._repoint_batch_item",
        lambda *_args, **_kwargs: _value(None),
    )
    target_service = ProductionStateService(_factory(target), store)  # type: ignore[arg-type]
    result = await target_service.import_state(
        subject_id=SUBJECT_A,
        edition_id=EDITION_ID,
        payload=snapshot.model_dump(mode="json"),
    )

    assert result.schema_version == 3
    imported = next(
        item
        for item in target.production_artifacts.items
        if item.stage is ProductionArtifactStage.EXTRACTION
    )
    reimported = technical_extraction_from_json(
        await store.read_json(imported.canonical_blob_id)  # type: ignore[arg-type]
    )
    assert [item.value for item in reimported.items] == [item.value for item in exported.items]
    assert reimported.items[0].evidence_basis is ProductionEvidenceBasis.ANALYST_OVERRIDE
    audit = imported.metadata["imported_repair_audit"]
    assert audit["included_repair_keys"] == [key]
    assert audit["decisions"][0]["repair_key"] == key
    # A projection marker would dangle in the target database.
    assert "repair_projection" not in imported.metadata


# --------------------------------------------------------------------------
# AUDIT 10 — review read model
# --------------------------------------------------------------------------


def _issue(
    repair_key: str,
    row: EditionReviewReadItem,
    *,
    kind: ProductionRepairIssueKind = ProductionRepairIssueKind.REJECTED_INDICATOR,
    is_ioc: bool = True,
    artifact_type: str | None = "domain",
) -> SimpleNamespace:
    return SimpleNamespace(
        repair_key=repair_key,
        kind=kind,
        subject_id=row.subject_id,
        production_run_id=row.run_id,
        observed_artifact_id=uuid4(),
        observed_artifact_version=1,
        observed_pipeline_generation=row.pipeline_generation,
        artifact_type=artifact_type,
        source_id="S1",
        source_title="One",
        source_url=SOURCE_ONE,
        reason_code="source_evidence_not_text_verifiable",
        value_sha256="f" * 64,
        preview="preview",
        payload_available=True,
        is_publication_ioc=is_ioc,
        effective_decision=None,
        projection_applied=False,
    )


def test_audit10_indicator_counters_ignore_non_ioc_artifact_types() -> None:
    row = _row()
    issues = [
        _issue("1" * 64, row, is_ioc=True, artifact_type="domain"),
        _issue("2" * 64, row, is_ioc=False, artifact_type="filename"),
        _issue("3" * 64, row, is_ioc=False, artifact_type="path"),
    ]
    review = EditionReviewService.from_rows(EDITION_ID, [row], repair_issues=issues)

    assert review.items[0].active_repair_count == 3
    # filename/path are losses worth showing but are not indicators to arbitrate.
    assert review.items[0].unresolved_repair_count == 1
    assert review.unresolved_repair_count == 1
    assert not review.can_accept


@pytest.mark.asyncio
async def test_audit10_excluded_article_does_not_gate_the_edition() -> None:
    included = _row(SUBJECT_A, RUN_A, position=1, decision=PublicationDecision.INCLUDE)
    excluded_run = uuid4()
    excluded = _row(SUBJECT_B, excluded_run, position=2, decision=PublicationDecision.EXCLUDE)
    issues = [
        _issue("4" * 64, excluded, is_ioc=True),
        _issue(
            "5" * 64,
            excluded,
            kind=ProductionRepairIssueKind.REJECTED_RULE,
            is_ioc=False,
            artifact_type="yara",
        ),
    ]

    review = EditionReviewService.from_rows(EDITION_ID, [included, excluded], repair_issues=issues)

    # The item keeps a truthful count for the desk...
    assert review.items[1].unresolved_repair_count == 2
    # ...but the excluded article is out of the publication scope.
    assert review.unresolved_repair_count == 0
    assert review.repair_review_complete
    assert review.can_accept

    class _Reader:
        async def list_issue_views(
            self, _edition_id: UUID, subject_id: UUID | None = None
        ) -> tuple[Any, ...]:
            return tuple(
                issue for issue in issues if subject_id is None or issue.subject_id == subject_id
            )

        async def list_supplemental_source_issues(
            self, _edition_id: UUID, _subject_id: UUID | None = None
        ) -> tuple[Any, ...]:
            return ()

    uow = _Uow(runs=[], artifacts=[], rows=[included, excluded])
    page = await EditionRepairReadService(_factory(uow), _Reader()).list(  # type: ignore[arg-type]
        EDITION_ID, status="all", limit=50
    )

    # Both issues stay listed and arbitrable...
    assert len(page.items) == 2
    assert all(not item.in_publication_scope for item in page.items)
    # ...while the sign-off counters describe only the publication scope.
    assert page.summary.unresolved_total == 0
    assert page.summary.rejected_iocs_to_review == 0
    assert page.summary.rejected_rules_to_review == 0
    assert page.summary.articles_with_repairs == 1
    assert page.summary.articles_needing_rebuild == 0


def test_audit10_can_accept_matches_the_backend_policy() -> None:
    row = _row(decision=PublicationDecision.INCLUDE)
    open_issue = _issue("6" * 64, row)
    blocked = EditionReviewService.from_rows(EDITION_ID, [row], repair_issues=[open_issue])
    assert (blocked.can_accept, blocked.repair_review_complete) == (False, False)

    open_issue.effective_decision = SimpleNamespace(
        id=uuid4(), action=ProductionRepairAction.INCLUDE, reason="arbitré"
    )
    open_issue.projection_applied = True
    resolved = EditionReviewService.from_rows(EDITION_ID, [row], repair_issues=[open_issue])
    assert (resolved.can_accept, resolved.repair_review_complete) == (True, True)
    assert resolved.unresolved_repair_count == 0


def test_audit10_datetime_import_is_used() -> None:
    # Keeps the datetime import meaningful for decision timestamps below.
    assert datetime.now(UTC).tzinfo is UTC


# --------------------------------------------------------------------------
# AUDIT 8 — an append-only decision must never freeze an article forever
# --------------------------------------------------------------------------


UNVERIFIABLE_VALUE = "not a domain at all"


def test_audit8_unbuildable_include_is_detected_before_it_is_recorded() -> None:
    entry = {
        "repair_key": "9" * 64,
        "source_id": "S1",
        "source_url": SOURCE_ONE,
        "artifact_type": "domain",
        "model_run_id": "run-1",
    }
    assert not repair_include_is_buildable(
        ProductionRepairIssueKind.REJECTED_INDICATOR, entry, UNVERIFIABLE_VALUE
    )
    assert repair_include_is_buildable(
        ProductionRepairIssueKind.REJECTED_INDICATOR, entry, "good.lot23-audit.com"
    )
    # A missing payload can never be rebuilt either.
    assert not repair_include_is_buildable(
        ProductionRepairIssueKind.REJECTED_INDICATOR, entry, None
    )
    rule_entry = dict(entry) | {"artifact_type": "yara", "name": "Lot23"}
    assert repair_include_is_buildable(
        ProductionRepairIssueKind.REJECTED_RULE, rule_entry, YARA_BODY
    )
    assert not repair_include_is_buildable(
        ProductionRepairIssueKind.REJECTED_RULE, rule_entry, "   "
    )


@pytest.mark.asyncio
async def test_audit8_recorded_unbuildable_include_never_freezes_the_article() -> None:
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    good = _ioc_entry(3)
    bad = {
        "source_id": "S1",
        "source_title": "One",
        "source_url": SOURCE_ONE,
        "proposal_kind": "artifact",
        "artifact_type": "domain",
        "reason_code": "normalization_error",
        "value": UNVERIFIABLE_VALUE,
        "value_sha256": _sha256(UNVERIFIABLE_VALUE),
        "model_run_id": "run-1",
    }
    good["source_url"] = SOURCE_ONE
    good["source_id"] = "S1"
    good_key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=SOURCE_ONE,
        artifact_type="domain",
        value=good["value"],
    )
    bad_key = repair_key_for_rejection(
        edition_id=EDITION_ID,
        subject_id=SUBJECT_A,
        kind=ProductionRepairIssueKind.REJECTED_INDICATOR,
        source_url=SOURCE_ONE,
        artifact_type="domain",
        value=UNVERIFIABLE_VALUE,
    )
    base = await _extraction_artifact(store, [good, bad])
    uow = _Uow(
        runs=[_run()],
        artifacts=[base],
        decisions=[
            _decision(
                good_key,
                ProductionRepairIssueKind.REJECTED_INDICATOR,
                ProductionRepairAction.INCLUDE,
            ),
            _decision(
                bad_key,
                ProductionRepairIssueKind.REJECTED_INDICATOR,
                ProductionRepairAction.INCLUDE,
            ),
        ],
    )

    result = await ProductionRepairProjectionService(
        _factory(uow),
        store,  # type: ignore[arg-type]
    ).project_effective_extraction(RUN_A, actor_id="analyst")

    # The article still builds, the good include still lands, and the
    # impossible one is recorded rather than raised.
    assert result.changed
    assert result.included_repair_keys == (good_key,)
    assert result.unbuildable_repair_keys == (bad_key,)
    assert result.unresolved_count == 0
    projected = technical_extraction_from_json(
        await store.read_json(result.artifact.canonical_blob_id)  # type: ignore[arg-type]
    )
    assert [item.value for item in projected.items] == [good["value"]]
    marker = result.artifact.metadata["repair_projection"]
    assert marker["unbuildable_repair_keys"] == [bad_key]
