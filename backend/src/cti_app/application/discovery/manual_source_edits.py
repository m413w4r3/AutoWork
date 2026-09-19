"""Manual URL entry and replacement for discovery publications.

An `IncompleteSourceCandidate` (see domain/discovery.py) has no URL and
cannot be verified. When the analyst knows the real URL for one — either
because automatic recovery (`recover_incomplete_source_urls`) found nothing,
or the match was ambiguous — this module lets them attach it by hand.

The attach or replacement must still land in the same auditable intake/batch/merge-run
ledger every other discovery contribution goes through, but it must target
the subject the analyst already picked, not ask a planner to rediscover it:
`HeuristicMergePlanner` can create a spurious new subject when more than one
existing subject shares a title, and the production planner is the
nondeterministic `ChatGptMergePlanner`. `TargetedMergePlanner`
(discovery/cumulative/planners.py) is the deterministic, single-group planner
built for exactly this.
"""

from __future__ import annotations

import hashlib
from copy import deepcopy
from dataclasses import dataclass
from typing import Literal
from uuid import NAMESPACE_URL, UUID, uuid5

from cti_app.application.discovery.cumulative.planners import TargetedMergePlanner
from cti_app.application.discovery.cumulative.service import CumulativeDiscoveryService
from cti_app.application.discovery.ports import ModelOutputArchive
from cti_app.application.persistence import UnitOfWorkFactory
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryBatch,
    DiscoveryCandidate,
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
)


class IncompleteSourceCandidateNotFoundError(LookupError):
    pass


class SourceCandidateNotFoundError(LookupError):
    pass


class ManualSourceEditOriginUnusableError(LookupError):
    """Base for "this candidate cannot be the origin of a manual edit"."""


class ManualSourceEditOriginNotFoundError(ManualSourceEditOriginUnusableError):
    pass


class ManualSourceEditSupersededError(ManualSourceEditOriginUnusableError):
    """The edited candidate has already been replaced by an earlier correction.

    A client holding a stale candidate list would otherwise publish a second
    replacement for the same historical candidate, which would make "active"
    ambiguous. The caller must reload and edit the current candidate.
    """


ManualSourceEditOperation = Literal["attach", "replace"]


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
        candidate_id: UUID,
        incomplete_source_id: UUID,
        url: str,
        *,
        actor_id: str,
    ) -> ManualSourceEditResult:
        # canonicalize_http_url raises ValueError on an unusable URL; the
        # caller (the API layer) is expected to turn that into a 400.
        canonicalize_http_url(url)

        try:
            canonical_candidate, _ = await self._get_candidate_for_edit(edition_id, candidate_id)
        except ManualSourceEditOriginNotFoundError as exc:
            raise IncompleteSourceCandidateNotFoundError(str(candidate_id)) from exc
        incomplete = _find_incomplete_source(canonical_candidate, incomplete_source_id)
        snapshot = await self._cumulative.active_snapshot(edition_id)
        subject = _legacy_subject_for_candidate(snapshot, candidate_id)
        assert snapshot is not None

        # Cross-instance propagation: any *other* legacy subject holding a
        # persisted candidate whose incomplete source is unambiguously the same
        # publication gets the same fix. The analyst just confirmed this exact
        # title<->URL pairing, and `same_publication` is the same strict rule
        # used everywhere else in the codebase.
        targets: list[tuple[UUID, DiscoveryCandidate, UUID]] = [
            (subject.subject_id, canonical_candidate, incomplete_source_id)
        ]
        for other in snapshot.subjects:
            if other.subject_id == subject.subject_id:
                continue
            for reference in other.member_references:
                if reference.candidate_id == candidate_id:
                    continue
                try:
                    other_candidate, _ = await self._get_candidate_for_edit(
                        edition_id, reference.candidate_id
                    )
                except ManualSourceEditOriginUnusableError:
                    continue
                match = next(
                    (
                        item
                        for item in other_candidate.evidence.incomplete_sources
                        if same_publication(item, incomplete)
                    ),
                    None,
                )
                if match is not None:
                    targets.append((other.subject_id, other_candidate, match.id))
                    break

        promoted_source: SourceCandidate | None = None
        updated_subject_ids: list[UUID] = []
        for target_subject_id, target_candidate, target_incomplete_id in targets:
            promoted, snapshot = await self._attach_url_to_candidate(
                edition_id=edition_id,
                snapshot=snapshot,
                subject_id=target_subject_id,
                canonical_candidate=target_candidate,
                incomplete_source_id=target_incomplete_id,
                url=url,
                actor_id=actor_id,
            )
            updated_subject_ids.append(target_subject_id)
            if target_candidate.id == candidate_id:
                promoted_source = promoted

        assert promoted_source is not None  # the requested candidate is always targeted
        return ManualSourceEditResult(
            promoted_source=promoted_source, updated_subject_ids=tuple(updated_subject_ids)
        )

    async def _attach_url_to_candidate(
        self,
        *,
        edition_id: UUID,
        snapshot: DiscoverySnapshot,
        subject_id: UUID,
        canonical_candidate: DiscoveryCandidate,
        incomplete_source_id: UUID,
        url: str,
        actor_id: str,
    ) -> tuple[SourceCandidate, DiscoverySnapshot]:
        candidate = canonical_candidate.to_candidate_topic()
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
        merged_sources, source_id_remap = deduplicate_sources([*candidate.sources, promoted])
        candidate.sources = merged_sources
        if source_id_remap:
            candidate.provisional_iocs = remap_ioc_publication_ids(
                candidate.provisional_iocs, source_id_remap
            )
        promoted_id = source_id_remap.get(promoted.id, promoted.id)
        promoted = next(item for item in candidate.sources if item.id == promoted_id)
        candidate.local_ref = "manual-url-attach"
        new_snapshot = await self._record_manual_edit(
            edition_id=edition_id,
            subject_id=subject_id,
            source_id=incomplete_source_id,
            url=url,
            candidate=candidate,
            original_candidate=canonical_candidate,
            snapshot=snapshot,
            discovery_run_id=canonical_candidate.discovery_run_id,
            actor_id=actor_id,
            operation="attach",
        )
        return promoted, new_snapshot

    async def attach_replacement_source_url(
        self,
        edition_id: UUID,
        candidate_id: UUID,
        replaced_canonical_url: str,
        url: str,
        *,
        actor_id: str,
    ) -> ManualSourceEditResult:
        # canonicalize_http_url raises ValueError on an unusable URL; the
        # caller (the API layer) is expected to turn that into a 400.
        canonicalize_http_url(url)

        try:
            canonical_candidate, discovery_run_id = await self._get_candidate_for_edit(
                edition_id, candidate_id
            )
        except ManualSourceEditOriginNotFoundError as exc:
            raise SourceCandidateNotFoundError(str(candidate_id)) from exc
        replaced = _find_source(canonical_candidate, replaced_canonical_url)
        snapshot = await self._cumulative.active_snapshot(edition_id)
        subject = _legacy_subject_for_candidate(snapshot, candidate_id)
        assert snapshot is not None

        # Do not propagate a replacement to other subjects: the same URL can
        # describe a different source in another editorial subject.
        candidate = canonical_candidate.to_candidate_topic()
        replacement = SourceCandidate(
            url=url,
            title=replaced.title,
            publisher=replaced.publisher,
            role=replaced.role,
            tlp=candidate.tlp,
            sensitivity=candidate.sensitivity,
            external_llm_allowed=candidate.external_llm_allowed,
            published_at=replaced.published_at,
            event_date=replaced.event_date,
            citation=replaced.citation,
            local_ref=replaced.local_ref,
            period_relation=replaced.period_relation,
            ioc_presence=replaced.ioc_presence,
            ioc_declared_count=replaced.ioc_declared_count,
            ioc_visible_count=replaced.ioc_visible_count,
            parsing_warnings=(*replaced.parsing_warnings, "url_replaced_manually"),
            markdown_block=replaced.markdown_block,
        )
        candidate.sources = [source for source in candidate.sources if source.id != replaced.id]
        # Fold against an existing near-duplicate rather than adding a second
        # row for the same article — the same rule used everywhere else.
        merged_sources, source_id_remap = deduplicate_sources([*candidate.sources, replacement])
        replacement_id = source_id_remap.get(replacement.id, replacement.id)
        # The replaced source is no longer present, so its IOC relations must
        # follow the replacement (or the surviving near-duplicate).
        source_id_remap[replaced.id] = replacement_id
        candidate.sources = merged_sources
        candidate.provisional_iocs = remap_ioc_publication_ids(
            candidate.provisional_iocs, source_id_remap
        )
        promoted = next(item for item in candidate.sources if item.id == replacement_id)
        candidate.local_ref = "manual-url-replace"

        await self._record_manual_edit(
            edition_id=edition_id,
            subject_id=subject.subject_id,
            source_id=replaced.id,
            url=url,
            candidate=candidate,
            original_candidate=canonical_candidate,
            snapshot=snapshot,
            discovery_run_id=discovery_run_id,
            actor_id=actor_id,
            operation="replace",
            replaced_canonical_url=replaced_canonical_url,
        )
        return ManualSourceEditResult(
            promoted_source=promoted,
            updated_subject_ids=(subject.subject_id,),
        )

    async def _get_candidate_for_edit(
        self, edition_id: UUID, candidate_id: UUID
    ) -> tuple[DiscoveryCandidate, UUID]:
        async with self._uow_factory() as uow:
            candidate = await uow.discovery_candidates.get(candidate_id)
            if candidate is None:
                raise ManualSourceEditOriginNotFoundError(str(candidate_id))
            run = await uow.discovery_runs.get(candidate.discovery_run_id)
            if run is None or run.edition_id != edition_id:
                raise ManualSourceEditOriginNotFoundError(str(candidate_id))
            active = await uow.discovery_candidates.list_for_run(candidate.discovery_run_id)
            if all(item.id != candidate_id for item in active):
                raise ManualSourceEditSupersededError(str(candidate_id))
            return candidate, candidate.discovery_run_id

    async def _record_manual_edit(
        self,
        *,
        edition_id: UUID,
        subject_id: UUID,
        source_id: UUID,
        url: str,
        candidate: CandidateTopic,
        original_candidate: DiscoveryCandidate,
        snapshot: DiscoverySnapshot,
        discovery_run_id: UUID,
        actor_id: str,
        operation: ManualSourceEditOperation,
        replaced_canonical_url: str | None = None,
    ) -> DiscoverySnapshot:
        async with self._uow_factory() as uow:
            batch, digest = _build_manual_edit_batch(
                edition_id,
                subject_id,
                source_id,
                url,
                candidate,
                discovery_run_id=discovery_run_id,
                operation=operation,
                replaced_canonical_url=replaced_canonical_url,
            )
            await self._model_output_archive.create_manual_research_output(
                batch.discovery_model_run_id,
                _manual_edit_content(
                    edition_id,
                    subject_id,
                    source_id,
                    url,
                    operation=operation,
                    replaced_canonical_url=replaced_canonical_url,
                ),
                evidence_pack_hash=digest,
                actor_id=actor_id,
                operation=operation,
            )
            existing_batch = await uow.discovery_batches.get(batch.id)
            if existing_batch is None:
                inserted = await uow.discovery_batches.add_if_absent(batch)
                if not inserted:
                    existing_batch = await uow.discovery_batches.get(batch.id)
                    if existing_batch is None:
                        raise RuntimeError("Discovery conflict without canonical batch")
                else:
                    # La correction publie une nouvelle DiscoveryCandidate. Sans ce
                    # lien, la liste brute de l'édition montrerait côte à côte la
                    # candidate d'origine et sa version corrigée.
                    await uow.discovery_candidates.mark_supersedes(
                        batch.candidates[0].id, original_candidate.id
                    )
                await uow.commit()
            if existing_batch is not None:
                batch = existing_batch

        intake, _ = await self._cumulative.ingest_batch(
            batch, input_mode=DiscoveryInputMode.MANUAL_IMPORT, actor_id=actor_id
        )
        # L'identité de fusion est l'UUID canonique de la candidate corrigée,
        # jamais une clé dérivée du `local_ref` : deux corrections successives
        # partagent le même `local_ref` mais sont deux candidates distinctes.
        return await self._cumulative.reconcile_intake(
            intake.id,
            expected_parent_snapshot_id=snapshot.id,
            actor_id=actor_id,
            planner_override=TargetedMergePlanner(
                subject_id, batch.candidates[0].id, operation=operation
            ),
        )


def _find_incomplete_source(
    candidate: DiscoveryCandidate, incomplete_source_id: UUID
) -> IncompleteSourceCandidate:
    incomplete = next(
        (item for item in candidate.evidence.incomplete_sources if item.id == incomplete_source_id),
        None,
    )
    if incomplete is None:
        raise IncompleteSourceCandidateNotFoundError(str(incomplete_source_id))
    return incomplete


def _find_source(
    candidate: DiscoveryCandidate, canonical_url: str
) -> SourceCandidate:
    source = next(
        (item for item in candidate.evidence.sources if item.canonical_url == canonical_url),
        None,
    )
    if source is None:
        raise SourceCandidateNotFoundError(canonical_url)
    return source


def _legacy_subject_for_candidate(
    snapshot: DiscoverySnapshot | None, candidate_id: UUID
) -> DiscoverySubject:
    if snapshot is None or not snapshot.is_active:
        raise ManualSourceEditOriginNotFoundError(
            f"No active discovery snapshot exists for candidate {candidate_id}"
        )
    matches = [
        subject
        for subject in snapshot.subjects
        if any(reference.candidate_id == candidate_id for reference in subject.member_references)
    ]
    if len(matches) != 1:
        raise ManualSourceEditOriginNotFoundError(
            f"Candidate {candidate_id} maps to {len(matches)} active discovery subjects"
        )
    return matches[0]


# Not produced by discovery_report_parser: identifies analyst-attached URLs.
MANUAL_SOURCE_EDIT_VERSION = "manual-url-attach-v1"


def _manual_edit_content(
    edition_id: UUID,
    subject_id: UUID,
    source_id: UUID,
    url: str,
    *,
    operation: ManualSourceEditOperation = "attach",
    replaced_canonical_url: str | None = None,
) -> bytes:
    _validate_operation(operation)
    if operation == "replace":
        if replaced_canonical_url is None:
            raise ValueError("A replacement edit requires the replaced canonical URL")
        content = (
            f"manual-url-replace:v1:{edition_id}:{subject_id}:{source_id}:"
            f"{replaced_canonical_url}:{url}"
        )
    else:
        content = f"manual-url-attach:v1:{edition_id}:{subject_id}:{source_id}:{url}"
    return content.encode()


def _build_manual_edit_batch(
    edition_id: UUID,
    subject_id: UUID,
    incomplete_source_id: UUID,
    url: str,
    candidate: CandidateTopic,
    *,
    discovery_run_id: UUID,
    operation: ManualSourceEditOperation = "attach",
    replaced_canonical_url: str | None = None,
) -> tuple[DiscoveryBatch, str]:
    _validate_operation(operation)
    digest = hashlib.sha256(
        _manual_edit_content(
            edition_id,
            subject_id,
            incomplete_source_id,
            url,
            operation=operation,
            replaced_canonical_url=replaced_canonical_url,
        )
    ).hexdigest()
    batch_id = uuid5(
        NAMESPACE_URL, f"cti-discovery-manual-url-batch:{discovery_run_id}:{digest}"
    )
    manual_run_id = uuid5(
        NAMESPACE_URL, f"cti-discovery-manual-url-run:{discovery_run_id}:{digest}"
    )
    candidate = deepcopy(candidate)
    candidate.id = uuid5(
        NAMESPACE_URL, f"cti-discovery-manual-url-candidate:{batch_id}:0"
    )
    batch = DiscoveryBatch(
        edition_id=edition_id,
        discovery_run_id=discovery_run_id,
        request_hash=digest,
        complementary_axis=f"manual-url-{operation}",
        queries=(),
        citations=(),
        candidates=[candidate],
        discovery_model_run_id=manual_run_id,
        tlp=candidate.tlp,
        sensitivity=candidate.sensitivity,
        external_llm_allowed=candidate.external_llm_allowed,
        parser_version=f"manual-url-{operation}-v1",
        report_sha256=digest,
        source_mode=DiscoverySourceMode.MANUAL_IMPORT,
        source_coverage_complete=False,
        source_coverage_incomplete_reason=(
            "Correction manuelle d'une publication : ne remplace pas une recherche complète."
        ),
        id=batch_id,
    )
    return batch, digest


def _validate_operation(operation: str) -> None:
    if operation not in {"attach", "replace"}:
        raise ValueError("Manual source edit operation must be attach or replace")
