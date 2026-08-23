"""Manual URL entry for incomplete (no-URL) discovery publications.

An `IncompleteSourceCandidate` (see domain/discovery.py) has no URL and
cannot be verified. When the analyst knows the real URL for one — either
because automatic recovery (`recover_incomplete_source_urls`) found nothing,
or the match was ambiguous — this module lets them attach it by hand.

The attach must still land in the same auditable intake/batch/merge-run
ledger every other discovery contribution goes through, but it must target
the subject the analyst already picked, not ask a planner to rediscover it:
`HeuristicMergePlanner` can create a spurious new subject when more than one
existing subject shares a title, and the production planner is the
nondeterministic `ChatGptMergePlanner`. `TargetedMergePlanner`
(discovery_cumulative.py) is the deterministic, single-group planner built
for exactly this.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.discovery import ModelOutputArchive
from cti_app.application.discovery_cumulative import (
    CumulativeDiscoveryService,
    TargetedMergePlanner,
)
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.discovery import (
    CandidateTopic,
    ContributionStatus,
    DiscoveryBatch,
    DiscoveryContribution,
    DiscoverySourceMode,
    IncompleteSourceCandidate,
    SourceCandidate,
    canonicalize_http_url,
    deduplicate_sources,
    remap_ioc_publication_ids,
    same_publication,
)
from cti_app.domain.discovery_cumulative import (
    DiscoveryInputMode,
    DiscoverySnapshot,
    DiscoverySubject,
    discovery_candidate_key,
)


class IncompleteSourceCandidateNotFoundError(LookupError):
    pass


@dataclass(frozen=True, slots=True)
class ManualSourceEditResult:
    promoted_source: SourceCandidate
    updated_subject_ids: tuple[UUID, ...]


class ManualSourceEditService:
    def __init__(
        self,
        uow_factory: UnitOfWorkFactory,
        model_output_archive: ModelOutputArchive,
        cumulative_discovery_service: CumulativeDiscoveryService,
    ) -> None:
        self._uow_factory = uow_factory
        self._model_output_archive = model_output_archive
        self._cumulative = cumulative_discovery_service

    async def attach_incomplete_source_url(
        self,
        edition_id: UUID,
        subject_id: UUID,
        incomplete_source_id: UUID,
        url: str,
        *,
        actor_id: str,
    ) -> ManualSourceEditResult:
        # canonicalize_http_url raises ValueError on an unusable URL; the
        # caller (the API layer) is expected to turn that into a 400.
        canonicalize_http_url(url)

        snapshot = await self._cumulative.active_snapshot(edition_id)
        _subject, incomplete = _find_incomplete_source(snapshot, subject_id, incomplete_source_id)
        assert snapshot is not None  # guaranteed by _find_incomplete_source above

        # Cross-instance propagation: any *other* subject whose incomplete
        # source is unambiguously the same publication gets the same fix.
        # The analyst just confirmed this exact title<->URL pairing, and
        # `same_publication` is the same strict rule used everywhere else in
        # the codebase, so applying it everywhere it matches is low-risk.
        targets: list[tuple[UUID, UUID]] = [(subject_id, incomplete_source_id)]
        for other in snapshot.subjects:
            if other.subject_id == subject_id:
                continue
            for candidate_incomplete in other.candidate.incomplete_sources:
                if same_publication(candidate_incomplete, incomplete):
                    targets.append((other.subject_id, candidate_incomplete.id))

        promoted_source: SourceCandidate | None = None
        updated_subject_ids: list[UUID] = []
        for target_subject_id, target_incomplete_id in targets:
            promoted, snapshot = await self._attach_url_to_one_subject(
                edition_id=edition_id,
                snapshot=snapshot,
                subject_id=target_subject_id,
                incomplete_source_id=target_incomplete_id,
                url=url,
                actor_id=actor_id,
            )
            updated_subject_ids.append(target_subject_id)
            if target_subject_id == subject_id:
                promoted_source = promoted

        assert promoted_source is not None  # the requested subject is always in `targets`
        return ManualSourceEditResult(
            promoted_source=promoted_source, updated_subject_ids=tuple(updated_subject_ids)
        )

    async def _attach_url_to_one_subject(
        self,
        *,
        edition_id: UUID,
        snapshot: DiscoverySnapshot | None,
        subject_id: UUID,
        incomplete_source_id: UUID,
        url: str,
        actor_id: str,
    ) -> tuple[SourceCandidate, DiscoverySnapshot]:
        subject, _ = _find_incomplete_source(snapshot, subject_id, incomplete_source_id)
        assert snapshot is not None  # guaranteed by _find_incomplete_source above
        candidate = deepcopy(subject.candidate)
        target = next(
            item for item in candidate.incomplete_sources if item.id == incomplete_source_id
        )

        promoted = SourceCandidate(
            url=url,
            title=target.title,
            publisher=target.publisher,
            role=target.role,
            tlp=candidate.tlp,
            sensitivity=candidate.sensitivity,
            external_llm_allowed=candidate.external_llm_allowed,
            published_at=target.published_at,
            local_ref=target.local_ref,
            period_relation=target.period_relation,
            ioc_presence=target.ioc_presence,
            ioc_declared_count=target.ioc_declared_count,
            ioc_visible_count=target.ioc_visible_count,
            parsing_warnings=(*target.parsing_warnings, "url_attached_manually"),
            markdown_block=target.markdown_block,
        )
        candidate.incomplete_sources = [
            item for item in candidate.incomplete_sources if item.id != incomplete_source_id
        ]
        # Fold against an existing near-duplicate rather than adding a second
        # row for the same article — the same rule used everywhere else.
        merged_sources, source_id_remap = deduplicate_sources([*candidate.sources, promoted])
        candidate.sources = merged_sources
        if source_id_remap:
            candidate.provisional_iocs = remap_ioc_publication_ids(
                candidate.provisional_iocs, source_id_remap
            )
        promoted_id = source_id_remap.get(promoted.id, promoted.id)
        promoted = next(item for item in candidate.sources if item.id == promoted_id)
        candidate.local_ref = "manual-url-attach"

        batch, digest = _build_manual_edit_batch(
            edition_id, subject_id, incomplete_source_id, url, candidate
        )
        await self._model_output_archive.create_manual_research_output(
            batch.discovery_model_run_id,
            _manual_edit_content(edition_id, subject_id, incomplete_source_id, url),
            evidence_pack_hash=digest,
            actor_id=actor_id,
        )
        async with self._uow_factory() as uow:
            existing_batch = await uow.discovery_batches.get_by_request_hash(edition_id, digest)
            if existing_batch is None:
                inserted = await uow.discovery_batches.add_if_absent(batch)
                if not inserted:
                    existing_batch = await uow.discovery_batches.get_by_request_hash(
                        edition_id, digest
                    )
                    if existing_batch is None:
                        raise RuntimeError("Discovery conflict without canonical batch")
                await uow.commit()
            if existing_batch is not None:
                batch = existing_batch

        intake, _ = await self._cumulative.ingest_batch(
            batch, input_mode=DiscoveryInputMode.MANUAL_IMPORT, actor_id=actor_id
        )
        incoming_candidate_key = discovery_candidate_key(intake.id, candidate.local_ref)
        new_snapshot = await self._cumulative.reconcile_intake(
            intake.id,
            expected_parent_snapshot_id=snapshot.id,
            actor_id=actor_id,
            planner_override=TargetedMergePlanner(subject_id, incoming_candidate_key),
        )
        return promoted, new_snapshot


def _find_incomplete_source(
    snapshot: DiscoverySnapshot | None, subject_id: UUID, incomplete_source_id: UUID
) -> tuple[DiscoverySubject, IncompleteSourceCandidate]:
    if snapshot is None:
        raise IncompleteSourceCandidateNotFoundError(str(incomplete_source_id))
    subject = next((item for item in snapshot.subjects if item.subject_id == subject_id), None)
    if subject is None:
        raise IncompleteSourceCandidateNotFoundError(str(incomplete_source_id))
    incomplete = next(
        (item for item in subject.candidate.incomplete_sources if item.id == incomplete_source_id),
        None,
    )
    if incomplete is None:
        raise IncompleteSourceCandidateNotFoundError(str(incomplete_source_id))
    return subject, incomplete


def _manual_edit_content(
    edition_id: UUID, subject_id: UUID, incomplete_source_id: UUID, url: str
) -> bytes:
    return (
        f"manual-url-attach:v1:{edition_id}:{subject_id}:{incomplete_source_id}:{url}"
    ).encode()


def _build_manual_edit_batch(
    edition_id: UUID,
    subject_id: UUID,
    incomplete_source_id: UUID,
    url: str,
    candidate: CandidateTopic,
) -> tuple[DiscoveryBatch, str]:
    digest = hashlib.sha256(
        _manual_edit_content(edition_id, subject_id, incomplete_source_id, url)
    ).hexdigest()
    manual_run_id = uuid5(NAMESPACE_URL, f"cti-discovery-manual-url-attach:{edition_id}:{digest}")
    now = datetime.now(UTC)
    batch = DiscoveryBatch(
        edition_id=edition_id,
        request_hash=digest,
        complementary_axis="manual-url-attach",
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
        discovery_model_run_id=manual_run_id,
        structuring_model_run_id=manual_run_id,
        tlp=candidate.tlp,
        sensitivity=candidate.sensitivity,
        external_llm_allowed=candidate.external_llm_allowed,
        report_sha256=digest,
        source_mode=DiscoverySourceMode.MANUAL_IMPORT,
        source_coverage_complete=False,
        source_coverage_incomplete_reason=(
            "Correction manuelle d'une publication : ne remplace pas une recherche complète."
        ),
    )
    return batch, digest
