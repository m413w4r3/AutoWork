from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import DateTime, Uuid, column, select, table
from sqlalchemy.dialects import postgresql

from cti_app.application.context_pagination import (
    ContextOmissionManifestEntryV1,
    ContextOmissionReason,
    decode_keyset_cursor,
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
        await invocations.page_for_investigation(
            investigation_id, cursor=None, limit=2
        ),
        await observations.page_for_investigation(
            investigation_id, cursor=None, limit=2
        ),
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
        )
        assert not verify_page_sha256(
            claimed_sha256=second.page_sha256,
            section=section,
            investigation_id=investigation_id,
            items=(*second.items, "falsified"),
            previous_page_sha256=chained["previous_page_sha256"],
        )


def test_sql_keyset_predicate_is_a_pure_stable_tuple_seek() -> None:
    rows = table(
        "evidence_observations",
        column("observed_at", DateTime(timezone=True)),
        column("id", Uuid(as_uuid=True)),
    )
    predicate = _keyset_predicate(
        rows.c.observed_at,
        rows.c.id,
        sort_key=datetime(2026, 1, 1, tzinfo=UTC),
        item_id=UUID(int=1),
        descending=False,
    )
    compiled = str(
        select(rows.c.id).where(predicate).compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        )
    )
    assert "observed_at >" in compiled
    assert "observed_at =" in compiled
    assert "id >" in compiled
    assert " OR " in compiled


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
        InvestigationService.external_llm_allowed(replace(snapshot, tlp="red"), envelope)
        is False
    )
    assert InvestigationService.external_llm_allowed(
        replace(snapshot, do_not_submit=True), envelope
    ) is False
    assert InvestigationService.external_llm_allowed(
        replace(snapshot, external_llm_allowed=False), envelope
    ) is False
    assert InvestigationService.external_llm_allowed(
        snapshot,
        replace(
            envelope,
            disclosure=DisclosureBounds(
                tlp=TLPLevel.GREEN,
                do_not_submit=True,
                external_llm_allowed=True,
            ),
        ),
    ) is False
