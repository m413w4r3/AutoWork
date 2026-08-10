from datetime import UTC, date, datetime

import pytest

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
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
    structuring_run = _run("structured", "b")
    source = SourceCandidate(
        url="https://vendor.example/report?utm_source=test",
        title="Original report",
        publisher="Vendor",
        role=SourceRole.PRIMARY,
        published_at=date(2026, 7, 10),
        event_date=date(2026, 7, 2),
        citation="Citation conservée",
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
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
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
        structuring_model_run_id=structuring_run.id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )
    try:
        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.model_runs.add(research_run)
            await uow.model_runs.add(structuring_run)
            assert await uow.discovery_batches.add_if_absent(batch)
            await uow.commit()

        async with SqlAlchemyUnitOfWork(session_factory) as uow:
            persisted = await uow.discovery_batches.get_by_request_hash(edition.id, "c" * 64)
            assert persisted is not None
            persisted_source = persisted.candidates[0].sources[0]
            assert persisted_source.canonical_url == "https://vendor.example/report"
            assert persisted_source.verification_status is SourceVerificationStatus.UNVERIFIED
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
    finally:
        await engine.dispose()


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
