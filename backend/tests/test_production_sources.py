"""Integration of the publications proposed by reference research.

A URL Q1 proposes may already be attached to the subject, may differ only by
tracking parameters, or may be genuinely new. Only the last case is allowed to
create a collection, and an event survives only if a source behind it was
actually archived.
"""

from __future__ import annotations

from datetime import date
from uuid import uuid4

from cti_app.application.production_context import build_subject_production_context
from cti_app.domain.classification import TLP
from cti_app.domain.collection import (
    CollectionState,
    SourceCollection,
    SourceOriginKind,
)
from cti_app.domain.discovery import SourceRole


def _collection(url: str, state: CollectionState = CollectionState.ARCHIVED) -> SourceCollection:
    return SourceCollection(
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        requested_url=url,
        proposed_role=SourceRole.PRIMARY,
        state=state,
    )


def test_canonical_url_is_derived_and_strips_tracking_parameters() -> None:
    """Q1 quoting a tracked URL must not create a second collection."""
    plain = _collection("https://research.example/rapport")
    tracked = _collection("https://research.example/rapport?utm_source=newsletter")

    assert plain.canonical_url == tracked.canonical_url


def test_canonical_url_removes_known_tracking_and_keeps_business_parameter() -> None:
    collection = _collection(
        "https://research.example/report?id=42&utm_source=chatgpt&utm_medium=x"
        "&utm_campaign=y&utm_term=z&utm_content=a&fbclid=one&gclid=two"
    )

    assert collection.canonical_url == "https://research.example/report?id=42"


def test_trailing_slash_and_case_do_not_create_a_second_source() -> None:
    a = _collection("https://Research.Example/rapport/")
    b = _collection("https://research.example/rapport")

    assert a.canonical_url == b.canonical_url


def test_reference_research_source_needs_no_discovery_batch() -> None:
    """The whole point of the origin kind: no batch, no candidate."""
    collection = SourceCollection(
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        requested_url="https://newly.example/found",
        proposed_role=SourceRole.INDEPENDENT,
        origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
        title="Analyse",
        publisher="Newly",
        published_at=date(2026, 7, 9),
    )

    assert collection.batch_id is None
    assert collection.source_candidate_id is None
    assert collection.origin_kind is SourceOriginKind.REFERENCE_RESEARCH
    assert collection.title == "Analyse"


def test_collection_carries_its_own_diffusion_policy() -> None:
    collection = SourceCollection(
        subject_id=uuid4(),
        edition_id=uuid4(),
        group_id=uuid4(),
        requested_url="https://restricted.example/report",
        proposed_role=SourceRole.PRIMARY,
        origin_kind=SourceOriginKind.REFERENCE_RESEARCH,
        source_tlp=TLP.AMBER_STRICT,
        do_not_submit=True,
        external_llm_allowed=False,
    )

    assert collection.do_not_submit is True
    assert collection.external_llm_allowed is False


# --- Production context ----------------------------------------------------


class _Groups:
    def __init__(self, edition_id: object) -> None:
        self._edition_id = edition_id

    async def get_by_subject(self, subject_id: object) -> object:
        return type(
            "Group",
            (),
            {
                "title": "TAG-182 et MarkiRAT",
                "grouping_justification": "Campagne contre la diaspora",
                "edition_id": self._edition_id,
                "actor_or_campaign": "TAG-182",
            },
        )()


class _Editions:
    async def get(self, edition_id: object) -> object:
        return type(
            "Edition",
            (),
            {"period_start": date(2026, 7, 1), "period_end": date(2026, 7, 31)},
        )()


class _Collections:
    def __init__(self, items: list[SourceCollection]) -> None:
        self._items = items

    async def list_for_subject(self, subject_id: object) -> list[SourceCollection]:
        return self._items


class _Uow:
    def __init__(self, collections: list[SourceCollection]) -> None:
        edition_id = uuid4()
        self.editorial_groups = _Groups(edition_id)
        self.editions = _Editions()
        self.source_collections = _Collections(collections)


async def test_context_carries_the_real_editorial_anchors() -> None:
    """The prompt slots were being filled with empty strings."""
    uow = _Uow([_collection("https://research.example/rapport")])

    ctx = await build_subject_production_context(
        uow,  # type: ignore[arg-type]
        uuid4(),
        date(2026, 8, 1),
    )

    assert ctx.subject_title == "TAG-182 et MarkiRAT"
    assert ctx.subject_description == "Campagne contre la diaspora"
    assert ctx.actor_info == "TAG-182"
    assert ctx.period_start == "2026-07-01"
    assert ctx.period_end == "2026-07-31"
    assert ctx.research_date == date(2026, 8, 1)
    assert "https://research.example/rapport" in ctx.existing_sources_text
    assert "1 publication(s)" in ctx.technical_summary


async def test_context_blocks_external_model_when_a_source_forbids_it() -> None:
    restricted = _collection("https://restricted.example/report")
    restricted.do_not_submit = True
    uow = _Uow([_collection("https://research.example/ok"), restricted])

    ctx = await build_subject_production_context(
        uow,  # type: ignore[arg-type]
        uuid4(),
        date(2026, 8, 1),
    )

    assert ctx.external_llm_allowed is False
    assert ctx.blocking_sources == ("https://restricted.example/report",)


async def test_context_allows_external_model_when_every_source_permits_it() -> None:
    uow = _Uow([_collection("https://a.example/x"), _collection("https://b.example/y")])

    ctx = await build_subject_production_context(
        uow,  # type: ignore[arg-type]
        uuid4(),
        date(2026, 8, 1),
    )

    assert ctx.external_llm_allowed is True
    assert ctx.blocking_sources == ()
