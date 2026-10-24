from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, Uuid, column, select, table
from sqlalchemy.dialects import postgresql

import cti_app.infrastructure.database.repositories.investigation as investigation_repositories
from cti_app.application.context_pagination import (
    ContextOmissionManifestEntryV1,
    ContextOmissionReason,
    decode_keyset_cursor,
    encode_keyset_cursor,
    make_keyset_page,
    verify_page_sha256,
)
from cti_app.application.investigation_contracts import ArticleContextV1
from cti_app.application.investigation_persistence import (
    InMemoryEvidenceObservationRepository,
    InMemoryFindingRepository,
    InMemoryToolInvocationRepository,
)
from cti_app.application.investigation_service import InvestigationService
from cti_app.application.research_context_builder import ArticleSnapshot
from cti_app.domain.investigation import (
    DisclosureBounds,
    EvidenceKind,
    EvidenceObservation,
    FindingAssessment,
    FindingKind,
    InvestigationBudget,
    InvestigationEnvelope,
    InvestigationFinding,
    TLPLevel,
    ToolInvocation,
)
from cti_app.infrastructure.database.repositories.investigation import _keyset_predicate


@pytest.mark.asyncio
async def test_all_three_keyset_ports_keep_stable_order_and_chained_cas() -> None:
    investigation_id = uuid4()
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    invocations = InMemoryToolInvocationRepository()
    observations = InMemoryEvidenceObservationRepository()
    findings = InMemoryFindingRepository()

    for index in range(1, 7):
        await invocations.add_if_absent(
            ToolInvocation(
                id=UUID(int=index),
                investigation_id=investigation_id,
                operation_key=f"operation-{index}",
                tool_name="test.read",
                schema_version="1",
                normalized_parameters={},
                purpose="pagination fixture",
                created_at=timestamp,
                updated_at=timestamp,
            )
        )
        await observations.append_if_absent(
            EvidenceObservation(
                id=UUID(int=index),
                investigation_id=investigation_id,
                kind=EvidenceKind.OTHER,
                blob_id=uuid4(),
                sha256=f"{index:064x}",
                source="test.source",
                source_version="1",
                observed_at=timestamp,
                object_type="fixture",
                object_id=str(index),
            )
        )
        await findings.append_version(
            InvestigationFinding(
                id=UUID(int=1_000 + index),
                investigation_id=investigation_id,
                kind=FindingKind.CAPABILITY,
                assessment=FindingAssessment.INCONCLUSIVE,
                statement=f"Finding {index}",
                observation_refs=(),
                created_at=timestamp,
            ),
            expected_previous_id=None,
        )

    first_pages = (
        await invocations.page_for_investigation(investigation_id, cursor=None, limit=2),
        await observations.page_for_investigation(investigation_id, cursor=None, limit=2),
        await findings.page_for_investigation(investigation_id, cursor=None, limit=2),
    )
    await invocations.add_if_absent(
        ToolInvocation(
            id=UUID(int=0),
            investigation_id=investigation_id,
            operation_key="insert-before-cursor",
            tool_name="test.read",
            schema_version="1",
            normalized_parameters={},
            purpose="insert before the saved keyset boundary",
            created_at=timestamp,
            updated_at=timestamp,
        )
    )
    await observations.append_if_absent(
        EvidenceObservation(
            id=UUID(int=0),
            investigation_id=investigation_id,
            kind=EvidenceKind.OTHER,
            blob_id=uuid4(),
            sha256="f" * 64,
            source="test.inserted",
            source_version="1",
            observed_at=timestamp,
            object_type="fixture",
            object_id="inserted-before-cursor",
        )
    )
    await findings.append_version(
        InvestigationFinding(
            id=UUID(int=999),
            investigation_id=investigation_id,
            kind=FindingKind.CAPABILITY,
            assessment=FindingAssessment.INCONCLUSIVE,
            statement="Inserted before the saved keyset boundary",
            observation_refs=(),
            created_at=timestamp,
        ),
        expected_previous_id=None,
    )

    page_sets = (
        (invocations, "tool_invocations", first_pages[0]),
        (observations, "evidence_observations", first_pages[1]),
        (findings, "findings", first_pages[2]),
    )
    for repository, section, first in page_sets:
        assert len(first.items) == 2
        assert first.has_more is True
        assert isinstance(first.next_cursor, str)
        second = await repository.page_for_investigation(
            investigation_id, cursor=first.next_cursor, limit=2
        )
        first_expected_id = 1_003 if section == "findings" else 3
        assert tuple(item.id for item in second.items) == tuple(
            UUID(int=first_expected_id + index) for index in (0, 1)
        )
        chained = decode_keyset_cursor(
            first.next_cursor, section=section, investigation_id=investigation_id
        )
        assert chained is not None
        assert verify_page_sha256(
            claimed_sha256=second.page_sha256,
            section=section,
            investigation_id=investigation_id,
            items=second.items,
            previous_page_sha256=chained["previous_page_sha256"],
            context_sha256=second.context_sha256,
            cursor=second.cursor,
            pivot=second.pivot,
            descending=second.descending,
            limit=second.limit,
            total=second.total,
            has_more=second.has_more,
            omission_manifest=second.omission_manifest,
            next_cursor=second.next_cursor,
        )
        assert not verify_page_sha256(
            claimed_sha256=second.page_sha256,
            section=section,
            investigation_id=investigation_id,
            items=(*second.items, "falsified"),
            previous_page_sha256=chained["previous_page_sha256"],
            context_sha256=second.context_sha256,
            cursor=second.cursor,
            pivot=second.pivot,
            descending=second.descending,
            limit=second.limit,
            total=second.total,
            has_more=second.has_more,
            omission_manifest=second.omission_manifest,
            next_cursor=second.next_cursor,
        )


def test_sql_keyset_predicate_is_a_pure_stable_tuple_seek_in_both_directions() -> None:
    rows = table(
        "evidence_observations",
        column("observed_at", DateTime(timezone=True)),
        column("id", Uuid(as_uuid=True)),
    )
    for descending, operator in ((False, ">"), (True, "<")):
        predicate = _keyset_predicate(
            rows.c.observed_at,
            rows.c.id,
            sort_key=datetime(2026, 1, 1, tzinfo=UTC),
            item_id=UUID(int=1),
            descending=descending,
        )
        compiled = str(
            select(rows.c.id)
            .where(predicate)
            .compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True})
        )
        assert f"observed_at {operator}" in compiled
        assert "observed_at =" in compiled
        assert f"id {operator}" in compiled
        assert " OR " in compiled


@pytest.mark.asyncio
async def test_all_sql_keyset_adapters_call_the_shared_predicate_and_request_limit_plus_one(
    monkeypatch,
) -> None:
    investigation_id = uuid4()
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    calls = []
    limits = []
    original_predicate = investigation_repositories._keyset_predicate

    def observed_predicate(*args, **kwargs):
        calls.append(kwargs["descending"])
        return original_predicate(*args, **kwargs)

    monkeypatch.setattr(investigation_repositories, "_keyset_predicate", observed_predicate)

    class SessionSpy:
        async def scalar(self, _statement):
            return 1

        async def scalars(self, statement):
            limits.append(statement._limit_clause.value)
            return ()

    for repository_type, section, descending in (
        (investigation_repositories.SqlAlchemyToolInvocationRepository, "tool_invocations", False),
        (
            investigation_repositories.SqlAlchemyEvidenceObservationRepository,
            "evidence_observations",
            True,
        ),
        (investigation_repositories.SqlAlchemyFindingRepository, "findings", False),
    ):
        cursor = encode_keyset_cursor(
            section=section,
            investigation_id=investigation_id,
            sort_key=timestamp.isoformat(),
            item_id=UUID(int=1),
            previous_page_sha256="a" * 64,
            descending=descending,
        )
        repository = repository_type(SessionSpy())
        await repository.page_for_investigation(
            investigation_id,
            cursor=cursor,
            limit=7,
            descending=descending,
        )

    assert calls == [False, True, False]
    assert limits == [8, 8, 8]


def test_omission_manifest_rejects_silent_or_false_bounded_omissions() -> None:
    with pytest.raises(ValidationError):
        ContextOmissionManifestEntryV1(
            section="evidence_index",
            total=3,
            selected=1,
            omitted=2,
            source_bounded=True,
            source_ref="investigation:test:evidence_index",
            page_sha256="a" * 64,
        )

    entry = ContextOmissionManifestEntryV1(
        section="sample_deltas",
        total=3,
        selected=1,
        omitted=2,
        source_bounded=False,
        source_ref="investigation:test:sample_deltas:materialized",
        page_sha256="b" * 64,
        omission_reasons={ContextOmissionReason.SOURCE_NOT_PAGINATED: 2},
        unbounded_reason="The aggregate source has no keyset port",
    )
    assert entry.selected + entry.omitted == entry.total


def test_keyset_cursor_rejects_oversized_or_invalid_pivots() -> None:
    investigation_id = uuid4()
    common = {
        "section": "evidence_index",
        "investigation_id": investigation_id,
        "item_id": UUID(int=1),
        "previous_page_sha256": "a" * 64,
    }
    with pytest.raises(ValueError):
        encode_keyset_cursor(**common, sort_key="x" * 257)
    with pytest.raises(ValueError):
        encode_keyset_cursor(**common, sort_key="not-a-sort-key")
    with pytest.raises(ValueError):
        decode_keyset_cursor(
            "a" * 1_025,
            section="evidence_index",
            investigation_id=investigation_id,
        )


def test_page_sha256_rejects_falsification_of_every_page_field() -> None:
    investigation_id = uuid4()
    timestamp = datetime(2026, 1, 1, tzinfo=UTC)
    items = (
        {"id": str(UUID(int=1)), "created_at": timestamp.isoformat(), "statement": "one"},
        {
            "id": str(UUID(int=2)),
            "created_at": (timestamp + timedelta(seconds=1)).isoformat(),
            "statement": "two",
        },
    )
    page = make_keyset_page(
        section="findings",
        investigation_id=investigation_id,
        items=items,
        total=2,
        limit=1,
        cursor=None,
        position=lambda item: (datetime.fromisoformat(item["created_at"]), UUID(item["id"])),
        descending=False,
        context_sha256="c" * 64,
    )
    previous_sha256 = "0" * 64
    baseline = {
        "claimed_sha256": page.page_sha256,
        "section": "findings",
        "investigation_id": investigation_id,
        "items": page.items,
        "previous_page_sha256": previous_sha256,
        "context_sha256": page.context_sha256,
        "cursor": page.cursor,
        "pivot": page.pivot,
        "descending": page.descending,
        "limit": page.limit,
        "total": page.total,
        "has_more": page.has_more,
        "omission_manifest": page.omission_manifest,
        "next_cursor": page.next_cursor,
    }
    assert verify_page_sha256(**baseline)

    omission = page.omission_manifest[0]
    changed_counts = omission.model_copy(
        update={
            "selected": 0,
            "omitted": 2,
            "omission_reasons": {ContextOmissionReason.PAGINATION: 2},
        }
    )
    changed_reason = omission.model_copy(
        update={"omission_reasons": {ContextOmissionReason.BUDGET: omission.omitted}}
    )
    changed_input_cursor = encode_keyset_cursor(
        section="findings",
        investigation_id=investigation_id,
        sort_key=timestamp.isoformat(),
        item_id=UUID(int=88),
        previous_page_sha256=previous_sha256,
        descending=False,
        context_sha256="c" * 64,
    )
    output_payload = decode_keyset_cursor(
        page.next_cursor, section="findings", investigation_id=investigation_id
    )
    assert output_payload is not None
    changed_output_pivot = encode_keyset_cursor(
        section="findings",
        investigation_id=investigation_id,
        sort_key=output_payload["sort_key"],
        item_id=UUID(int=99),
        previous_page_sha256=page.page_sha256,
        descending=False,
        context_sha256="c" * 64,
    )
    falsifications = (
        {"pivot": {**page.pivot, "item_id": str(UUID(int=77))}},
        {"investigation_id": uuid4()},
        {"previous_page_sha256": "f" * 64},
        {"limit": page.limit + 1},
        {"total": page.total + 1},
        {"has_more": not page.has_more},
        {"omission_manifest": (changed_counts,)},
        {"omission_manifest": (changed_reason,)},
        {"items": (*page.items, {"id": "falsified"})},
        {"cursor": changed_input_cursor},
        {"context_sha256": "d" * 64},
        {"descending": not page.descending},
        {"section": "evidence_index"},
        {"next_cursor": changed_output_pivot},
    )
    for changed in falsifications:
        assert not verify_page_sha256(**(baseline | changed)), changed


def test_disclosure_controls_refuse_external_context_submission() -> None:
    snapshot = ArticleSnapshot(
        subject_id=uuid4(),
        production_run_id=uuid4(),
        synthesis_artifact_id=uuid4(),
        synthesis_sha256="a" * 64,
        article=ArticleContextV1(
            short_summary="Synthetic",
            claims=(),
            documented_samples=(),
            known_chain=(),
            open_questions=(),
        ),
        tlp="green",
        do_not_submit=False,
        external_llm_allowed=True,
    )
    envelope = InvestigationEnvelope(
        investigation_id=uuid4(),
        allowed_providers=(),
        budget=InvestigationBudget(),
        acquire_hits_allowed=False,
        disclosure=DisclosureBounds(
            tlp=TLPLevel.GREEN,
            do_not_submit=False,
            external_llm_allowed=True,
        ),
        approval_decision_id=uuid4(),
        approval_actor_id="test-analyst",
    )
    assert InvestigationService.external_llm_allowed(snapshot, envelope) is True
    assert (
        InvestigationService.external_llm_allowed(replace(snapshot, tlp="red"), envelope) is False
    )
    assert (
        InvestigationService.external_llm_allowed(replace(snapshot, do_not_submit=True), envelope)
        is False
    )
    assert (
        InvestigationService.external_llm_allowed(
            replace(snapshot, external_llm_allowed=False), envelope
        )
        is False
    )
    assert (
        InvestigationService.external_llm_allowed(
            snapshot,
            replace(
                envelope,
                disclosure=DisclosureBounds(
                    tlp=TLPLevel.GREEN,
                    do_not_submit=True,
                    external_llm_allowed=True,
                ),
            ),
        )
        is False
    )
