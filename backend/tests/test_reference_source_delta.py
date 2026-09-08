"""LOT 42 -- the references delta must not invent content changes.

An analyst who adds one article reads this delta to know what their
correction touched. Two ways it used to lie:

* a source whose baseline this subject never recorded was reported as
  "content changed";
* the missing baseline was completed from ``source_extractions``, a
  content-addressed table shared by every subject, so another edition's
  capture was attributed to this one.

Both made a one-article correction look like a full corpus rewrite.
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from typing import Any

import pytest

from cti_app.application.production_parsers import ParsedSource, ReferenceReport
from cti_app.application.production_repairs import _source_delta
from cti_app.domain.discovery import SourceRole

_SHA_A = "a" * 64
_SHA_B = "b" * 64
_SHA_OTHER_SUBJECT = "c" * 64


def _source(index: int, url: str) -> ParsedSource:
    return ParsedSource(
        local_id=f"S{index}",
        title=f"Source {index}",
        url=url,
        canonical_url=url,
        publisher="Publisher",
        published_at=date(2026, 7, index),
        role=SourceRole.INDEPENDENT,
    )


def _report(*urls: str) -> ReferenceReport:
    return ReferenceReport(
        sources=tuple(_source(index, url) for index, url in enumerate(urls, start=1)),
        events=(),
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


def _artifact(metadata: dict[str, Any]) -> Any:
    return SimpleNamespace(metadata=metadata)


def _urls(entries: list[dict[str, str | None]]) -> list[str | None]:
    return [entry["canonical_url"] for entry in entries]


@pytest.mark.asyncio
async def test_an_unrecorded_baseline_is_unknown_not_changed() -> None:
    kept = "https://example.test/kept"
    added = "https://example.test/added"
    extractions = _LeakyExtractions()

    delta = await _source_delta(
        SimpleNamespace(source_extractions=extractions),
        previous_report=_report(kept),
        current_report=_report(kept, added),
        archived_projection=((kept, _SHA_A), (added, _SHA_B)),
        # A legacy REFERENCES artifact: no recorded per-source baseline.
        base_artifact=_artifact({}),
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

    delta = await _source_delta(
        SimpleNamespace(source_extractions=_LeakyExtractions()),
        previous_report=_report(kept),
        current_report=_report(kept, added),
        archived_projection=((kept, _SHA_A), (added, _SHA_B)),
        base_artifact=_artifact({"archived_sources": [[kept, _SHA_A]]}),
    )

    assert _urls(delta["unchanged_sources"]) == [kept]
    assert delta["changed_content_sources"] == []
    assert delta["unknown_baseline_sources"] == []
    assert _urls(delta["added_sources"]) == [added]


@pytest.mark.asyncio
async def test_a_genuinely_recaptured_source_is_still_reported_as_changed() -> None:
    """The fix must not blind the delta to a real re-archival."""
    kept = "https://example.test/kept"

    delta = await _source_delta(
        SimpleNamespace(source_extractions=_LeakyExtractions()),
        previous_report=_report(kept),
        current_report=_report(kept),
        archived_projection=((kept, _SHA_B),),
        base_artifact=_artifact({"repair_source_index": {"source_hashes": {kept: _SHA_A}}}),
    )

    assert _urls(delta["changed_content_sources"]) == [kept]
    assert delta["changed_content_sources"][0]["previous_source_sha256"] == _SHA_A
    assert delta["changed_content_sources"][0]["current_source_sha256"] == _SHA_B
    assert delta["unknown_baseline_sources"] == []
