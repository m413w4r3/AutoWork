from datetime import UTC, date, datetime
from uuid import UUID

import pytest

from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    ContributionStatus,
    DiscoveryBatch,
    DiscoveryContribution,
    DiscoverySourceMode,
    SourceCandidate,
    SourceRole,
)
from cti_app.domain.discovery_cumulative import DiscoveryInputMode, SubjectMergeEvent
from cti_app.domain.editions import Edition
from cti_app.domain.model_runs import ModelProvider, ModelRole, ModelRun
from cti_app.infrastructure.database.session import create_postgres_engine, create_session_factory
from cti_app.infrastructure.database.uow import SqlAlchemyUnitOfWork

pytestmark = pytest.mark.integration


async def test_cumulative_snapshot_identity_and_contribution_round_trip(
    migrated_postgres_url: str,
) -> None:
    engine = create_postgres_engine(migrated_postgres_url)
    session_factory = create_session_factory(engine)

    def uow_factory() -> SqlAlchemyUnitOfWork:
        return SqlAlchemyUnitOfWork(session_factory)

    edition = Edition(
        country="Cumulative Iran",
        country_code="CI",
        period_start=date(2026, 7, 1),
        period_end=date(2026, 7, 31),
        tlp=TLP.AMBER,
        languages=("fr", "en"),
        target_articles=8,
        source_profile="iran-default",
    )
    run = ModelRun(
        provider=ModelProvider.FAKE,
        model_role=ModelRole.RESEARCH,
        requested_model="fake",
        prompt_template_id="discovery",
        prompt_template_version="1",
        authorized_input_hash="a" * 64,
        evidence_pack_hash="b" * 64,
        parameters={},
    )
    batch = _batch(edition.id, run.id)
    second_batch = _batch(
        edition.id,
        run.id,
        title="Distinct title",
        url="https://vendor.example/distinct",
        request_hash="e" * 64,
    )
    second_batch.candidates[0].campaigns = ("Distinct Campaign",)
    second_batch.candidates[0].malware = ("Distinct Malware",)
    try:
        async with uow_factory() as uow:
            assert await uow.editions.add_if_absent(edition)
            await uow.model_runs.add(run)
            assert await uow.discovery_batches.add_if_absent(batch)
            assert await uow.discovery_batches.add_if_absent(second_batch)
            await uow.commit()

        service = CumulativeDiscoveryService(uow_factory)
        intake, snapshot = await service.reconcile_batch(
            batch,
            input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
            actor_id="integration-test",
        )
        _, retried_snapshot = await service.reconcile_batch(
            batch,
            input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
            actor_id="integration-test",
        )

        async with uow_factory() as uow:
            persisted = await uow.discovery_snapshots.get_active(edition.id)
            identities = await uow.discovery_subject_identities.list_for_edition(edition.id)
            contributions = await uow.subject_contributions.list_for_subject(
                snapshot.subjects[0].subject_id
            )

        assert persisted is not None
        assert retried_snapshot.id == snapshot.id
        assert persisted.snapshot_hash == snapshot.snapshot_hash
        assert persisted.subjects[0].canonical_title == "Stable title"
        assert identities[0].id == persisted.subjects[0].subject_id
        assert contributions[0].intake_id == intake.id
        assert contributions[0].subject_id == identities[0].id

        _, second_snapshot = await service.reconcile_batch(
            second_batch,
            input_mode=DiscoveryInputMode.BRIDGE_RESEARCH,
            actor_id="integration-test",
        )
        survivor = next(
            subject
            for subject in second_snapshot.subjects
            if subject.canonical_title == "Stable title"
        )
        absorbed = next(
            subject
            for subject in second_snapshot.subjects
            if subject.canonical_title == "Distinct title"
        )
        async with uow_factory() as uow:
            await uow.subject_merge_events.append_many(
                [
                    SubjectMergeEvent(
                        edition_id=edition.id,
                        from_subject_id=absorbed.subject_id,
                        into_subject_id=survivor.subject_id,
                        merge_run_id=second_snapshot.merge_run_id,
                        actor_id="integration-test",
                        reason="explicit test merge",
                    )
                ]
            )
            await uow.commit()
        async with uow_factory() as uow:
            assert (
                await uow.discovery_subject_identities.resolve_canonical_subject(
                    absorbed.subject_id
                )
                == survivor.subject_id
            )
            closure = await uow.discovery_subject_identities.contribution_closure(
                survivor.subject_id
            )
        assert {item.subject_id for item in closure} == {
            survivor.subject_id,
            absorbed.subject_id,
        }
    finally:
        await engine.dispose()


def _batch(
    edition_id: UUID,
    model_run_id: UUID,
    *,
    title: str = "Stable title",
    url: str = "https://vendor.example/report",
    request_hash: str = "c" * 64,
) -> DiscoveryBatch:
    now = datetime.now(UTC)
    candidate = CandidateTopic(
        title=title,
        summary="Stable summary",
        novelty="New evidence",
        technical_potential=4,
        uncertainties=(),
        relevance_reasons=("Technical source",),
        actors=("Actor",),
        campaigns=("Campaign",),
        malware=("Malware",),
        cves=(),
        victims=(),
        sectors=("government",),
        countries=("Iran",),
        likely_artifacts=("ioc",),
        sources=[
            SourceCandidate(
                url=url,
                title="Report",
                publisher="Vendor",
                role=SourceRole.PRIMARY,
                tlp=TLP.AMBER,
                sensitivity="internal",
                external_llm_allowed=True,
            )
        ],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        local_ref="S1",
    )
    return DiscoveryBatch(
        edition_id=edition_id,
        request_hash=request_hash,
        complementary_axis="initial",
        queries=(),
        citations=(),
        contributions=[
            DiscoveryContribution(
                candidate=candidate,
                status=ContributionStatus.ACCEPTED,
                created_at=now,
                accepted_at=now,
            )
        ],
        discovery_model_run_id=model_run_id,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        parser_version="test-parser-v1",
        report_sha256="d" * 64,
        source_mode=DiscoverySourceMode.MODEL_DECLARED_URLS,
    )
