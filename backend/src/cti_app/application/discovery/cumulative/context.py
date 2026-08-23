from __future__ import annotations

import re
from collections.abc import Sequence
from copy import deepcopy
from difflib import SequenceMatcher
from uuid import UUID

from cti_app.application.discovery.cumulative.types import (
    DiscoveryDelta,
    IncomingDiscoveryCandidate,
    ResolvedMergeHandles,
)
from cti_app.application.discovery_identity import normalize
from cti_app.domain.discovery import CandidateTopic, DiscoveryBatch
from cti_app.domain.discovery_cumulative import (
    DiscoveryIntake,
    DiscoverySnapshot,
    DiscoverySubject,
    canonical_sha256,
    discovery_candidate_key,
)

NO_BLOCKING_VERSION = "all-active-v1"
DISCOVERY_BLOCKING_VERSION = "recall-v1"


def build_discovery_delta(intake: DiscoveryIntake, batch: DiscoveryBatch) -> DiscoveryDelta:
    candidates = tuple(
        IncomingDiscoveryCandidate(
            handle=f"C{index}",
            candidate_key=discovery_candidate_key(intake.id, candidate.local_ref or f"S{index}"),
            candidate=deepcopy(candidate),
            batch_id=batch.id,
        )
        for index, candidate in enumerate(batch.candidates, 1)
    )
    return DiscoveryDelta(
        intake_id=intake.id,
        candidates=candidates,
        delta_hash=canonical_sha256(
            [
                _candidate_content(item.candidate, candidate_key=item.candidate_key)
                for item in candidates
            ]
        ),
    )


class DiscoveryBlockingStrategy:
    version = DISCOVERY_BLOCKING_VERSION

    def __init__(
        self,
        *,
        full_context_threshold: int = 30,
        top_n_lexical: int = 10,
        top_n_editorial_neighbors: int = 5,
    ) -> None:
        self.full_context_threshold = full_context_threshold
        self.top_n_lexical = top_n_lexical
        self.top_n_editorial_neighbors = top_n_editorial_neighbors

    def select(
        self,
        parent_snapshot: DiscoverySnapshot | None,
        delta: DiscoveryDelta,
        *,
        editorial_subject_ids: set[UUID] | None = None,
        recent_subject_ids: set[UUID] | None = None,
    ) -> tuple[DiscoverySubject, ...]:
        if parent_snapshot is None:
            return ()
        subjects = tuple(parent_snapshot.subjects)
        if len(subjects) <= self.full_context_threshold:
            return tuple(sorted(subjects, key=lambda item: str(item.subject_id)))

        editorial_ids = editorial_subject_ids or set()
        selected = set(recent_subject_ids or set())
        incoming = tuple(item.candidate for item in delta.candidates)
        for subject in subjects:
            if any(_shares_blocking_key(subject.candidate, candidate) for candidate in incoming):
                selected.add(subject.subject_id)

        scored = sorted(
            (
                max(
                    (_lexical_similarity(subject.candidate, candidate) for candidate in incoming),
                    default=0.0,
                ),
                subject.subject_id,
            )
            for subject in subjects
        )
        selected.update(subject_id for _, subject_id in scored[-self.top_n_lexical :])
        editorial_scored = [item for item in scored if item[1] in editorial_ids and item[0] >= 0.08]
        selected.update(
            subject_id for _, subject_id in editorial_scored[-self.top_n_editorial_neighbors :]
        )
        return tuple(
            sorted(
                (subject for subject in subjects if subject.subject_id in selected),
                key=lambda item: str(item.subject_id),
            )
        )


def build_merge_handles(
    parent_snapshot: DiscoverySnapshot | None,
    delta: DiscoveryDelta,
    *,
    included_subjects: Sequence[DiscoverySubject] | None = None,
) -> ResolvedMergeHandles:
    subjects = (
        sorted(
            included_subjects if included_subjects is not None else parent_snapshot.subjects,
            key=lambda item: str(item.subject_id),
        )
        if parent_snapshot
        else []
    )
    return ResolvedMergeHandles(
        existing={f"X{index}": subject.subject_id for index, subject in enumerate(subjects, 1)},
        incoming={item.handle: item for item in delta.candidates},
    )


DISCOVERY_SUBJECT_PROJECTION_KEYS = frozenset(
    {
        "handle",
        "title",
        "summary",
        "actors",
        "campaigns",
        "malware",
        "cves",
        "victims",
        "sectors",
        "countries",
        "likely_artifacts",
        "technical_potential",
        "uncertainties",
        "sources",
    }
)
DISCOVERY_SOURCE_PROJECTION_KEYS = frozenset(
    {"canonical_url", "title", "publisher", "role", "published_at", "event_date"}
)


def project_merge_subject(handle: str, candidate: CandidateTopic) -> dict[str, object]:
    return {
        "handle": handle,
        "title": candidate.title,
        "summary": candidate.summary,
        "actors": list(candidate.actors),
        "campaigns": list(candidate.campaigns),
        "malware": list(candidate.malware),
        "cves": list(candidate.cves),
        "victims": list(candidate.victims),
        "sectors": list(candidate.sectors),
        "countries": list(candidate.countries),
        "likely_artifacts": list(candidate.likely_artifacts),
        "technical_potential": candidate.technical_potential,
        "uncertainties": list(candidate.uncertainties),
        "sources": [
            {
                "canonical_url": source.canonical_url,
                "title": source.title,
                "publisher": source.publisher,
                "role": source.role.value,
                "published_at": source.published_at.isoformat() if source.published_at else None,
                "event_date": source.event_date.isoformat() if source.event_date else None,
            }
            for source in sorted(candidate.sources, key=lambda item: item.canonical_url)
        ],
    }


def project_merge_input(
    parent_snapshot: DiscoverySnapshot | None,
    handles: ResolvedMergeHandles,
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
    by_id = {
        subject.subject_id: subject
        for subject in (parent_snapshot.subjects if parent_snapshot else ())
    }
    current = [
        project_merge_subject(handle, by_id[subject_id].candidate)
        for handle, subject_id in sorted(
            handles.existing.items(), key=lambda item: _handle_number(item[0])
        )
    ]
    incoming = [
        project_merge_subject(handle, item.candidate)
        for handle, item in sorted(
            handles.incoming.items(), key=lambda item: _handle_number(item[0])
        )
    ]
    return current, incoming


def _candidate_content(
    candidate: CandidateTopic, *, candidate_key: UUID | None = None
) -> dict[str, object]:
    return {
        "candidate_key": str(candidate_key) if candidate_key else None,
        "title": candidate.title,
        "summary": candidate.summary,
        "novelty": candidate.novelty,
        "technical_potential": candidate.technical_potential,
        "event_date": candidate.event_date.isoformat() if candidate.event_date else None,
        "uncertainties": sorted(candidate.uncertainties, key=normalize),
        "relevance_reasons": sorted(candidate.relevance_reasons, key=normalize),
        "actors": sorted(candidate.actors, key=normalize),
        "campaigns": sorted(candidate.campaigns, key=normalize),
        "malware": sorted(candidate.malware, key=normalize),
        "cves": sorted(candidate.cves, key=normalize),
        "victims": sorted(candidate.victims, key=normalize),
        "sectors": sorted(candidate.sectors, key=normalize),
        "countries": sorted(candidate.countries, key=normalize),
        "likely_artifacts": sorted(candidate.likely_artifacts, key=normalize),
        "sources": [
            {
                "id": str(source.id),
                "canonical_url": source.canonical_url,
                "title": source.title,
                "publisher": source.publisher,
                "role": source.role.value,
                "published_at": source.published_at.isoformat() if source.published_at else None,
                "event_date": source.event_date.isoformat() if source.event_date else None,
            }
            for source in sorted(candidate.sources, key=lambda item: item.canonical_url)
        ],
        "provisional_iocs": [
            {
                "id": str(ioc.id),
                "type": ioc.proposed_type.value,
                "value": ioc.normalized_value or ioc.raw_value,
            }
            for ioc in sorted(
                candidate.provisional_iocs,
                key=lambda item: (
                    item.proposed_type.value,
                    item.normalized_value or item.raw_value,
                ),
            )
        ],
    }


def _shares_blocking_key(left: CandidateTopic, right: CandidateTopic) -> bool:
    for field_name in ("actors", "campaigns", "malware", "cves"):
        left_values = {normalize(value) for value in getattr(left, field_name) if normalize(value)}
        right_values = {
            normalize(value) for value in getattr(right, field_name) if normalize(value)
        }
        if left_values & right_values:
            return True
    return bool(
        {source.canonical_url for source in left.sources}
        & {source.canonical_url for source in right.sources}
    )


def _lexical_similarity(left: CandidateTopic, right: CandidateTopic) -> float:
    left_text = normalize(f"{left.title} {left.summary}")
    right_text = normalize(f"{right.title} {right.summary}")
    left_tokens = set(re.findall(r"[a-z0-9][a-z0-9._-]+", left_text))
    right_tokens = set(re.findall(r"[a-z0-9][a-z0-9._-]+", right_text))
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    return max(jaccard, SequenceMatcher(None, left_text, right_text).ratio())


def _handle_number(value: str) -> tuple[str, int]:
    prefix = value[:1]
    try:
        return prefix, int(value[1:])
    except ValueError:
        return value, 0
