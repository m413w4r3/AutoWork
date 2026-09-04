"""Focused unit coverage for the production repair foundation."""

from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from types import SimpleNamespace
from uuid import UUID, uuid4

import pytest

from cti_app.application.production_artifact_store import (
    MAX_ARTIFACT_BYTES,
    MAX_REPAIR_EVIDENCE_BYTES,
    ProductionArtifactStore,
)
from cti_app.application.production_repairs import (
    ProductionRepairDecisionService,
    ProductionRepairIssueService,
    build_repair_evidence_pack,
    repair_key_for_rejection,
)
from cti_app.application.production_stages import ExtractionService
from cti_app.domain.editions import EditionStatus
from cti_app.domain.production import (
    ProductionArtifactStatus,
    ProductionRepairAction,
    ProductionRepairDecision,
    ProductionRepairIssueKind,
)

EDITION_ID = uuid4()
SUBJECT_ID = uuid4()
RUN_ID = uuid4()
ARTIFACT_ID = uuid4()
SOURCE_URL = "https://example.test/report/"


class _BlobCatalog:
    def __init__(self) -> None:
        self.contents: dict[UUID, bytes] = {}
        self.addresses: dict[tuple[str, str], UUID] = {}
        self.calls: list[tuple[str, str, int]] = []

    async def ingest(self, source: object, *, logical_bucket: str, mime_type: str) -> object:
        del mime_type
        content = source.read()  # type: ignore[union-attr]
        digest = hashlib.sha256(content).hexdigest()
        key = (logical_bucket, digest)
        blob_id = self.addresses.setdefault(key, uuid4())
        self.contents[blob_id] = content
        self.calls.append((logical_bucket, digest, len(content)))
        return SimpleNamespace(id=blob_id)

    async def read(self, blob_id: UUID, *, max_bytes: int) -> bytes:
        content = self.contents[blob_id]
        if len(content) > max_bytes:
            raise ValueError("too large")
        return content


def _key(
    *,
    edition_id: UUID = EDITION_ID,
    subject_id: UUID = SUBJECT_ID,
    source_url: str = SOURCE_URL,
    artifact_type: str = "sigma",
    value: str = "title: example\nlogsource:\n  product: linux",
) -> str:
    return repair_key_for_rejection(
        edition_id=edition_id,
        subject_id=subject_id,
        kind=ProductionRepairIssueKind.REJECTED_RULE,
        source_url=source_url,
        artifact_type=artifact_type,
        value=value,
    )


def test_repair_key_ignores_run_and_proposal_identity_but_changes_with_content() -> None:
    first = _key()
    rerun = _key()
    assert first == rerun
    assert _key(source_url="https://example.test/other") != first
    assert _key(artifact_type="yara") != first
    assert _key(value="different rule body") != first


@pytest.mark.asyncio
async def test_repair_evidence_round_trip_is_integral_and_inert() -> None:
    catalog = _BlobCatalog()
    store = ProductionArtifactStore(catalog)  # type: ignore[arg-type]
    body = "rule body that must remain inert\n" + ("A" * 700)
    pack = build_repair_evidence_pack(
        [
            {
                "repair_key": _key(value=body),
                "source_id": "S6",
                "source_url": SOURCE_URL,
                "proposal_kind": "rule",
                "artifact_type": "sigma",
                "reason_code": "source_rule_evidence_missing",
                "value": body,
                "value_sha256": hashlib.sha256(body.encode()).hexdigest(),
            }
        ]
    )

    blob_id = await store.put_repair_evidence(pack)
    loaded = await store.read_repair_evidence(blob_id)

    assert loaded["entries"][0]["value"] == body
    assert len(loaded["entries"][0]["value"]) > 512
    assert catalog.calls[0][0] == "production-repair-evidence"
    assert "compile" not in body


@pytest.mark.asyncio
async def test_repair_evidence_keeps_more_than_200_entries() -> None:
    catalog = _BlobCatalog()
    store = ProductionArtifactStore(catalog)  # type: ignore[arg-type]
    entries = [
        {
            "repair_key": _key(value=f"rule {index}"),
            "source_id": "S6",
            "source_url": SOURCE_URL,
            "proposal_kind": "rule",
            "artifact_type": "sigma",
            "reason_code": "source_rule_evidence_missing",
            "value": f"rule {index}",
            "value_sha256": hashlib.sha256(f"rule {index}".encode()).hexdigest(),
        }
        for index in range(201)
    ]

    blob_id = await store.put_repair_evidence(build_repair_evidence_pack(entries))
    loaded = await store.read_repair_evidence(blob_id)
    assert len(loaded["entries"]) == 201


@pytest.mark.asyncio
async def test_repair_evidence_uses_specific_limit_and_keeps_artifact_limit_unchanged() -> None:
    assert MAX_REPAIR_EVIDENCE_BYTES > MAX_ARTIFACT_BYTES
    store = ProductionArtifactStore(_BlobCatalog())  # type: ignore[arg-type]
    oversized = {
        "schema_version": "1",
        "entries": [{"value": "x" * MAX_REPAIR_EVIDENCE_BYTES}],
    }

    with pytest.raises(ValueError, match="Repair evidence pack exceeds"):
        await store.put_repair_evidence(oversized)


class _ExtractionArtifacts:
    def __init__(self) -> None:
        self.items: list[object] = []

    async def list_for_run(self, _run_id: UUID) -> list[object]:
        return self.items

    async def append(self, artifact: object) -> None:
        self.items.append(artifact)

    async def mark_downstream_stale(self, _run_id: UUID, _stage: str) -> None:
        return None


class _ExtractionUow:
    def __init__(self) -> None:
        self.production_artifacts = _ExtractionArtifacts()
        self.committed = False

    async def __aenter__(self) -> _ExtractionUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _ExtractionFactory:
    def __init__(self, uow: _ExtractionUow) -> None:
        self.uow = uow

    def __call__(self) -> _ExtractionUow:
        return self.uow


@pytest.mark.asyncio
async def test_extraction_metadata_keeps_only_bounded_repair_pointer() -> None:
    uow = _ExtractionUow()
    artifact = await ExtractionService(_ExtractionFactory(uow)).store_extraction_result(
        run_id=RUN_ID,
        subject_id=SUBJECT_ID,
        input_hash="a" * 64,
        raw_result="raw",
        canonical_json={"items": []},
        verification_diagnostics={"q2_rejected_rule_count": 201},
        repair_evidence_blob_id=UUID("00000000-0000-0000-0000-000000000099"),
        repair_evidence_entry_count=201,
    )

    assert artifact.metadata["repair_evidence"] == {
        "schema_version": "1",
        "blob_id": "00000000-0000-0000-0000-000000000099",
        "entry_count": 201,
    }
    assert artifact.metadata["deterministic_verification"] == {
        "q2_rejected_rule_count": 201
    }


class _DecisionRepository:
    def __init__(self) -> None:
        self.items: list[ProductionRepairDecision] = []

    async def append(self, decision: ProductionRepairDecision) -> None:
        self.items.append(decision)

    async def effective_decisions(
        self, edition_id: UUID, subject_id: UUID | None = None
    ) -> tuple[ProductionRepairDecision, ...]:
        latest: dict[tuple[UUID, str], ProductionRepairDecision] = {}
        for decision in sorted(self.items, key=lambda item: (item.created_at, item.id)):
            if decision.edition_id != edition_id:
                continue
            if subject_id is not None and decision.subject_id != subject_id:
                continue
            latest[(decision.subject_id, decision.repair_key)] = decision
        return tuple(latest.values())


class _DecisionUow:
    def __init__(
        self,
        *,
        generation: int = 2,
        artifact_status: object = ProductionArtifactStatus.VERIFIED,
    ) -> None:
        async def get_edition(_id: UUID) -> object:
            return SimpleNamespace(status=EditionStatus.REVIEW)

        async def get_run(_id: UUID) -> object:
            return SimpleNamespace(
                id=RUN_ID,
                edition_id=EDITION_ID,
                subject_id=SUBJECT_ID,
                pipeline_generation=generation,
            )

        async def get_artifact(_id: UUID) -> object:
            return SimpleNamespace(
                id=ARTIFACT_ID,
                production_run_id=RUN_ID,
                status=artifact_status,
            )

        self.editions = SimpleNamespace(get_for_update=get_edition)
        self.subject_production_runs = SimpleNamespace(
            get_for_update=get_run
        )
        self.production_artifacts = SimpleNamespace(get=get_artifact)
        self.production_repair_decisions = _DecisionRepository()
        self.committed = False

    async def __aenter__(self) -> _DecisionUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None

    async def commit(self) -> None:
        self.committed = True


class _DecisionFactory:
    def __init__(self, uow: _DecisionUow) -> None:
        self.uow = uow

    def __call__(self) -> _DecisionUow:
        return self.uow


def _decision(
    *,
    action: ProductionRepairAction,
    when: datetime,
    decision_id: UUID | None = None,
) -> ProductionRepairDecision:
    return ProductionRepairDecision(
        id=decision_id or uuid4(),
        edition_id=EDITION_ID,
        subject_id=SUBJECT_ID,
        production_run_id=RUN_ID,
        observed_artifact_id=ARTIFACT_ID,
        observed_pipeline_generation=2,
        repair_key=_key(),
        issue_kind=ProductionRepairIssueKind.REJECTED_RULE,
        action=action,
        actor_id=" analyst ",
        reason="  reviewed  ",
        created_at=when,
    )


@pytest.mark.asyncio
async def test_effective_decision_is_last_append_only_entry() -> None:
    uow = _DecisionUow()
    factory = _DecisionFactory(uow)
    first = _decision(
        action=ProductionRepairAction.EXCLUDE,
        when=datetime(2026, 1, 1, tzinfo=UTC),
    )
    second = _decision(
        action=ProductionRepairAction.INCLUDE,
        when=datetime(2026, 1, 2, tzinfo=UTC),
    )
    uow.production_repair_decisions.items.extend([first, second])

    effective = await ProductionRepairDecisionService(factory).effective_decisions(
        EDITION_ID, SUBJECT_ID
    )
    assert effective == (second,)


def test_invalid_action_is_rejected_by_issue_kind() -> None:
    with pytest.raises(ValueError, match="incompatible"):
        _decision(
            action=ProductionRepairAction.CONTINUE_WITHOUT_SOURCE,
            when=datetime.now(UTC),
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generation,artifact_status",
    [(3, ProductionArtifactStatus.VERIFIED), (2, ProductionArtifactStatus.STALE)],
)
async def test_stale_generation_or_artifact_is_rejected(
    generation: int, artifact_status: ProductionArtifactStatus
) -> None:
    uow = _DecisionUow(generation=generation, artifact_status=artifact_status)
    service = ProductionRepairDecisionService(_DecisionFactory(uow))
    with pytest.raises(ValueError, match="production_repair_stale"):
        await service.decide(
            edition_id=EDITION_ID,
            subject_id=SUBJECT_ID,
            production_run_id=RUN_ID,
            observed_artifact_id=ARTIFACT_ID,
            observed_pipeline_generation=2,
            repair_key=_key(),
            issue_kind=ProductionRepairIssueKind.REJECTED_RULE,
            action=ProductionRepairAction.INCLUDE,
            actor_id="analyst",
        )


class _IssueArtifacts:
    async def get_current(self, _run_id: UUID, _stage: str) -> object:
        return SimpleNamespace(
            id=ARTIFACT_ID,
            version=1,
            status=ProductionArtifactStatus.VERIFIED,
            metadata={"repair_evidence": {"blob_id": str(uuid4())}},
        )


class _IssueUow:
    def __init__(self, pack: dict[str, object]) -> None:
        async def list_runs(_edition_id: UUID) -> list[object]:
            return [
                SimpleNamespace(
                    id=RUN_ID,
                    subject_id=SUBJECT_ID,
                    pipeline_generation=2,
                )
            ]

        self.subject_production_runs = SimpleNamespace(
            list_for_edition=list_runs
        )
        self.production_artifacts = _IssueArtifacts()
        self.production_repair_decisions = _DecisionRepository()
        self.pack = pack

    async def __aenter__(self) -> _IssueUow:
        return self

    async def __aexit__(self, *_args: object) -> None:
        return None


class _IssueFactory:
    def __init__(self, uow: _IssueUow) -> None:
        self.uow = uow

    def __call__(self) -> _IssueUow:
        return self.uow


class _IssueStore:
    def __init__(self, pack: dict[str, object]) -> None:
        self.pack = pack

    async def read_repair_evidence(self, _blob_id: UUID) -> dict[str, object]:
        return self.pack


@pytest.mark.asyncio
async def test_issue_reader_returns_all_entries_and_detail_loads_one_body() -> None:
    entries = [
        {
            "repair_key": _key(value=f"rule {index}"),
            "source_id": "S6",
            "source_url": SOURCE_URL,
            "proposal_kind": "rule",
            "artifact_type": "sigma",
            "reason_code": "source_rule_evidence_missing",
            "value": f"rule {index}",
            "value_sha256": hashlib.sha256(f"rule {index}".encode()).hexdigest(),
        }
        for index in range(201)
    ]
    body = "exact body\n" + "x" * 700
    entries[0]["value"] = body
    entries[0]["value_sha256"] = hashlib.sha256(body.encode()).hexdigest()
    entries[0]["repair_key"] = _key(value=body)
    pack = build_repair_evidence_pack(entries)
    uow = _IssueUow(pack)
    service = ProductionRepairIssueService(
        _IssueFactory(uow), _IssueStore(pack)  # type: ignore[arg-type]
    )

    issues = await service.list_issues(EDITION_ID, SUBJECT_ID)
    detail = await service.get_issue(EDITION_ID, issues[0].repair_key, SUBJECT_ID)

    assert len(issues) == 201
    assert all(len(issue.preview) <= 512 for issue in issues)
    assert detail is not None
    assert detail.value == body
