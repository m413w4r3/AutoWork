"""Tests for the shared publication-identity/dedup machinery in domain/discovery.py.

This is the fix for the "same article proposed 10+ times" bug: `same_publication`
is the single rule used everywhere "is this a duplicate of / the same article as"
needs deciding (intra-batch dedup, cross-batch merge, and incomplete-source URL
recovery), and `deduplicate_sources`/`deduplicate_incomplete_sources` are the
functions that apply it.
"""

from __future__ import annotations

from datetime import date
from typing import Any

from cti_app.domain.classification import TLP
from cti_app.domain.discovery import (
    CandidateTopic,
    DiscoveryIocType,
    IncompleteSourceCandidate,
    ProvisionalDiscoveryIoc,
    ProvisionalIocPublicationRelation,
    SourceCandidate,
    SourceRole,
    deduplicate_incomplete_sources,
    deduplicate_sources,
    recover_incomplete_source_urls,
    remap_ioc_publication_ids,
    same_publication,
)


def make_source(
    url: str,
    title: str = "Iran central bank says no new disruption after banking cyberattack",
    publisher: str = "bne IntelliNews",
    published_at: date | None = None,
    **kwargs: Any,
) -> SourceCandidate:
    return SourceCandidate(
        url=url,
        title=title,
        publisher=publisher,
        role=SourceRole.INDEPENDENT,
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
        published_at=published_at,
        **kwargs,
    )


def make_incomplete(
    title: str = "Iran central bank says no new disruption after banking cyberattack",
    publisher: str = "bne IntelliNews",
    **kwargs: Any,
) -> IncompleteSourceCandidate:
    return IncompleteSourceCandidate(title=title, publisher=publisher, **kwargs)


def test_same_publication_true_on_exact_canonical_url() -> None:
    a = make_source("https://example.com/article", title="Different title A")
    b = make_source("https://example.com/article?utm_source=x", title="Different title B")
    assert same_publication(a, b) is True


def test_same_publication_requires_corroborator_not_title_alone() -> None:
    # Same title, but different publisher AND different date: two genuinely
    # distinct articles (e.g. recurring "Weekly roundup" pieces) must not merge.
    a = make_source(
        "https://a.example/one", title="Weekly threat roundup", publisher="Vendor A",
        published_at=date(2026, 5, 1),
    )
    b = make_source(
        "https://b.example/two", title="Weekly threat roundup", publisher="Vendor B",
        published_at=date(2026, 5, 8),
    )
    assert same_publication(a, b) is False


def test_same_publication_matches_on_title_plus_publisher() -> None:
    a = make_source("https://mirror-one.example/story?ref=123", publisher="bne IntelliNews")
    b = make_source("https://mirror-two.example/story?session=abc", publisher="bne IntelliNews")
    assert same_publication(a, b) is True


def test_same_publication_matches_publisher_regardless_of_word_order() -> None:
    """Co-published/syndicated bylines are often written both ways (e.g. a
    report jointly attributed to two organizations, order not meaningful) —
    regression test for a real case where "Insikt Group / Recorded Future"
    and "Recorded Future / Insikt Group" failed to corroborate a match."""
    a = make_source("https://a.example/story", publisher="Insikt Group / Recorded Future")
    b = make_incomplete(publisher="Recorded Future / Insikt Group")
    assert same_publication(a, b) is True


def test_same_publication_matches_on_title_plus_date() -> None:
    a = make_source(
        "https://mirror-one.example/story", publisher="Publisher A", published_at=date(2026, 5, 12)
    )
    b = make_source(
        "https://mirror-two.example/story", publisher="Publisher B", published_at=date(2026, 5, 12)
    )
    assert same_publication(a, b) is True


def test_same_publication_never_matches_placeholder_titles() -> None:
    a = make_incomplete(title="")
    b = make_incomplete(title="")
    assert a.title == "Publication incomplète"
    assert a.title_fingerprint is None
    assert same_publication(a, b) is False


def test_same_publication_never_matches_generic_short_titles() -> None:
    a = make_source("https://a.example/x", title="Update")
    b = make_source("https://b.example/y", title="Update")
    assert a.title_fingerprint is None
    assert same_publication(a, b) is False


def test_deduplicate_sources_collapses_near_duplicate_urls_and_unions_warnings() -> None:
    first = make_source("https://mirror-one.example/story?ref=123", publisher="bne IntelliNews")
    first.parsing_warnings = ("first warning",)
    second = make_source(
        "https://mirror-two.example/story?session=abc", publisher="bne IntelliNews"
    )
    second.parsing_warnings = ("second warning",)

    deduped, remap = deduplicate_sources([first, second])

    assert len(deduped) == 1
    assert remap == {second.id: first.id}
    assert set(deduped[0].parsing_warnings) == {"first warning", "second warning"}


def test_deduplicate_incomplete_sources_collapses_repeated_no_url_citations() -> None:
    sources = [make_incomplete(local_ref=f"P{i}") for i in range(1, 12)]

    deduped = deduplicate_incomplete_sources(sources)

    assert len(deduped) == 1


def test_deduplicate_incomplete_sources_keeps_distinct_articles_separate() -> None:
    a = make_incomplete(title="First distinct article headline", publisher="Vendor A")
    b = make_incomplete(title="Second distinct article headline", publisher="Vendor B")

    deduped = deduplicate_incomplete_sources([a, b])

    assert len(deduped) == 2


def test_remap_ioc_publication_ids_follows_dropped_source_to_survivor() -> None:
    first = make_source("https://mirror-one.example/story?ref=123", publisher="bne IntelliNews")
    second = make_source(
        "https://mirror-two.example/story?session=abc", publisher="bne IntelliNews"
    )
    ioc = ProvisionalDiscoveryIoc(
        raw_value="1.2.3.4",
        normalized_value="1.2.3.4",
        declared_type="ipv4",
        proposed_type=DiscoveryIocType.IPV4,
        publication_relations=(
            ProvisionalIocPublicationRelation(
                publication_id=second.id,
                publication_ref="P2",
                raw_value="1.2.3.4",
                markdown_block="### publication P2\nioc: 1.2.3.4",
            ),
        ),
        model_run_id=None,
        markdown_block="### publication P2\nioc: 1.2.3.4",
    )

    _, remap = deduplicate_sources([first, second])
    remapped = remap_ioc_publication_ids([ioc], remap)

    assert remapped[0].publication_relations[0].publication_id == first.id


def test_recover_incomplete_source_urls_promotes_on_unambiguous_match() -> None:
    source = make_source("https://example.com/article", publisher="bne IntelliNews")
    incomplete = make_incomplete(publisher="bne IntelliNews")

    remaining = recover_incomplete_source_urls([source], [incomplete])

    assert remaining == []
    assert "url_recovered_from_local_match" in source.parsing_warnings


def test_recover_incomplete_source_urls_keeps_ambiguous_matches() -> None:
    first = make_source("https://a.example/story", publisher="bne IntelliNews")
    second = make_source("https://b.example/story", publisher="bne IntelliNews")
    incomplete = make_incomplete(publisher="bne IntelliNews")

    remaining = recover_incomplete_source_urls([first, second], [incomplete])

    assert remaining == [incomplete]
    assert "url_recovery_ambiguous" in incomplete.parsing_warnings
    assert "url_recovered_from_local_match" not in first.parsing_warnings
    assert "url_recovered_from_local_match" not in second.parsing_warnings


def test_recover_incomplete_source_urls_leaves_unmatched_alone() -> None:
    source = make_source("https://example.com/article", title="Completely unrelated headline")
    incomplete = make_incomplete()

    remaining = recover_incomplete_source_urls([source], [incomplete])

    assert remaining == [incomplete]
    assert incomplete.parsing_warnings == ()


def test_candidate_topic_construction_auto_recovers_matching_incomplete_source() -> None:
    """End-to-end: this is what actually runs when the parser builds a subject."""
    source = make_source("https://example.com/article", publisher="bne IntelliNews")
    incomplete = make_incomplete(publisher="bne IntelliNews")

    candidate = CandidateTopic(
        title="Subject title",
        summary="Summary",
        novelty="Novelty",
        technical_potential=2,
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
        incomplete_sources=[incomplete],
        tlp=TLP.AMBER,
        sensitivity="internal",
        external_llm_allowed=True,
    )

    assert candidate.incomplete_sources == []
    assert "url_recovered_from_local_match" in candidate.sources[0].parsing_warnings
