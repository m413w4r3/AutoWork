"""LOT 42 -- the references delta must not invent content changes.

An analyst who adds one article reads this delta to know what their
correction touched. Two ways it used to lie:

* a source whose baseline this subject never recorded was reported as
  "content changed";
* the missing baseline was completed from ``source_extractions``, a
  content-addressed table shared by every subject, so another edition's
  capture was attributed to this one.

Both made a one-article correction look like a full corpus rewrite.  AW-010
moved the per-source baseline into the corpus itself: ``content_sha256`` is the
capture the artifact really recorded, and the legacy metadata index is gone.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest

from cti_app.application.production_repairs import (
    _corpus_source_hashes,
    _legacy_source_hashes,
    _source_delta,
)
from cti_app.domain.collection import CollectionState
from cti_app.domain.discovery import SourceRole
from cti_app.domain.production_references import (
    ProductionReferenceCorpusV1,
    ProductionReferenceKind,
    ProductionReferenceResearchStatus,
    ProductionReferenceSourceV1,
    ProductionReferenceTier,
)

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_OTHER_SUBJECT = "c" * 64


def _source(url: str, *, digest: str | None) -> ProductionReferenceSourceV1:
    return ProductionReferenceSourceV1(
        canonical_url=url,
        tier=ProductionReferenceTier.SUPPORTING,
        kind=ProductionReferenceKind.PUBLICATION,
        role=SourceRole.INDEPENDENT,
        title="Source",
        publisher="Publisher",
        published_at=date(2026, 7, 1),
        source_collection_id=uuid4() if digest is not None else None,
        source_document_id=uuid4() if digest is not None else None,
        discovery_candidate_ids=(),
        collection_state=(
            CollectionState.ARCHIVED if digest is not None else CollectionState.UNAVAILABLE
        ),
        content_sha256=digest,
        relevance_reason="Corroborates the core report",
        proposed_by_model=True,
        eligible_for_extraction=digest is not None,
    )


def _corpus(*sources: ProductionReferenceSourceV1) -> ProductionReferenceCorpusV1:
    return ProductionReferenceCorpusV1(
        schema_version=1,
        subject_id=uuid4(),
        research_date=date(2026, 8, 1),
        production_input_hash="d" * 64,
        research_status=ProductionReferenceResearchStatus.COMPLETED,
        sources=sources,
        warnings=(),
    )


class _LeakyExtractions:
    """A cross-subject `source_extractions` view, as the real repository is.

    If the delta consults it, it will hand back another edition's capture --
    which is exactly what must no longer happen.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_for_url(self, canonical_url: str) -> tuple[Any, ...]:
        self.calls.append(canonical_url)
        return (SimpleNamespace(source_content_sha256=_SHA_OTHER_SUBJECT),)


def _urls(entries: list[dict[str, str | None]]) -> list[str | None]:
    return [entry["canonical_url"] for entry in entries]


@pytest.mark.asyncio
async def test_an_unrecorded_baseline_is_unknown_not_changed() -> None:
    kept = "https://example.test/kept"
    added = "https://example.test/added"
    extractions = _LeakyExtractions()
    previous = _corpus(_source(kept, digest=None))
    current = _corpus(_source(kept, digest=_SHA_A), _source(added, digest=_SHA_B))

    delta = await _source_delta(
        SimpleNamespace(source_extractions=extractions),
        previous_urls=[source.canonical_url for source in previous.sources],
        current_urls=[source.canonical_url for source in current.sources],
        previous_hashes=_corpus_source_hashes(previous),
        current_hashes={kept: _SHA_A, added: _SHA_B},
    )

    # The one real change is the added article, and nothing else.
    assert _urls(delta["added_sources"]) == [added]
    assert delta["removed_sources"] == []
    assert delta["changed_content_sources"] == [], (
        "an unknown baseline must never be reported as a content change"
    )
    assert _urls(delta["unknown_baseline_sources"]) == [kept]
    # The shared, cross-subject table is never consulted for a baseline.
    assert extractions.calls == []


@pytest.mark.asyncio
async def test_a_recorded_identical_baseline_is_unchanged() -> None:
    kept = "https://example.test/kept"
    added = "https://example.test/added"
    previous = _corpus(_source(kept, digest=_SHA_A))

    delta = await _source_delta(
        SimpleNamespace(source_extractions=_LeakyExtractions()),
        previous_urls=[source.canonical_url for source in previous.sources],
        current_urls=[kept, added],
        previous_hashes=_corpus_source_hashes(previous),
        current_hashes={kept: _SHA_A, added: _SHA_B},
    )

    assert _urls(delta["unchanged_sources"]) == [kept]
    assert delta["changed_content_sources"] == []
    assert delta["unknown_baseline_sources"] == []
    assert _urls(delta["added_sources"]) == [added]


@pytest.mark.asyncio
async def test_a_genuinely_recaptured_source_is_still_reported_as_changed() -> None:
    """The fix must not blind the delta to a real re-archival."""
    kept = "https://example.test/kept"
    previous = _corpus(_source(kept, digest=_SHA_A))

    delta = await _source_delta(
        SimpleNamespace(source_extractions=_LeakyExtractions()),
        previous_urls=[source.canonical_url for source in previous.sources],
        current_urls=[kept],
        previous_hashes=_corpus_source_hashes(previous),
        current_hashes={kept: _SHA_B},
    )

    assert _urls(delta["changed_content_sources"]) == [kept]
    assert delta["changed_content_sources"][0]["previous_source_sha256"] == _SHA_A
    assert delta["changed_content_sources"][0]["current_source_sha256"] == _SHA_B
    assert delta["unknown_baseline_sources"] == []


@pytest.mark.asyncio
async def test_a_legacy_artifact_keeps_its_recorded_metadata_baseline() -> None:
    """A V4 import still compares against the baseline its metadata recorded."""
    kept = "https://example.test/kept"
    base = SimpleNamespace(metadata={"archived_sources": [[kept, _SHA_A]]})

    assert _legacy_source_hashes(base) == {kept: _SHA_A}

    delta = await _source_delta(
        SimpleNamespace(source_extractions=_LeakyExtractions()),
        previous_urls=[kept],
        current_urls=[kept],
        previous_hashes=_legacy_source_hashes(base),
        current_hashes={kept: _SHA_A},
    )

    assert _urls(delta["unchanged_sources"]) == [kept]
