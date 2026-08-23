from datetime import UTC, date, datetime
from uuid import uuid4

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryIocType,
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

pytestmark = pytest.mark.integration


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
        target_major_articles=2,
        target_briefs=6,
        source_profile="iran-default",
    )
    research_run = _run("research", "a")
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
            await uow.model_runs.add(research_run)
            assert await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.discovery_batches.get_by_request_hash(edition.id, "c" * 64)
            assert persisted is not None
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
            persisted_source.mark(SourceVerificationStatus.INVALID, actor_id="dev-analyst")
            await uow.discovery_batches.save(persisted)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            reread = await uow.discovery_batches.get(batch.id)
        assert reread is not None
        assert (
            reread.candidates[0].sources[0].verification_status is SourceVerificationStatus.INVALID
        )
        assert reread.citations[0]["label"] == "Original report"

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.delete(edition.id, edition.version)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.get(edition.id) is None
            assert await uow.discovery_batches.get(batch.id) is None
            assert await uow.model_runs.get(research_run.id) is None
    finally:
        await engine.dispose()


async def test_discovery_batch_contributions_metadata_preserved(
    migrated_postgres_url: str,
) -> None:
    """Verify contributions metadata is preserved in round-trip."""
    from cti_app.domain.discovery import ContributionStatus, DiscoveryContribution

    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)
    edition = Edition(
        country="Iran",
        country_code="IR",
        period_start=date(2026, 8, 1),
        period_end=date(2026, 8, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en", "fa"),
        target_major_articles=2,
        target_briefs=6,
        source_profile="iran-default",
    )
    research_run = _run("research", "a")
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
    now = datetime.now(UTC)
    batch = DiscoveryBatch(
        edition_id=edition.id,
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
        contributions=[
            DiscoveryContribution(
                candidate=candidate,
                status=ContributionStatus.ACCEPTED,
                created_at=now,
                accepted_at=now,
                human_note="Manual review: valid candidate",
            )
        ],
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.model_runs.add(research_run)
            assert await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.discovery_batches.get_by_request_hash(edition.id, "d" * 64)
            assert persisted is not None
            assert len(persisted.contributions) == 1
            contrib = persisted.contributions[0]
            assert contrib.status == ContributionStatus.ACCEPTED
            assert contrib.created_at == now
            assert contrib.accepted_at == now
            assert contrib.human_note == "Manual review: valid candidate"
            assert contrib.candidate.id == candidate.id
    finally:
        await engine.dispose()


async def test_discovery_batch_missing_contributions_meta_fails(
    migrated_postgres_url: str,
) -> None:
    """Missing contributions_meta must cause KeyError."""
    from cti_app.infrastructure.database.models import DiscoveryBatchRow
    from cti_app.infrastructure.database.repositories.discovery import _discovery_batch_from_row

    # Manually construct a row with missing contributions_meta
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
            "candidates": [],
            # Missing: contributions_meta
            "queries": [],
            "citations": [],
            "parser_version": "v1",
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

    with pytest.raises(KeyError, match="contributions_meta"):
        _discovery_batch_from_row(row)


async def test_discovery_batch_missing_parser_version_fails(
    migrated_postgres_url: str,
) -> None:
    """Missing parser_version must cause KeyError."""
    from cti_app.infrastructure.database.models import DiscoveryBatchRow
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
            "candidates": [],
            "contributions_meta": [],
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
        _discovery_batch_from_row(row)


async def test_discovery_batch_candidate_without_contribution_metadata_fails(
    migrated_postgres_url: str,
) -> None:
    """Candidate without matching contribution metadata must cause KeyError."""
    from uuid import uuid4 as make_uuid

    from cti_app.infrastructure.database.models import DiscoveryBatchRow
    from cti_app.infrastructure.database.repositories.discovery import _discovery_batch_from_row

    candidate_id = make_uuid()
    row = DiscoveryBatchRow(
        id=make_uuid(),
        edition_id=make_uuid(),
        request_hash="test",
        complementary_axis="initial",
        status="completed",
        discovery_model_run_id=make_uuid(),
        tlp="AMBER",
        sensitivity="internal",
        external_llm_allowed=True,
        payload={
            "candidates": [
                {
                    "id": str(candidate_id),
                    "title": "Test",
                    "summary": "Summary.",
                    "novelty": "Novel.",
                    "technical_potential": 1,
                    "tlp": "AMBER",
                    "sensitivity": "internal",
                    "external_llm_allowed": True,
                    "sources": [],
                    "incomplete_sources": [],
                    "provisional_iocs": [],
                    "likely_artifacts": [],
                    "uncertainties": [],
                    "relevance_reasons": [],
                    "actors": [],
                    "campaigns": [],
                    "malware": [],
                    "cves": [],
                    "victims": [],
                    "sectors": [],
                    "countries": [],
                    "iocs": [],
                    "parsing_warnings": [],
                }
            ],
            "contributions_meta": [
                # Missing entry for candidate_id
            ],
            "queries": [],
            "citations": [],
            "parser_version": "v1",
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

    with pytest.raises(KeyError):
        _discovery_batch_from_row(row)


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
