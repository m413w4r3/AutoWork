"""Checkpoint assertions against canonical PostgreSQL state."""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from cti_app.application.edition_review import EditionReviewService
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.edition_composition import CompositionDisposition
from cti_app.domain.production import ProductionArtifactStage, ProductionArtifactStatus
from cti_app.domain.publication_review import PublicationDecision
from cti_app.infrastructure.database.models.edition_publication import PublicationManifestRow
from cti_app.infrastructure.database.models.jobs import JobRow
from cti_app.infrastructure.database.models.model_execution import ModelRunRow


def require(condition: bool, scenario: str, step: str, detail: str) -> None:
    if not condition:
        raise AssertionError(f"{scenario} / {step}: {detail}")


async def assert_manifest_entries_verified(
    uow_factory: UnitOfWorkFactory, edition_id: UUID, *, scenario: str, step: str
) -> tuple[str, ...]:
    checked: list[str] = []
    async with uow_factory() as uow:
        manifests = await uow._session.scalars(
            select(PublicationManifestRow)
            .where(PublicationManifestRow.edition_id == edition_id)
            .order_by(PublicationManifestRow.created_at, PublicationManifestRow.id)
        )
        for row in manifests:
            entries = await uow.publication_manifest_entries.list_for_manifest(row.id)
            for entry in entries:
                run = await uow.production_runs.get(entry.production_run_id)
                artifact = await uow.production_artifacts.get(entry.document_artifact_id)
                require(run is not None, scenario, step, "manifest run is absent from PostgreSQL")
                require(
                    artifact is not None,
                    scenario,
                    step,
                    "manifest publication artifact is absent from PostgreSQL",
                )
                assert run is not None and artifact is not None
                require(
                    row.edition_id == run.edition_id,
                    scenario,
                    step,
                    "manifest, run, and artifact editions differ",
                )
                require(
                    entry.subject_id == run.subject_id == artifact.subject_id,
                    scenario,
                    step,
                    "manifest, run, and artifact subjects differ",
                )
                require(
                    entry.production_run_id == artifact.production_run_id,
                    scenario,
                    step,
                    "manifest points at another production run",
                )
                require(
                    artifact.stage is ProductionArtifactStage.PUBLICATION
                    and artifact.status is ProductionArtifactStatus.VERIFIED,
                    scenario,
                    step,
                    "manifest artifact is not a verified PUBLICATION artifact",
                )
                require(
                    entry.document_artifact_version == artifact.version
                    and entry.document_input_hash == artifact.input_hash
                    and entry.pipeline_generation == run.pipeline_generation,
                    scenario,
                    step,
                    "manifest artifact version, hash, or generation differs",
                )
                checked.append(f"manifest-entry:{entry.subject_id}")
    return tuple(checked)


async def assert_manifest_exclusions_applicable(
    uow_factory: UnitOfWorkFactory, edition_id: UUID, *, scenario: str, step: str
) -> tuple[str, ...]:
    checked: list[str] = []
    async with uow_factory() as uow:
        manifests = await uow._session.scalars(
            select(PublicationManifestRow).where(PublicationManifestRow.edition_id == edition_id)
        )
        decisions = {
            item.id: item
            for item in await uow.publication_review_decisions.list_for_edition(edition_id)
        }
        for row in manifests:
            exclusions = await uow.publication_manifest_exclusions.list_for_manifest(row.id)
            for exclusion in exclusions:
                decision = decisions.get(exclusion.review_decision_id)
                require(
                    decision is not None,
                    scenario,
                    step,
                    "manifest exclusion decision is absent from PostgreSQL",
                )
                assert decision is not None
                require(
                    decision.edition_id == edition_id
                    and decision.subject_id == exclusion.subject_id
                    and decision.decision is PublicationDecision.EXCLUDE,
                    scenario,
                    step,
                    "manifest exclusion decision does not apply to its edition and subject",
                )
                require(
                    bool(decision.reason.strip()) and bool(decision.actor_id.strip()),
                    scenario,
                    step,
                    "manifest exclusion has no audit actor or reason",
                )
                checked.append(f"manifest-exclusion:{exclusion.subject_id}")
    return tuple(checked)


async def assert_deferred_subjects_not_excluded(
    uow_factory: UnitOfWorkFactory, edition_id: UUID, *, scenario: str, step: str
) -> tuple[str, ...]:
    async with uow_factory() as uow:
        composition = await uow.edition_compositions.get_for_edition(edition_id)
        manifests = await uow._session.scalars(
            select(PublicationManifestRow).where(PublicationManifestRow.edition_id == edition_id)
        )
        deferred = {
            entry.subject_id
            for entry in (composition.entries if composition is not None else ())
            if entry.disposition is CompositionDisposition.DEFERRED
        }
        manifested_exclusions: set[UUID] = set()
        for row in manifests:
            exclusions = await uow.publication_manifest_exclusions.list_for_manifest(row.id)
            manifested_exclusions.update(item.subject_id for item in exclusions)

    applicable_exclusions = {
        item.subject_id
        for item in (await EditionReviewService(uow_factory).get(edition_id)).exclusions
        if item.applicable
    }
    require(
        deferred.isdisjoint(manifested_exclusions),
        scenario,
        step,
        "a deferred subject is persisted as a publication exclusion",
    )
    require(
        deferred.isdisjoint(applicable_exclusions),
        scenario,
        step,
        "a deferred subject has an applicable exclusion decision",
    )
    return (
        "deferred-not-excluded",
        f"checked={len(deferred)}",
        f"manifests={len(manifested_exclusions)}",
    )


async def production_state(
    uow_factory: UnitOfWorkFactory, edition_id: UUID
) -> tuple[tuple[str, ...], ...]:
    async with uow_factory() as uow:
        runs = await uow.production_runs.list_for_edition(edition_id)
        state: list[tuple[str, ...]] = []
        for run in runs:
            artifacts = await uow.production_artifacts.list_for_run(run.id)
            artifact_identities: list[str] = []
            for artifact in artifacts:
                blob = (
                    await uow.blobs.get(artifact.canonical_blob_id)
                    if artifact.canonical_blob_id is not None
                    else None
                )
                artifact_identities.append(
                    ":".join(
                        (
                            str(artifact.id),
                            artifact.stage.value,
                            str(artifact.version),
                            artifact.status.value,
                            artifact.input_hash,
                            str(artifact.canonical_blob_id or ""),
                            (
                                blob.descriptor.sha256
                                if blob is not None
                                else "missing-canonical-blob"
                            ),
                        )
                    )
                )
            state.append(
                (
                    str(run.id),
                    run.status.value,
                    str(run.pipeline_generation),
                    *sorted(artifact_identities),
                )
            )
        return tuple(sorted(state))


async def assert_production_state_unchanged(
    uow_factory: UnitOfWorkFactory,
    edition_id: UUID,
    before: tuple[tuple[str, ...], ...],
    *,
    scenario: str,
    step: str,
) -> tuple[str, ...]:
    after = await production_state(uow_factory, edition_id)
    require(before == after, scenario, step, "Discovery changed a run or existing artifact")
    return ("run-status-and-artifact-identities-unchanged",)


async def work_counts(uow_factory: UnitOfWorkFactory) -> tuple[int, int]:
    async with uow_factory() as uow:
        jobs = await uow._session.scalar(select(func.count()).select_from(JobRow))
        model_runs = await uow._session.scalar(select(func.count()).select_from(ModelRunRow))
    return int(jobs or 0), int(model_runs or 0)


async def assert_no_jobs_or_model_runs_created(
    uow_factory: UnitOfWorkFactory,
    before: tuple[int, int],
    *,
    scenario: str,
    step: str,
) -> tuple[str, ...]:
    after = await work_counts(uow_factory)
    require(before == after, scenario, step, "composition or exclusion created jobs/model_runs")
    return ("jobs-and-model-runs-unchanged",)


async def manifest_hashes(uow_factory: UnitOfWorkFactory, edition_id: UUID) -> dict[UUID, str]:
    async with uow_factory() as uow:
        rows = await uow._session.execute(
            select(PublicationManifestRow.id, PublicationManifestRow.content_sha256).where(
                PublicationManifestRow.edition_id == edition_id
            )
        )
        return {manifest_id: digest for manifest_id, digest in rows}


async def assert_manifest_hashes_unchanged(
    uow_factory: UnitOfWorkFactory,
    edition_id: UUID,
    before: dict[UUID, str],
    *,
    scenario: str,
    step: str,
) -> tuple[str, ...]:
    after = await manifest_hashes(uow_factory, edition_id)
    require(
        all(after.get(manifest_id) == digest for manifest_id, digest in before.items()),
        scenario,
        step,
        "a historical publication manifest content_sha256 changed",
    )
    return ("historical-manifest-hashes-unchanged", f"checked={len(before)}")


async def assert_persisted_manifest_matches_accept(
    uow_factory: UnitOfWorkFactory,
    manifest_id: UUID,
    returned_manifest: Any,
    *,
    scenario: str,
    step: str,
) -> tuple[str, ...]:
    async with uow_factory() as uow:
        persisted = await uow.publication_manifests.get(manifest_id)
        entries = await uow.publication_manifest_entries.list_for_manifest(manifest_id)
        exclusions = await uow.publication_manifest_exclusions.list_for_manifest(manifest_id)
        manifest_row = await uow._session.get(PublicationManifestRow, manifest_id)
    require(persisted is not None, scenario, step, "accepted manifest is absent from PostgreSQL")
    require(manifest_row is not None, scenario, step, "accepted manifest row is absent")
    assert persisted is not None and manifest_row is not None
    require(
        persisted.content_sha256 == returned_manifest.content_sha256
        and persisted.to_json() == returned_manifest.to_json(),
        scenario,
        step,
        "persisted manifest differs from the object returned by accept",
    )
    persisted_entry_payloads = [entry.to_json() for entry in entries]
    expected_entry_payloads = [entry.to_json() for entry in returned_manifest.entries]
    persisted_exclusion_payloads = [item.to_json() for item in exclusions]
    expected_exclusion_payloads = [item.to_json() for item in returned_manifest.exclusions]
    require(
        persisted_entry_payloads == expected_entry_payloads,
        scenario,
        step,
        "publication_manifest_entries differ from the accepted manifest",
    )
    require(
        persisted_exclusion_payloads == expected_exclusion_payloads,
        scenario,
        step,
        "publication_manifest_exclusions differ from the accepted manifest",
    )
    return (
        "manifest-row-matches-accept-result",
        "manifest-entry-rows-match-accept-result",
        "manifest-exclusion-rows-match-accept-result",
    )
