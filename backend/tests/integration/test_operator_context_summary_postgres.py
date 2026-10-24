from __future__ import annotations

import hashlib
from datetime import UTC, datetime
from typing import cast
from uuid import UUID, uuid4

import httpx
import pytest
from httpx import ASGITransport
from sqlalchemy import DateTime, Uuid, column, select, text, update, values

from cti_app.application.blobs import BlobCatalogService
from cti_app.application.bridge_investigator import ScriptedTurnTransport
from cti_app.application.identity import Identity, IdentityScope
from cti_app.application.production_artifact_store import ProductionArtifactStore
from cti_app.domain.investigation import (
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationExecutionMode,
    InvestigationFinding,
)
from cti_app.infrastructure.blob_storage.filesystem import FilesystemBlobStore
from cti_app.infrastructure.database.models.investigation import EvidenceObservationRow
from cti_app.infrastructure.database.repositories import investigation as investigation_repository
from cti_app.infrastructure.database.repositories.investigation import _keyset_predicate
from tests.workflow_emulator.investigation_support import (
    build_investigation_app,
    composition_factory,
    investigation_settings,
    seed_verified_article,
)

pytestmark = pytest.mark.integration


class _OperatorClientIdentityProvider:
    async def current(self) -> Identity:
        return Identity(
            actor_id="human:operator-context-integration",
            scopes=frozenset({IdentityScope.HUMAN_OPERATOR, IdentityScope.OPERATOR_CLIENT}),
        )


@pytest.mark.asyncio
async def test_postgres_timestamp_keyset_predicate_handles_null_pivots(uow_factory) -> None:
    """Exercise ASC/DESC timestamp seeks, including NULL pivots, in PostgreSQL."""
    first = datetime(2026, 1, 1, tzinfo=UTC)
    second = datetime(2026, 1, 2, tzinfo=UTC)
    rows = (
        (first, UUID(int=1)),
        (first, UUID(int=2)),
        (second, UUID(int=3)),
        (None, UUID(int=4)),
        (None, UUID(int=5)),
    )
    source = (
        values(
            column("observed_at", DateTime(timezone=True)),
            column("id", Uuid(as_uuid=True)),
            name="evidence_positions",
        )
        .data(rows)
        .alias("evidence_positions")
    )
    cases = (
        (False, first, UUID(int=1), (UUID(int=2), UUID(int=3), UUID(int=4), UUID(int=5))),
        (False, None, UUID(int=4), (UUID(int=5),)),
        (True, second, UUID(int=3), (UUID(int=2), UUID(int=1), UUID(int=5), UUID(int=4))),
        (True, None, UUID(int=5), (UUID(int=4),)),
    )

    async with uow_factory() as uow:
        assert uow._session is not None
        for descending, pivot_time, pivot_id, expected in cases:
            timestamp_order = (
                source.c.observed_at.desc().nulls_last()
                if descending
                else source.c.observed_at.asc().nulls_last()
            )
            id_order = source.c.id.desc() if descending else source.c.id.asc()
            statement = (
                select(source.c.id)
                .where(
                    _keyset_predicate(
                        source.c.observed_at,
                        source.c.id,
                        sort_key=pivot_time,
                        item_id=pivot_id,
                        descending=descending,
                    )
                )
                .order_by(timestamp_order, id_order)
            )
            actual = tuple((await uow._session.scalars(statement)).all())
            assert actual == expected, (descending, pivot_time, pivot_id)


@pytest.mark.asyncio
async def test_postgres_repositories_page_nullable_evidence_and_finding_versions(
    uow_factory, tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "context-keyset"), uow_factory)
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=uuid4().hex[:10],
    )
    investigation_id = seeded.investigation.id
    timestamps = (
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 1, tzinfo=UTC),
        datetime(2025, 1, 2, tzinfo=UTC),
    )
    observations: list[EvidenceObservation] = []
    for index, observed_at in enumerate(timestamps):
        content = f"repository pagination observation {index}".encode()
        blob_id = await artifact_store.put_bytes(
            content,
            bucket="context-keyset-observations",
            mime_type="application/octet-stream",
        )
        observations.append(
            EvidenceObservation(
                id=UUID(int=40_000 + index),
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=blob_id,
                sha256=hashlib.sha256(content).hexdigest(),
                source="context-keyset-test",
                source_version="1",
                observed_at=observed_at,
                object_type="test_observation",
                object_id=str(index),
            )
        )
    null_observation_ids = (UUID(int=40_003), UUID(int=40_004))
    null_payload = b"repository pagination NULL timestamp observation"
    null_blob_id = await artifact_store.put_bytes(
        null_payload,
        bucket="context-keyset-observations",
        mime_type="application/octet-stream",
    )
    observations.append(
        EvidenceObservation(
            id=null_observation_ids[1],
            investigation_id=investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=null_blob_id,
            sha256=hashlib.sha256(null_payload).hexdigest(),
            source="context-keyset-test",
            source_version="1",
            observed_at=datetime(2025, 1, 4, tzinfo=UTC),
            object_type="test_observation",
            object_id="null-1",
        )
    )
    null_payload = b"repository pagination second NULL timestamp observation"
    null_blob_id = await artifact_store.put_bytes(
        null_payload,
        bucket="context-keyset-observations",
        mime_type="application/octet-stream",
    )
    observations.append(
        EvidenceObservation(
            id=null_observation_ids[0],
            investigation_id=investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=null_blob_id,
            sha256=hashlib.sha256(null_payload).hexdigest(),
            source="context-keyset-test",
            source_version="1",
            observed_at=datetime(2025, 1, 5, tzinfo=UTC),
            object_type="test_observation",
            object_id="null-0",
        )
    )

    original_converter = investigation_repository._evidence_observation_from_row

    def convert_test_null_row(row: EvidenceObservationRow) -> EvidenceObservation:
        if row.observed_at is not None:
            return original_converter(row)
        # The production schema/domain require timestamps. Bypass only that
        # constructor guard in this isolated test after making its disposable
        # table nullable, so the real repository seek and ordering SQL sees NULL.
        observation = object.__new__(EvidenceObservation)
        for field_name, value in (
            ("id", row.id),
            ("investigation_id", row.investigation_id),
            ("kind", EvidenceKind(row.kind)),
            ("blob_id", row.blob_id),
            ("sha256", row.sha256),
            ("source", row.source),
            ("source_version", row.source_version),
            ("observed_at", None),
            ("object_type", row.object_type),
            ("object_id", row.object_id),
            ("limitations", tuple(row.limitations)),
            ("invocation_ref", row.invocation_ref),
        ):
            object.__setattr__(observation, field_name, value)
        return cast(EvidenceObservation, observation)

    monkeypatch.setattr(
        investigation_repository,
        "_evidence_observation_from_row",
        convert_test_null_row,
    )

    finding_v1_id = UUID(int=41_001)
    finding_v2_id = UUID(int=41_002)
    finding_created_at = datetime(2025, 2, 1, tzinfo=UTC)
    finding_v1 = InvestigationFinding(
        id=finding_v1_id,
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="Version one evidence-backed capability finding.",
        observation_refs=(observations[0].id,),
        version=1,
        created_at=finding_created_at,
    )
    finding_v2 = InvestigationFinding(
        id=finding_v2_id,
        investigation_id=investigation_id,
        kind=FindingKind.CAPABILITY,
        assessment=FindingAssessment.SUPPORTED,
        statement="Version two revised evidence-backed capability finding.",
        observation_refs=(observations[0].id,),
        version=2,
        supersedes=finding_v1_id,
        created_at=datetime(2025, 2, 2, tzinfo=UTC),
    )

    async with uow_factory() as uow:
        assert uow._session is not None
        # This is a test-only shim in the temporary migrated database. The
        # production model/migration retain NOT NULL, and UOW exit rolls the
        # schema change and seeded rows back before the next integration test.
        await uow._session.execute(
            text("ALTER TABLE evidence_observations ALTER COLUMN observed_at DROP NOT NULL")
        )
        for observation in observations:
            await uow.evidence_observations.append_if_absent(observation)
        await uow.investigation_findings.append_version(finding_v1, expected_previous_id=None)
        await uow.investigation_findings.append_version(
            finding_v2, expected_previous_id=finding_v1_id
        )
        await uow._session.execute(
            update(EvidenceObservationRow)
            .where(EvidenceObservationRow.id.in_(null_observation_ids))
            .values(observed_at=None)
        )

        full_evidence = await uow.evidence_observations.list_for_investigation(investigation_id)
        assert tuple(item.id for item in full_evidence[-2:]) == tuple(
            sorted(null_observation_ids, key=lambda item_id: item_id.int)
        )
        tie_ids = tuple(item.id for item in full_evidence if item.observed_at == timestamps[0])
        assert tie_ids == (UUID(int=40_000), UUID(int=40_001))

        for page_size in (1, 2, 3):
            cursor: str | None = None
            collected: list[UUID] = []
            while True:
                page = await uow.evidence_observations.page_for_investigation(
                    investigation_id,
                    cursor=cursor,
                    limit=page_size,
                    descending=False,
                    section="evidence_index",
                )
                collected.extend(item.id for item in page.items)
                if not page.has_more:
                    break
                assert page.next_cursor is not None
                cursor = page.next_cursor
            assert tuple(collected) == tuple(item.id for item in full_evidence)

        full_history = await uow.investigation_findings.list_for_investigation(investigation_id)
        full_current_page = await uow.investigation_findings.page_for_investigation(
            investigation_id,
            cursor=None,
            limit=1_000,
            descending=True,
            section="findings",
            latest_only=True,
        )
        assert tuple(
            item.id for item in full_history if item.id in {finding_v1_id, finding_v2_id}
        ) == (
            finding_v1_id,
            finding_v2_id,
        )
        assert tuple(item.id for item in full_current_page.items) == (finding_v2_id,)
        assert finding_v1_id not in {item.id for item in full_current_page.items}

        for section, latest_only, descending, expected_ids in (
            ("findings", True, True, tuple(item.id for item in full_current_page.items)),
            (
                "finding_versions",
                False,
                False,
                tuple(item.id for item in full_history),
            ),
        ):
            for page_size in (1, 2):
                cursor = None
                collected = []
                while True:
                    page = await uow.investigation_findings.page_for_investigation(
                        investigation_id,
                        cursor=cursor,
                        limit=page_size,
                        descending=descending,
                        section=section,
                        latest_only=latest_only,
                    )
                    collected.extend(item.id for item in page.items)
                    if not page.has_more:
                        break
                    assert page.next_cursor is not None
                    cursor = page.next_cursor
                assert tuple(collected) == expected_ids, (section, page_size)


@pytest.mark.asyncio
async def test_operator_context_summary_and_pages_preserve_postgres_cas(
    uow_factory, tmp_path
) -> None:
    artifact_store = ProductionArtifactStore(
        BlobCatalogService(FilesystemBlobStore(tmp_path / "operator-context-summary"), uow_factory)
    )
    seeded = await seed_verified_article(
        uow_factory,
        artifact_store,
        suffix=uuid4().hex[:10],
    )
    assert seeded.seed_observation is not None
    settings = investigation_settings(providers=False)
    factory = composition_factory(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        settings=settings,
        transport=ScriptedTurnTransport([]),
    )
    app, _registry, _dispatcher = build_investigation_app(
        uow_factory=uow_factory,
        artifact_store=artifact_store,
        composition_factory=factory,
    )
    app.state.identity_provider = _OperatorClientIdentityProvider()

    async with httpx.AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        started = await client.post(
            f"/api/subjects/{seeded.subject.id}/investigations",
            json={
                "production_run_id": str(seeded.run.id),
                "synthesis_artifact_id": str(seeded.synthesis.id),
                "synthesis_sha256": seeded.synthesis.input_hash,
                "execution_mode": InvestigationExecutionMode.OPERATOR_LED.value,
            },
        )
        assert started.status_code == 200, started.text
        investigation_id = seeded.investigation.id
        async with uow_factory() as uow:
            state = await uow.investigation_states.get(investigation_id)
        assert state is not None

        params = {
            "view": "summary",
            "expected_version": state.version,
            "expected_sha256": state.sha256,
        }
        summary = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params=params,
        )
        assert summary.status_code == 200, summary.text
        summary_payload = summary.json()
        unchanged = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params={
                **params,
                "since_version": state.version,
                "since_fingerprint": summary_payload["fingerprint"],
            },
        )
        missing_fingerprint = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params={**params, "since_version": state.version},
        )

        observation_bytes = b"operator context delta invalidation observation"
        observation_blob_id = await artifact_store.put_bytes(
            observation_bytes,
            bucket="operator-context-summary-observations",
            mime_type="application/octet-stream",
        )
        added_observation = EvidenceObservation(
            investigation_id=investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=observation_blob_id,
            sha256=hashlib.sha256(observation_bytes).hexdigest(),
            source="operator-context-delta-test",
            source_version="1",
            observed_at=datetime.now(UTC),
            object_type="test_observation",
            object_id="added-after-summary",
        )
        async with uow_factory() as uow:
            await uow.evidence_observations.append_if_absent(added_observation)
            await uow.commit()
        after_observation = await client.get(
            f"/api/investigations/{investigation_id}/operator/context",
            params={
                **params,
                "since_version": state.version,
                "since_fingerprint": summary_payload["fingerprint"],
            },
        )
        evidence_page = await client.get(
            f"/api/investigations/{investigation_id}/operator/context/section/evidence_index",
            params={
                "offset": 0,
                "limit": 20,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )
        oversized_page = await client.get(
            f"/api/investigations/{investigation_id}/operator/context/section/evidence_index",
            params={
                "offset": 0,
                "limit": 21,
                "expected_version": state.version,
                "expected_sha256": state.sha256,
            },
        )

    assert summary.status_code == 200, summary.text
    summary_payload = summary.json()
    assert summary_payload["schema"] == "investigation-operator-context-summary.v1"
    assert summary_payload["working_state_version"] == state.version
    assert summary_payload["working_state_sha256"] == state.sha256
    assert summary_payload["source"]["synthesis_sha256"] == seeded.synthesis.input_hash
    assert "context" not in summary_payload
    assert len(summary_payload["pointers"]["section_cursors"]) == 29
    assert len(summary_payload["pointers"]["endpoints"]) == 3
    assert summary_payload["pointers"]["section_limit"] == 20
    assert unchanged.status_code == 200, unchanged.text
    unchanged_delta = unchanged.json()["delta_since_version"]
    assert unchanged_delta["strategy"] == "section_hashes"
    assert unchanged_delta["pointers_to_check"] == []
    assert missing_fingerprint.status_code == 200, missing_fingerprint.text
    missing_delta = missing_fingerprint.json()["delta_since_version"]
    assert missing_delta["strategy"] == "all_sections_conservative"
    assert len(missing_delta["pointers_to_check"]) == 32
    assert after_observation.status_code == 200, after_observation.text
    observation_delta = after_observation.json()["delta_since_version"]
    assert observation_delta["strategy"] == "all_sections_conservative"
    assert len(observation_delta["pointers_to_check"]) == 32
    assert evidence_page.status_code == 200, evidence_page.text
    assert evidence_page.json()["schema"] == "investigation-operator-context-section.v1"
    assert evidence_page.json()["total"] == 2
    assert str(seeded.seed_observation.id) in {
        item["evidence_id"] for item in evidence_page.json()["items"]
    }
    assert oversized_page.status_code == 422
