from datetime import UTC, date, datetime
from uuid import UUID, uuid4

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryCandidate,
    DiscoveryCandidateEvidence,
    DiscoveryIocType,
    DiscoveryRequestSnapshot,
    DiscoveryRun,
    DiscoveryRunInputMode,
    IncompleteSourceCandidate,
    IocPresence,
    PeriodRelation,
    ProvisionalDiscoveryIoc,
    ProvisionalIocPublicationRelation,
    SourceCandidate,
    SourceRole,
    SourceVerificationStatus,
)
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

from .edition_codes import reserve_edition_code

pytestmark = pytest.mark.integration


async def test_discovery_run_repository_round_trip_idempotency_and_order(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    edition = Edition(
        country="France",
        country_code="FR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
    )
    snapshot = DiscoveryRequestSnapshot(
        country=edition.country,
        country_code=edition.country_code,
        country_aliases=("French Republic",),
        period_start=edition.period_start,
        period_end=edition.period_end,
        as_of_date=date(2026, 9, 1),
        languages=edition.languages,
        source_profile="default-v1",
        keywords=("cyber", "intrusion"),
        exclusions=("sport",),
        complementary_axis="initial",
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    run_times = (
        datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        datetime(2026, 9, 1, 11, 0, tzinfo=UTC),
        datetime(2026, 9, 1, 12, 0, tzinfo=UTC),
    )
    runs = (
        DiscoveryRun(
            edition_id=edition.id,
            input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
            source_profile="default-v1",
            complementary_axis="initial",
            request_snapshot=snapshot,
            idempotency_key="run-old",
            created_by="analyst-1",
            created_at=run_times[0],
        ),
        DiscoveryRun(
            edition_id=edition.id,
            input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
            source_profile="default-v1",
            complementary_axis="initial",
            request_snapshot=snapshot,
            idempotency_key="run-same-snapshot",
            created_by="analyst-1",
            created_at=run_times[1],
        ),
        DiscoveryRun(
            edition_id=edition.id,
            input_mode=DiscoveryRunInputMode.MANUAL_IMPORT,
            source_profile="default-v1",
            complementary_axis="initial",
            request_snapshot=snapshot,
            idempotency_key="run-old",
            created_by="analyst-2",
            created_at=run_times[2],
        ),
        DiscoveryRun(
            edition_id=edition.id,
            input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
            source_profile="default-v1",
            complementary_axis="initial",
            request_snapshot=snapshot,
            idempotency_key="run-latest",
            created_by="analyst-1",
            created_at=run_times[3],
        ),
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            for run in runs:
                assert await uow.discovery_runs.add_if_absent(run)
            assert not await uow.discovery_runs.add_if_absent(runs[0])
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.discovery_runs.get(runs[1].id)
            assert persisted == runs[1]
            assert (
                await uow.discovery_runs.get_by_idempotency_key(
                    edition.id, DiscoveryRunInputMode.BRIDGE_RESEARCH, "run-old"
                )
                == runs[0]
            )
            assert (
                await uow.discovery_runs.get_by_idempotency_key(
                    edition.id, DiscoveryRunInputMode.MANUAL_IMPORT, "run-old"
                )
                == runs[2]
            )
            assert [run.id for run in await uow.discovery_runs.list_for_edition(edition.id)] == [
                run.id for run in reversed(runs)
            ]
    finally:
        await engine.dispose()


async def test_discovery_batch_round_trip_and_source_status(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    edition = Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
    )
    research_run = _run("research", "a")
    discovery_run = DiscoveryRun(
        edition_id=edition.id,
        input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
        source_profile="default-v1",
        complementary_axis="initial",
        request_snapshot=DiscoveryRequestSnapshot(
            country=edition.country,
            country_code=edition.country_code,
            country_aliases=(edition.country, edition.country_code),
            period_start=edition.period_start,
            period_end=edition.period_end,
            as_of_date=date(2026, 9, 1),
            languages=edition.languages,
            source_profile="default-v1",
            keywords=(),
            exclusions=(),
            complementary_axis="initial",
            tlp=edition.tlp,
            sensitivity="internal",
            external_llm_allowed=True,
        ),
        idempotency_key="batch-round-trip",
        created_by="dev-analyst",
    )
    source = SourceCandidate(
        url="https://vendor.example/report?utm_source=test",
        title="Original report",
        publisher="Vendor",
        role=SourceRole.PRIMARY,
        published_at=date(2026, 7, 10),
        event_date=date(2026, 7, 2),
        citation="Citation conservée",
        local_ref="P1",
        raw_url="https://vendor.example/report?utm_source=test",
        period_relation=PeriodRelation.IN_PERIOD,
        ioc_presence=IocPresence.DECLARED,
        ioc_declared_count=12,
        parsing_warnings=("Compte non vérifié",),
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    candidate = CandidateTopic(
        title="Iran-linked campaign",
        summary="Technical report with indicators.",
        novelty="New configuration.",
        technical_potential=4,
        event_date=date(2026, 7, 2),
        uncertainties=("Attribution non vérifiée",),
        relevance_reasons=("Original technical report",),
        actors=("Example actor",),
        campaigns=(),
        malware=("ExampleRAT",),
        cves=(),
        victims=(),
        sectors=("government",),
        countries=("Iran",),
        likely_artifacts=("ioc", "configurations"),
        sources=[source],
        provisional_iocs=[
            ProvisionalDiscoveryIoc(
                raw_value="192.0.2.1",
                normalized_value="192.0.2.1",
                declared_type="ipv4",
                proposed_type=DiscoveryIocType.IPV4,
                publication_relations=(
                    ProvisionalIocPublicationRelation(
                        publication_id=source.id,
                        publication_ref="P1",
                        raw_value="192.0.2.1",
                        markdown_block="visible-iocs: 192.0.2.1",
                    ),
                ),
                model_run_id=research_run.id,
                markdown_block="visible-iocs: 192.0.2.1",
            )
        ],
        incomplete_sources=[
            IncompleteSourceCandidate(
                title="Source sans URL",
                raw_url="ftp://invalid.example/report",
                local_ref="P2",
                parsing_warnings=("no_explicit_url",),
            )
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        local_ref="S1",
        actor_or_campaign="Example actor",
        technical_potential_reason="Configurations annoncées.",
        parsing_warnings=("Métadonnées provisoires",),
    )
    batch = DiscoveryBatch(
        edition_id=edition.id,
        discovery_run_id=discovery_run.id,
        request_hash="c" * 64,
        complementary_axis="initial",
        queries=("Iran APT July 2026",),
        citations=(
            {
                "label": "Original report",
                "url": "https://vendor.example/report",
                "excerpt": "Technical details",
            },
        ),
        candidates=[candidate],
        discovery_model_run_id=research_run.id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        report_sha256="d" * 64,
        parser_version="chatgpt-markdown-v1",
        parsing_status="report_parsing_partial",
        parsing_warnings=("Métadonnées provisoires",),
        unattached_visible_citations=(
            {
                "label": "Citation orpheline",
                "url": "https://relay.example/context",
                "canonical_url": "https://relay.example/context",
                "excerpt": None,
            },
        ),
        parsing_revision=2,
        supersedes_batch_id=uuid4(),
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            assert await uow.discovery_runs.add_if_absent(discovery_run)
            await uow.model_runs.add(research_run)
            assert await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.discovery_batches.get(batch.id)
            assert persisted is not None
            assert persisted.discovery_run_id == discovery_run.id
            assert [
                item.id for item in await uow.discovery_batches.list_for_run(discovery_run.id)
            ] == [batch.id]
            persisted_source = persisted.candidates[0].sources[0]
            assert persisted_source.canonical_url == "https://vendor.example/report"
            assert persisted_source.verification_status is SourceVerificationStatus.UNVERIFIED
            assert persisted_source.source_ref.startswith("source-")
            assert persisted_source.ioc_declared_count == 12
            assert persisted.candidates[0].incomplete_sources[0].raw_url == (
                "ftp://invalid.example/report"
            )
            assert persisted.report_sha256 == "d" * 64
            assert persisted.parsing_revision == 2
            assert persisted.supersedes_batch_id == batch.supersedes_batch_id
            assert persisted.unattached_visible_citations[0]["label"] == "Citation orpheline"
            persisted_ioc = persisted.candidates[0].provisional_iocs[0]
            assert persisted_ioc.status.value == "provisional_visible"
            assert persisted_ioc.model_run_id == research_run.id
            assert persisted_ioc.publication_relations[0].publication_id == source.id
            persisted_candidate = (await uow.discovery_candidates.list_for_batch(batch.id))[0]
            persisted_candidate.evidence.sources[0].mark(
                SourceVerificationStatus.INVALID, actor_id="dev-analyst"
            )
            await uow.discovery_candidates.save_evidence(persisted_candidate)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            reread = await uow.discovery_batches.get(batch.id)
        assert reread is not None
        assert (
            reread.candidates[0].sources[0].verification_status is SourceVerificationStatus.INVALID
        )
        assert reread.citations[0]["label"] == "Original report"

    finally:
        await engine.dispose()


def test_discovery_candidate_domain_projection_and_invariants() -> None:
    run_id = uuid4()
    batch_id = uuid4()
    source = SourceCandidate(
        url="https://vendor.example/candidate",
        title="Candidate source",
        publisher="Vendor",
        role=SourceRole.PRIMARY,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    topic = CandidateTopic(
        id=uuid4(),
        title=" Topic ",
        summary=" Summary ",
        novelty=" Novelty ",
        technical_potential=3,
        uncertainties=("uncertain",),
        relevance_reasons=("relevant",),
        actors=("actor",),
        campaigns=("campaign",),
        malware=("malware",),
        cves=("CVE-2026-0001",),
        victims=("victim",),
        sectors=("sector",),
        countries=("country",),
        likely_artifacts=("artifact",),
        iocs=("192.0.2.1",),
        sources=[source],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        local_ref="C1",
        actor_or_campaign="actor",
        technical_potential_reason="Reason",
        parsing_warnings=("warning",),
        markdown_block="candidate markdown",
        context_only=True,
    )
    candidate = DiscoveryCandidate.from_candidate_topic(
        topic,
        discovery_run_id=run_id,
        discovery_batch_id=batch_id,
        position=4,
    )

    projected = candidate.to_candidate_topic()
    assert projected.id == topic.id
    assert projected.title == "Topic"
    assert projected.uncertainties == topic.uncertainties
    assert projected.relevance_reasons == topic.relevance_reasons
    assert projected.iocs == topic.iocs
    assert projected.sources[0].url == source.url
    assert projected.context_only is True

    with pytest.raises(ValueError, match="title, summary and novelty"):
        DiscoveryCandidate(
            discovery_run_id=run_id,
            discovery_batch_id=batch_id,
            position=0,
            title=" ",
            summary="summary",
            novelty="novelty",
            technical_potential=0,
            technical_potential_reason="reason",
            event_date=None,
            actor_or_campaign="unknown",
            context_only=False,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        )
    with pytest.raises(ValueError, match="between 0 and 4"):
        DiscoveryCandidate(
            discovery_run_id=run_id,
            discovery_batch_id=batch_id,
            position=0,
            title="title",
            summary="summary",
            novelty="novelty",
            technical_potential=5,
            technical_potential_reason="reason",
            event_date=None,
            actor_or_campaign="unknown",
            context_only=False,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        )
    with pytest.raises(ValueError, match="cannot be negative"):
        DiscoveryCandidate(
            discovery_run_id=run_id,
            discovery_batch_id=batch_id,
            position=-1,
            title="title",
            summary="summary",
            novelty="novelty",
            technical_potential=0,
            technical_potential_reason="reason",
            event_date=None,
            actor_or_campaign="unknown",
            context_only=False,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        )


async def test_discovery_candidate_repository_round_trip_and_provenance(
    migrated_postgres_url: str,
) -> None:
    from sqlalchemy import delete
    from sqlalchemy.exc import IntegrityError

    from cti_app.infrastructure.database.models.discovery import (
        DiscoveryBatchRow,
        DiscoveryCandidateRow,
        DiscoveryRunRow,
    )

    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    # Editions are unique per (country_code, period) in the shared test database;
    # reserve a code no other scenario of this process can allocate.
    edition = Edition(
        country="Candidate repository",
        country_code=reserve_edition_code(),
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr",),
    )
    discovery_run = DiscoveryRun(
        edition_id=edition.id,
        input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
        source_profile="default-v1",
        complementary_axis="initial",
        request_snapshot=DiscoveryRequestSnapshot(
            country=edition.country,
            country_code=edition.country_code,
            country_aliases=(edition.country,),
            period_start=date(2026, 8, 1),
            period_end=date(2026, 8, 31),
            as_of_date=date(2026, 9, 1),
            languages=("fr",),
            source_profile="default-v1",
            keywords=(),
            exclusions=(),
            complementary_axis="initial",
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        ),
        idempotency_key="candidate-repository",
        created_by="dev-analyst",
    )
    research_run = _run("research", "a")
    old_batch = DiscoveryBatch(
        id=uuid4(),
        edition_id=edition.id,
        discovery_run_id=discovery_run.id,
        request_hash="a" * 64,
        complementary_axis="initial",
        queries=(),
        citations=(),
        discovery_model_run_id=research_run.id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="v1",
        created_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, 9, 0, tzinfo=UTC),
    )
    new_batch = DiscoveryBatch(
        id=uuid4(),
        edition_id=edition.id,
        discovery_run_id=discovery_run.id,
        request_hash="b" * 64,
        complementary_axis="initial",
        queries=(),
        citations=(),
        discovery_model_run_id=research_run.id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="v1",
        supersedes_batch_id=old_batch.id,
        created_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
        updated_at=datetime(2026, 9, 1, 10, 0, tzinfo=UTC),
    )
    old_batch.replaced_by_batch_id = new_batch.id
    source = SourceCandidate(
        url="https://vendor.example/candidate",
        title="Candidate source",
        publisher="Vendor",
        role=SourceRole.PRIMARY,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    evidence = DiscoveryCandidateEvidence(
        uncertainties=("uncertain",),
        relevance_reasons=("relevant",),
        actors=("actor",),
        campaigns=("campaign",),
        malware=("malware",),
        cves=("CVE-2026-0001",),
        victims=("victim",),
        sectors=("sector",),
        countries=("France",),
        likely_artifacts=("artifact",),
        iocs=("192.0.2.1",),
        sources=[source],
        parsing_warnings=("warning",),
        markdown_block="candidate markdown",
    )

    def make_candidate(batch_id: UUID, position: int) -> DiscoveryCandidate:
        return DiscoveryCandidate(
            discovery_run_id=discovery_run.id,
            discovery_batch_id=batch_id,
            position=position,
            local_ref=f"C{position}",
            title="Candidate title",
            summary="Candidate summary",
            novelty="Candidate novelty",
            technical_potential=4,
            technical_potential_reason="Technical reason",
            event_date=date(2026, 8, 20),
            actor_or_campaign="actor",
            context_only=True,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
            evidence=evidence,
        )

    old_candidate = make_candidate(old_batch.id, 0)
    new_candidate_late = make_candidate(new_batch.id, 2)
    new_candidate_early = make_candidate(new_batch.id, 0)
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            assert await uow.discovery_runs.add_if_absent(discovery_run)
            await uow.model_runs.add(research_run)
            assert await uow.discovery_batches.add_if_absent(old_batch)
            assert await uow.discovery_batches.add_if_absent(new_batch)
            await uow.discovery_batches.save(old_batch)
            await uow.discovery_candidates.add_many(
                [new_candidate_late, old_candidate, new_candidate_early]
            )
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.discovery_candidates.get(new_candidate_early.id)
            assert persisted is not None
            assert persisted.id == new_candidate_early.id
            assert persisted.context_only is True
            assert persisted.evidence.uncertainties == evidence.uncertainties
            assert persisted.evidence.sources[0].url == source.url
            assert [
                item.position
                for item in await uow.discovery_candidates.list_for_batch(new_batch.id)
            ] == [0, 2]
            assert [
                item.position
                for item in await uow.discovery_candidates.list_for_run(discovery_run.id)
            ] == [0, 0, 2]
            assert [
                item.id for item in await uow.discovery_candidates.list_for_edition(edition.id)
            ] == [new_candidate_early.id, new_candidate_late.id]
            assert {
                item.id
                for item in await uow.discovery_candidates.list_for_edition(
                    edition.id, include_replaced=True
                )
            } == {old_candidate.id, new_candidate_early.id, new_candidate_late.id}

            persisted.evidence.sources[0].mark(
                SourceVerificationStatus.INVALID, actor_id="dev-analyst"
            )
            await uow.discovery_candidates.save_evidence(persisted)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            reread = await uow.discovery_candidates.get(new_candidate_early.id)
            assert reread is not None
            assert (
                reread.evidence.sources[0].verification_status is SourceVerificationStatus.INVALID
            )
            duplicate = make_candidate(new_batch.id, 0)
            with pytest.raises(IntegrityError):
                await uow.discovery_candidates.add_many([duplicate])
            await uow.rollback()

        async with engine.connect() as connection:
            with pytest.raises(IntegrityError):
                await connection.execute(
                    delete(DiscoveryBatchRow).where(DiscoveryBatchRow.id == new_batch.id)
                )
            await connection.rollback()
        # discovery_runs is append-only (its trigger fires before any FK check),
        # so the run-side RESTRICT is asserted on the mapped foreign key.
        run_foreign_keys = {
            foreign_key.target_fullname.split(".")[0]: foreign_key.ondelete
            for foreign_key in DiscoveryCandidateRow.metadata.tables[
                DiscoveryCandidateRow.__tablename__
            ].foreign_keys
        }
        assert run_foreign_keys == {
            DiscoveryRunRow.__tablename__: "RESTRICT",
            DiscoveryBatchRow.__tablename__: "RESTRICT",
        }
    finally:
        await engine.dispose()


async def test_discovery_batch_candidates_round_trip_without_payload_copy(
    migrated_postgres_url: str,
) -> None:
    from cti_app.infrastructure.database.models.discovery import DiscoveryBatchRow

    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    edition = Edition(
        country="Iraq",
        country_code="IQ",
        period_start=date(2026, 9, 1),
        period_end=date(2026, 9, 30),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
    )
    research_run = _run("research", "a")
    discovery_run = DiscoveryRun(
        edition_id=edition.id,
        input_mode=DiscoveryRunInputMode.BRIDGE_RESEARCH,
        source_profile="default-v1",
        complementary_axis="initial",
        request_snapshot=DiscoveryRequestSnapshot(
            country=edition.country,
            country_code=edition.country_code,
            country_aliases=(edition.country, edition.country_code),
            period_start=edition.period_start,
            period_end=edition.period_end,
            as_of_date=date(2026, 9, 1),
            languages=edition.languages,
            source_profile="default-v1",
            keywords=(),
            exclusions=(),
            complementary_axis="initial",
            tlp=edition.tlp,
            sensitivity="internal",
            external_llm_allowed=True,
        ),
        idempotency_key="contributions-meta",
        created_by="dev-analyst",
    )
    source = SourceCandidate(
        url="https://vendor.example/report",
        title="Report",
        publisher="Vendor",
        role=SourceRole.PRIMARY,
        published_at=date(2026, 7, 10),
        citation="Citation",
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    candidate = CandidateTopic(
        title="Candidate",
        summary="Summary.",
        novelty="Novel.",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=(),
        actors=(),
        campaigns=(),
        malware=(),
        cves=(),
        victims=(),
        sectors=(),
        countries=(),
        likely_artifacts=(),
        sources=[source],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    batch = DiscoveryBatch(
        edition_id=edition.id,
        discovery_run_id=discovery_run.id,
        request_hash="d" * 64,
        complementary_axis="initial",
        queries=("Query",),
        citations=(),
        candidates=[candidate],
        discovery_model_run_id=research_run.id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="v1",
        parsing_status="completed",
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            assert await uow.discovery_runs.add_if_absent(discovery_run)
            await uow.model_runs.add(research_run)
            assert await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.discovery_batches.get(batch.id)
            assert persisted is not None
            assert [item.id for item in persisted.candidates] == [candidate.id]
            row = await uow._session.get(DiscoveryBatchRow, batch.id)
            assert row is not None
            assert "candidates" not in row.payload
            assert "contributions_meta" not in row.payload
            canonical = await uow.discovery_candidates.list_for_batch(batch.id)
            assert [(item.id, item.discovery_run_id, item.position) for item in canonical] == [
                (candidate.id, discovery_run.id, 0)
            ]

        # A technical retry of the same deterministic batch never duplicates candidates.
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert not await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert len(await uow.discovery_candidates.list_for_run(discovery_run.id)) == 1

        # Batch and candidates share one transaction: a failure before commit
        # leaves neither the batch nor its candidates durable.
        rolled_back_candidate = CandidateTopic(
            title="Rolled back",
            summary="Summary.",
            novelty="Novel.",
            technical_potential=1,
            uncertainties=(),
            relevance_reasons=(),
            actors=(),
            campaigns=(),
            malware=(),
            cves=(),
            victims=(),
            sectors=(),
            countries=(),
            likely_artifacts=(),
            sources=[],
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
        )
        rolled_back_batch = DiscoveryBatch(
            edition_id=edition.id,
            discovery_run_id=discovery_run.id,
            request_hash="e" * 64,
            complementary_axis="initial",
            queries=(),
            citations=(),
            candidates=[rolled_back_candidate],
            discovery_model_run_id=research_run.id,
            tlp=TLP.AMBER,
            sensitivity="internal",
            external_llm_allowed=True,
            parser_version="v1",
        )
        with pytest.raises(RuntimeError, match="before commit"):
            async with SqlAlchemyUnitOfWork(session_factory) as uow:
                assert await uow.discovery_batches.add_if_absent(rolled_back_batch)
                raise RuntimeError("failure before commit")
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.discovery_batches.get(rolled_back_batch.id) is None
            assert await uow.discovery_candidates.get(rolled_back_candidate.id) is None
            assert await uow.discovery_candidates.list_for_batch(rolled_back_batch.id) == []
    finally:
        await engine.dispose()


async def test_discovery_batch_missing_parser_version_fails(
    migrated_postgres_url: str,
) -> None:
    from cti_app.infrastructure.database.models.discovery import DiscoveryBatchRow
    from cti_app.infrastructure.database.repositories.discovery import _discovery_batch_from_row

    row = DiscoveryBatchRow(
        id=uuid4(),
        edition_id=uuid4(),
        request_hash="test",
        complementary_axis="initial",
        status="completed",
        discovery_model_run_id=uuid4(),
        tlp="AMBER",
        sensitivity="internal",
        external_llm_allowed=True,
        payload={
            "queries": [],
            "citations": [],
            # Missing: parser_version
            "parsing_status": "completed",
            "parsing_warnings": [],
            "unattached_visible_citations": [],
            "parsing_revision": 1,
            "supersedes_batch_id": None,
            "replaced_by_batch_id": None,
            "source_mode": "visible_citations_only",
            "bridge_capabilities": {},
            "citation_count": 0,
            "source_coverage_complete": False,
            "source_coverage_incomplete_reason": None,
            "report_sha256": None,
        },
        created_at=datetime.now(UTC),
        updated_at=datetime.now(UTC),
    )

    with pytest.raises(KeyError, match="parser_version"):
        _discovery_batch_from_row(row, ())


def _run(template: str, hash_prefix: str) -> ModelRun:
    return ModelRun(
        provider=ModelProvider.FAKE,
        model_role=(
            ModelRole.RESEARCH if template == "research" else ModelRole.STRUCTURED_EXTRACTION
        ),
        requested_model="fake-deterministic-v1",
        prompt_template_id=template,
        prompt_template_version="1",
        authorized_input_hash=hash_prefix * 64,
        evidence_pack_hash="e" * 64,
        parameters={},
        started_at=datetime.now(UTC),
    )
