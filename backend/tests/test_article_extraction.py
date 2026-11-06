from __future__ import annotations

import base64
import json
from dataclasses import replace
from io import BytesIO

import pytest
from pypdf import PdfWriter

from cti_app.application import article_extraction as article_extraction_module
from cti_app.application.article_extraction import (
    ExtractionMethod,
    extract_candidates,
    extract_feed_candidates,
    extract_pdf_candidates,
    select_best_candidate,
)

ARTICLE_URL = "https://publisher.example/reports/sample-incident"
ARTICLE_TITLE = "Sample Incident Analysis"
PARAGRAPHS = (
    "The analysts examined a synthetic incident affecting several public test systems. "
    "Their report records the sequence of activity and the evidence available at each stage.",
    "The first observation describes an attachment delivered to a controlled laboratory inbox. "
    "Investigators then reviewed process creation and file access using offline fixtures.",
    "A later section explains how the sample attempted network communication with a reserved "
    "address. "
    "No connection was made during this test and every indicator below is fictional.",
    "The final section summarizes containment steps, detection ideas, and remaining uncertainty. "
    "Readers should compare every claim with the cited evidence before taking action.",
)
ARTICLE_TEXT = " ".join(PARAGRAPHS)
PDF_FIXTURE = base64.b64decode(
    "JVBERi0xLjQKMSAwIG9iago8PCAvVHlwZSAvQ2F0YWxvZyAvUGFnZXMgMiAwIFIgPj4KZW5kb2JqCjIgMCBvYmoK"
    "PDwgL1R5cGUgL1BhZ2VzIC9LaWRzIFszIDAgUl0gL0NvdW50IDEgPj4KZW5kb2JqCjMgMCBvYmoKPDwgL1R5cGUg"
    "L1BhZ2UgL1BhcmVudCAyIDAgUiAvTWVkaWFCb3ggWzAgMCA2MTIgNzkyXSAvUmVzb3VyY2VzIDw8IC9Gb250IDw8"
    "IC9GMSA0IDAgUiA+PiA+PiAvQ29udGVudHMgNSAwIFIgPj4KZW5kb2JqCjQgMCBvYmoKPDwgL1R5cGUgL0ZvbnQg"
    "L1N1YnR5cGUgL1R5cGUxIC9CYXNlRm9udCAvSGVsdmV0aWNhID4+CmVuZG9iago1IDAgb2JqCjw8IC9MZW5ndGgg"
    "OTc1ID4+CnN0cmVhbQpCVCAvRjEgMTAgVGYgNTAgNzUwIFRkIChTeW50aGV0aWMgaW5jaWRlbnQgcmVwb3J0LiBB"
    "bmFseXN0cyBvYnNlcnZlZCBFeGFtcGxlIG1hbHdhcmUgY29udGFjdGluZyBNaVhlRC5FeGFtcGxlLkNPTSBhdCAxOT"
    "IuMC4yLjQ0LiBTeW50aGV0aWMgaW5jaWRlbnQgcmVwb3J0LiBBbmFseXN0cyBvYnNlcnZlZCBFeGFtcGxlIG1hbHdh"
    "cmUgY29udGFjdGluZyBNaVhlRC5FeGFtcGxlLkNPTSBhdCAxOTIuMC4yLjQ0LiBTeW50aGV0aWMgaW5jaWRlbnQg"
    "cmVwb3J0LiBBbmFseXN0cyBvYnNlcnZlZCBFeGFtcGxlIG1hbHdhcmUgY29udGFjdGluZyBNaVhlRC5FeGFtcGxlLk"
    "NPTSBhdCAxOTIuMC4yLjQ0LiBTeW50aGV0aWMgaW5jaWRlbnQgcmVwb3J0LiBBbmFseXN0cyBvYnNlcnZlZCBFeGFt"
    "cGxlIG1hbHdhcmUgY29udGFjdGluZyBNaVhlRC5FeGFtcGxlLkNPTSBhdCAxOTIuMC4yLjQ0LiBTeW50aGV0aWMg"
    "aW5jaWRlbnQgcmVwb3J0LiBBbmFseXN0cyBvYnNlcnZlZCBFeGFtcGxlIG1hbHdhcmUgY29udGFjdGluZyBNaVhlRC"
    "5FeGFtcGxlLkNPTSBhdCAxOTIuMC4yLjQ0LiBTeW50aGV0aWMgaW5jaWRlbnQgcmVwb3J0LiBBbmFseXN0cyBvYnNl"
    "cnZlZCBFeGFtcGxlIG1hbHdhcmUgY29udGFjdGluZyBNaVhlRC5FeGFtcGxlLkNPTSBhdCAxOTIuMC4yLjQ0LiBT"
    "eW50aGV0aWMgaW5jaWRlbnQgcmVwb3J0LiBBbmFseXN0cyBvYnNlcnZlZCBFeGFtcGxlIG1hbHdhcmUgY29udGFj"
    "dGluZyBNaVhlRC5FeGFtcGxlLkNPTSBhdCAxOTIuMC4yLjQ0LiBTeW50aGV0aWMgaW5jaWRlbnQgcmVwb3J0LiBB"
    "bmFseXN0cyBvYnNlcnZlZCBFeGFtcGxlIG1hbHdhcmUgY29udGFjdGluZyBNaVhlRC5FeGFtcGxlLkNPTSBhdCAx"
    "OTIuMC4yLjQ0LiBTeW50aGV0aWMgaW5jaWRlbnQgcmVwb3J0LiBBbmFseXN0cyBvYnNlcnZlZCBFeGFtcGxlIG1h"
    "bHdhcmUgY29udGFjdGluZyBNaVhlRC5FeGFtcGxlLkNPTSBhdCAxOTIuMC4yLjQ0LikgVGogRVQKZW5kc3RyZWFt"
    "CmVuZG9iagp4cmVmCjAgNgowMDAwMDAwMDAwIDY1NTM1IGYgCjAwMDAwMDAwMDkgMDAwMDAgbiAKMDAwMDAwMDA1"
    "OCAwMDAwMCBuIAowMDAwMDAwMTE1IDAwMDAwIG4gCjAwMDAwMDAyNDEgMDAwMDAgbiAKMDAwMDAwMDMxMSAwMDAw"
    "MCBuIAp0cmFpbGVyCjw8IC9TaXplIDYgL1Jvb3QgMSAwIFIgPj4Kc3RhcnR4cmVmCjEzMzcKJSVFT0YK"
)


def _paragraph_markup(paragraphs: tuple[str, ...] = PARAGRAPHS) -> str:
    return "".join(f"<p>{paragraph}</p>" for paragraph in paragraphs)


def _html(body: str, title: str = ARTICLE_TITLE) -> bytes:
    return (
        f"<html><head><title>{title} | Example</title>"
        f'<meta property="og:title" content="{title}">'
        f'<meta property="og:site_name" content="Synthetic Publisher">'
        f'<link rel="canonical" href="{ARTICLE_URL}"></head><body>{body}</body></html>'
    ).encode()


def _jsonld_script(*, article_type: str = "NewsArticle", title: str = ARTICLE_TITLE) -> bytes:
    value = {
        "@context": "https://schema.org",
        "@type": article_type,
        "headline": title,
        "articleBody": ARTICLE_TEXT,
        "datePublished": "2025-02-03T12:30:00Z",
        "author": {"name": "Synthetic Author"},
        "publisher": {"name": "Synthetic Publisher"},
        "mainEntityOfPage": {"@id": ARTICLE_URL},
    }
    return f'<script type="application/ld+json">{json.dumps(value)}</script>'.encode()


def _extract(
    body: bytes, *, content_type: str = "text/html", title: str | None = ARTICLE_TITLE, **kwargs
):
    return extract_candidates(
        body,
        content_type=content_type,
        url=ARTICLE_URL,
        expected_title=title,
        **kwargs,
    )


def test_html_article_ignores_navigation_and_prefers_article() -> None:
    source = _html(
        '<nav><a href="/">' + ("navigation link " * 80) + "</a></nav>"
        "<article><h1>Sample Incident Analysis</h1>" + _paragraph_markup() + "</article>"
        "<footer>subscribe to continue</footer>"
    )

    candidates = _extract(source)

    article = next(item for item in candidates if item.method is ExtractionMethod.HTML_ARTICLE)
    assert article.quality.text_chars >= 200
    assert "navigation link" not in article.text
    assert "subscribe to continue" not in article.text
    assert article.publisher == "Synthetic Publisher"


def test_html_malformed_markup_is_handled_without_crashing() -> None:
    source = b"<html><title>Sample Incident Analysis</title><body><article>" + ARTICLE_TEXT.encode()

    candidates = _extract(source)

    assert any(item.method is ExtractionMethod.HTML_ARTICLE for item in candidates)


def test_html_script_style_and_template_content_are_removed() -> None:
    source = _html(
        "<article>" + _paragraph_markup() + "</article>"
        "<script>SCRIPT_ONLY_MARKER</script><style>STYLE_ONLY_MARKER</style>"
        "<template>TEMPLATE_ONLY_MARKER</template>"
    )

    candidates = _extract(source)

    assert candidates
    assert all(
        marker not in item.text
        for item in candidates
        for marker in ("SCRIPT_ONLY_MARKER", "STYLE_ONLY_MARKER", "TEMPLATE_ONLY_MARKER")
    )


def test_json_ld_article_fields_are_extracted() -> None:
    candidates = _extract(_jsonld_script(), content_type="text/html")

    candidate = next(item for item in candidates if item.method is ExtractionMethod.JSON_LD)
    assert candidate.text == ARTICLE_TEXT
    assert candidate.title == ARTICLE_TITLE
    assert candidate.publisher == "Synthetic Publisher"
    assert candidate.published_at.isoformat() == "2025-02-03"
    assert ("author", "Synthetic Author") in candidate.metadata


def test_json_ld_graph_and_array_are_traversed() -> None:
    article = json.loads(
        _jsonld_script()
        .decode()
        .removeprefix('<script type="application/ld+json">')
        .removesuffix("</script>")
    )
    graph = [{"@type": "Organization", "name": "Synthetic Publisher"}, article]
    source = (
        f'<script type="application/ld+json">{json.dumps([{"@graph": graph}])}</script>'.encode()
    )

    candidates = _extract(source)

    assert len([item for item in candidates if item.method is ExtractionMethod.JSON_LD]) == 1


def test_json_ld_uses_is_based_on_as_url_fallback() -> None:
    article = {
        "@type": "https://schema.org/Article",
        "headline": ARTICLE_TITLE,
        "articleBody": ARTICLE_TEXT,
        "isBasedOn": {"@id": ARTICLE_URL},
    }
    source = f'<script type="application/ld+json">{json.dumps(article)}</script>'.encode()

    candidates = _extract(source)

    candidate = next(item for item in candidates if item.method is ExtractionMethod.JSON_LD)
    assert candidate.canonical_url == ARTICLE_URL


def test_non_article_json_ld_type_is_rejected() -> None:
    candidates = _extract(_jsonld_script(article_type="Product"))

    assert not any(item.method is ExtractionMethod.JSON_LD for item in candidates)


def test_next_data_embedded_article_is_extracted() -> None:
    state = {
        "props": {
            "pageProps": {
                "article": {"title": ARTICLE_TITLE, "url": ARTICLE_URL, "content": ARTICLE_TEXT}
            }
        }
    }
    source = _html(
        f'<script id="__NEXT_DATA__">{json.dumps(state)}</script>'
        f"<article>{_paragraph_markup()}</article>"
    )

    candidates = _extract(source)

    embedded = next(item for item in candidates if item.method is ExtractionMethod.EMBEDDED_STATE)
    assert embedded.text == ARTICLE_TEXT
    assert embedded.canonical_url == ARTICLE_URL


def test_embedded_state_with_wrong_canonical_url_is_marked_low_quality() -> None:
    state = {
        "article": {
            "title": ARTICLE_TITLE,
            "url": "https://other.example/reports/sample-incident",
            "content": ARTICLE_TEXT,
        }
    }
    candidates = _extract(f'<script id="__NEXT_DATA__">{json.dumps(state)}</script>'.encode())
    candidate = next(item for item in candidates if item.method is ExtractionMethod.EMBEDDED_STATE)

    assert "canonical_host_mismatch" in candidate.reasons
    assert candidate.quality.score < 0.55
    assert (
        select_best_candidate(
            (candidate,), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )
    assert (
        select_best_candidate(
            (candidate,),
            expected_title=ARTICLE_TITLE,
            expected_url=ARTICLE_URL,
            allow_cross_domain=True,
        )
        is candidate
    )


def test_nuxt_assignment_state_is_extracted() -> None:
    state = {
        "article": {
            "headline": ARTICLE_TITLE,
            "canonicalUrl": ARTICLE_URL,
            "articleBody": ARTICLE_TEXT,
        }
    }
    source = _html(
        f"<article>{_paragraph_markup()}</article>"
        f"<script>window.__NUXT__ = {json.dumps(state)};</script>"
    )

    candidates = _extract(source)

    assert any(item.method is ExtractionMethod.EMBEDDED_STATE for item in candidates)


def test_embedded_state_requires_title_or_url_coherence() -> None:
    state = {"article": {"title": "Unrelated local story", "content": ARTICLE_TEXT}}
    source = _html(f'<script id="__NEXT_DATA__">{json.dumps(state)}</script>')

    candidates = _extract(source)

    assert not any(item.method is ExtractionMethod.EMBEDDED_STATE for item in candidates)


def test_rss_returns_only_matching_full_text_item() -> None:
    xml = (
        f'<rss version="2.0"><channel><title>Example feed</title>'
        f"<item><title>Other report</title><link>https://publisher.example/other</link>"
        f"<description>{ARTICLE_TEXT}</description></item>"
        f"<item><title>{ARTICLE_TITLE}</title><link>{ARTICLE_URL}</link>"
        '<content:encoded xmlns:content="http://purl.org/rss/1.0/modules/content/">'
        f"<![CDATA[<p>{ARTICLE_TEXT}</p>]]></content:encoded>"
        "<pubDate>Mon, 03 Feb 2025 12:30:00 GMT</pubDate></item></channel></rss>"
    ).encode()

    candidates = extract_feed_candidates(xml, url=ARTICLE_URL, expected_title=ARTICLE_TITLE)

    assert len(candidates) == 1
    assert candidates[0].method is ExtractionMethod.RSS_ATOM
    assert candidates[0].canonical_url == ARTICLE_URL
    assert candidates[0].published_at.isoformat() == "2025-02-03"


def test_atom_feed_matches_requested_entry() -> None:
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>Example feed</title>'
        f"<entry><title>{ARTICLE_TITLE}</title>"
        f'<link rel="alternate" href="{ARTICLE_URL}"/>'
        '<updated>2025-02-03T12:30:00Z</updated><content type="html">'
        f"<![CDATA[<p>{ARTICLE_TEXT}</p>]]></content></entry></feed>"
    ).encode()

    candidates = extract_feed_candidates(xml, url=ARTICLE_URL, expected_title=ARTICLE_TITLE)

    assert len(candidates) == 1
    assert candidates[0].text == ARTICLE_TEXT


def test_atom_ignores_self_link_before_alternate_link() -> None:
    xml = (
        '<feed xmlns="http://www.w3.org/2005/Atom"><title>Example feed</title>'
        f"<entry><title>{ARTICLE_TITLE}</title>"
        '<link rel="self" type="application/atom+xml" href="/feed/entry/42"/>'
        f'<link rel="alternate" type="text/html" href="{ARTICLE_URL}"/>'
        '<content type="html">'
        f"<![CDATA[<p>{ARTICLE_TEXT}</p>]]></content></entry></feed>"
    ).encode()

    candidates = extract_feed_candidates(xml, url=ARTICLE_URL, expected_title=ARTICLE_TITLE)

    assert len(candidates) == 1
    assert candidates[0].canonical_url == ARTICLE_URL


def test_json_feed_returns_matching_item_only() -> None:
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "title": "Synthetic Publisher",
        "items": [
            {
                "title": "Other report",
                "url": "https://publisher.example/other",
                "content_text": ARTICLE_TEXT,
            },
            {
                "title": ARTICLE_TITLE,
                "url": ARTICLE_URL,
                "content_text": ARTICLE_TEXT,
                "date_published": "2025-02-03T10:00:00Z",
            },
        ],
    }

    candidates = extract_feed_candidates(
        json.dumps(feed).encode(), url=ARTICLE_URL, expected_title=ARTICLE_TITLE
    )

    assert len(candidates) == 1
    assert candidates[0].method is ExtractionMethod.JSON_FEED
    assert candidates[0].published_at.isoformat() == "2025-02-03"


def test_expected_language_is_applied_to_json_feed_candidates() -> None:
    feed = {
        "version": "https://jsonfeed.org/version/1.1",
        "items": [
            {
                "title": ARTICLE_TITLE,
                "url": ARTICLE_URL,
                "content_text": ARTICLE_TEXT,
            }
        ],
    }

    candidates = _extract(
        json.dumps(feed).encode(),
        content_type="application/feed+json",
        expected_languages=("fr",),
    )

    assert candidates[0].quality.language == "en"
    assert "language_mismatch" in candidates[0].reasons


def test_wordpress_rest_rendered_post_is_extracted() -> None:
    post = {
        "date": "2025-02-03T12:30:00",
        "link": ARTICLE_URL,
        "title": {"rendered": ARTICLE_TITLE},
        "content": {"rendered": _paragraph_markup()},
    }

    candidates = _extract(json.dumps(post).encode(), content_type="application/json")

    candidate = next(item for item in candidates if item.method is ExtractionMethod.CMS_JSON)
    assert candidate.text == ARTICLE_TEXT
    assert candidate.published_at.isoformat() == "2025-02-03"


def test_oembed_html_and_title_are_extracted() -> None:
    response = {
        "type": "rich",
        "title": ARTICLE_TITLE,
        "html": f"<blockquote>{_paragraph_markup()}</blockquote>",
    }

    candidates = _extract(json.dumps(response).encode(), content_type="application/json")

    candidate = next(item for item in candidates if item.method is ExtractionMethod.OEMBED)
    assert candidate.text == ARTICLE_TEXT


def test_generic_article_json_requires_coherent_title() -> None:
    response = {"title": ARTICLE_TITLE, "articleBody": ARTICLE_TEXT, "url": ARTICLE_URL}

    candidates = _extract(json.dumps(response).encode(), content_type="application/json")

    assert any(item.method is ExtractionMethod.CMS_JSON for item in candidates)


@pytest.mark.parametrize(
    ("method", "body"),
    [
        (
            ExtractionMethod.JSON_LD,
            lambda wrong_url: (
                '<script type="application/ld+json">'
                + json.dumps(
                    {
                        "@type": "NewsArticle",
                        "headline": ARTICLE_TITLE,
                        "articleBody": ARTICLE_TEXT,
                        "mainEntityOfPage": {"@id": wrong_url},
                    }
                )
                + "</script>"
            ).encode(),
        ),
        (
            ExtractionMethod.CMS_JSON,
            lambda wrong_url: json.dumps(
                {
                    "link": wrong_url,
                    "title": {"rendered": ARTICLE_TITLE},
                    "content": {"rendered": f"<p>{ARTICLE_TEXT}</p>"},
                }
            ).encode(),
        ),
    ],
)
def test_structured_candidate_with_wrong_canonical_url_is_low_quality(method, body) -> None:
    wrong_url = "https://other.example/reports/sample-incident"
    content_type = "text/html" if method is ExtractionMethod.JSON_LD else "application/json"
    candidates = _extract(body(wrong_url), content_type=content_type)
    candidate = next(item for item in candidates if item.method is method)

    assert "canonical_host_mismatch" in candidate.reasons
    assert candidate.quality.score < 0.55
    assert (
        select_best_candidate(
            (candidate,), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_json_ld_without_canonical_requires_strong_body_coherence() -> None:
    payload = {
        "@type": "NewsArticle",
        "headline": ARTICLE_TITLE,
        "articleBody": ARTICLE_TEXT,
    }
    candidates = _extract(
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'.encode()
    )
    candidate = next(item for item in candidates if item.method is ExtractionMethod.JSON_LD)

    assert candidate.canonical_url is None
    assert (
        select_best_candidate(
            (candidate,), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is candidate
    )

    payload["articleBody"] = "An unrelated local report describes a different event. " * 30
    weak_candidates = _extract(
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'.encode()
    )
    weak_candidate = next(
        item for item in weak_candidates if item.method is ExtractionMethod.JSON_LD
    )
    assert (
        select_best_candidate(
            (weak_candidate,), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_same_host_but_different_structured_canonical_is_low_quality() -> None:
    payload = {
        "@type": "NewsArticle",
        "headline": ARTICLE_TITLE,
        "articleBody": ARTICLE_TEXT,
        "url": "https://publisher.example/reports/another-incident",
    }
    candidates = _extract(
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'.encode()
    )
    candidate = next(item for item in candidates if item.method is ExtractionMethod.JSON_LD)

    assert candidate.canonical_url == payload["url"]
    assert "canonical_url_mismatch" in candidate.reasons
    assert candidate.quality.score < 0.55
    assert (
        select_best_candidate(
            (candidate,), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_same_host_different_path_canonical_is_rejected_despite_matching_article() -> None:
    candidates = _extract(
        _html("<article><h1>" + ARTICLE_TITLE + "</h1>" + _paragraph_markup() + "</article>")
    )
    wrong_path = tuple(
        replace(item, canonical_url="https://publisher.example/reports/another-incident")
        for item in candidates
    )

    assert all(item.title == ARTICLE_TITLE for item in wrong_path)
    assert all(item.quality.text_chars >= 200 for item in wrong_path)
    assert (
        select_best_candidate(
            wrong_path, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_same_article_slug_path_variant_requires_strong_coherence() -> None:
    payload = {
        "@type": "NewsArticle",
        "headline": ARTICLE_TITLE,
        "articleBody": ARTICLE_TEXT,
        "url": "https://publisher.example/articles/sample-incident",
    }
    candidates = _extract(
        f'<script type="application/ld+json">{json.dumps(payload)}</script>'.encode()
    )
    candidate = next(item for item in candidates if item.method is ExtractionMethod.JSON_LD)

    assert (
        select_best_candidate(
            (candidate,), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is candidate
    )


def test_unrelated_generic_json_payload_is_rejected() -> None:
    response = {
        "headline": "Unrelated local story",
        "body": ARTICLE_TEXT,
        "url": "https://other.example/story",
    }

    candidates = _extract(json.dumps(response).encode(), content_type="application/json")

    assert candidates == ()


def test_wrong_title_candidate_is_not_selected() -> None:
    candidates = _extract(
        _html("<article>" + _paragraph_markup() + "</article>", title="Unrelated local story")
    )

    assert candidates
    assert (
        select_best_candidate(
            candidates, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_matching_metadata_title_with_unrelated_body_is_not_selected() -> None:
    unrelated = "The unrelated local report reviews a different event and its findings. " * 40
    candidates = _extract(
        _html(f"<article><h1>{ARTICLE_TITLE}</h1><p>{unrelated}</p></article>")
    )

    assert candidates
    assert all(item.title == ARTICLE_TITLE for item in candidates)
    assert (
        select_best_candidate(
            candidates, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_expected_url_rejects_same_title_from_another_host() -> None:
    candidates = _extract(
        _html("<article><h1>" + ARTICLE_TITLE + "</h1>" + _paragraph_markup() + "</article>")
    )
    cross_domain = tuple(
        replace(item, canonical_url="https://other.example/reports/sample-incident")
        for item in candidates
    )

    assert (
        select_best_candidate(
            cross_domain, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )
    assert (
        select_best_candidate(
            cross_domain,
            expected_title=ARTICLE_TITLE,
            expected_url=ARTICLE_URL,
            allow_cross_domain=True,
        )
        is not None
    )


def test_same_title_other_host_with_long_unrelated_body_is_not_selected() -> None:
    unrelated = "The unrelated local report reviews a different event and its findings. " * 40
    candidates = _extract(_html(f"<article><p>{unrelated}</p></article>"))
    cross_domain = tuple(
        replace(item, canonical_url="https://other.example/reports/sample-incident")
        for item in candidates
    )

    assert (
        select_best_candidate(
            cross_domain, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_expected_url_accepts_same_host_canonical_candidate() -> None:
    candidates = _extract(
        _html("<article><h1>" + ARTICLE_TITLE + "</h1>" + _paragraph_markup() + "</article>")
    )

    assert (
        select_best_candidate(
            candidates, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is not None
    )


@pytest.mark.parametrize(
    "canonical_url",
    [
        "https://www.publisher.example/reports/sample-incident/?source=feed",
        "https://publisher.example/reports/sample-incident?source=feed",
    ],
)
def test_expected_url_accepts_www_trailing_slash_and_query_variants(canonical_url: str) -> None:
    candidates = _extract(
        _html("<article><h1>" + ARTICLE_TITLE + "</h1>" + _paragraph_markup() + "</article>")
    )
    equivalent = tuple(replace(item, canonical_url=canonical_url) for item in candidates)

    assert (
        select_best_candidate(
            equivalent, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is not None
    )


def test_expected_url_is_required_to_select_candidates() -> None:
    candidates = _extract(
        _html("<article><h1>" + ARTICLE_TITLE + "</h1>" + _paragraph_markup() + "</article>")
    )

    with pytest.raises(TypeError):
        select_best_candidate(candidates, expected_title=ARTICLE_TITLE)  # type: ignore[call-arg]


def test_repeated_state_assignments_are_bounded() -> None:
    state = {
        "article": {"title": ARTICLE_TITLE, "url": ARTICLE_URL, "content": ARTICLE_TEXT}
    }
    assignment = f"window.__NUXT__ = {json.dumps(state)};"
    source = assignment * 1_000

    matches = article_extraction_module._state_assignments(source)
    assert iter(matches) is matches
    found = list(matches)

    assert len(found) <= 8
    assert sum(map(len, found)) <= 2 * 1024 * 1024
    candidates = _extract(_html(f"<script>{source}</script>"))
    assert len([item for item in candidates if item.method is ExtractionMethod.EMBEDDED_STATE]) <= 3


def test_aggregate_embedded_state_character_cap_is_observable() -> None:
    state = {
        "article": {"title": ARTICLE_TITLE, "url": ARTICLE_URL, "content": ARTICLE_TEXT},
        "padding": "x" * 300_000,
    }
    script = f'<script id="__NEXT_DATA__">{json.dumps(state)}</script>'
    source = _html(script * 8).decode()
    parser = article_extraction_module._SafeHTMLParser(ARTICLE_URL)
    parser.feed(source)
    parser.close()
    accepted = [
        text
        for text, attrs in parser.scripts
        if attrs.get("id") == "__NEXT_DATA__"
    ]
    accepted_chars = sum(map(len, accepted))

    assert len(accepted) < 8
    assert accepted_chars <= article_extraction_module._MAX_STATE_JSON_CHARS
    assert accepted_chars < len(script) * 8

    candidates = _extract(source.encode())
    state_candidates = [
        item for item in candidates if item.method is ExtractionMethod.EMBEDDED_STATE
    ]
    assert len(state_candidates) <= 3


@pytest.mark.parametrize(
    ("cap_name", "cap_value", "payload"),
    [
        (
            "_MAX_JSON_NODES",
            4,
            {"items": [{"noise": index} for index in range(8)] + [{"headline": ARTICLE_TITLE}]},
        ),
        (
            "_MAX_JSON_DEPTH",
            1,
            {"items": [{"item": {"headline": ARTICLE_TITLE, "articleBody": ARTICLE_TEXT}}]},
        ),
    ],
)
def test_generic_json_node_and_depth_caps_bound_traversal(
    monkeypatch, cap_name: str, cap_value: int, payload: object
) -> None:
    monkeypatch.setattr(article_extraction_module, cap_name, cap_value)

    candidates = article_extraction_module._generic_json_candidates(
        payload, ARTICLE_URL, ARTICLE_TITLE, (), 200
    )

    assert candidates == []


def test_duplicate_json_ld_candidates_are_deduplicated() -> None:
    duplicate = _jsonld_script().decode()

    candidates = _extract((duplicate + duplicate).encode())

    json_ld_candidates = [item for item in candidates if item.method is ExtractionMethod.JSON_LD]
    assert len(json_ld_candidates) == 1


def test_deeply_nested_json_ld_url_does_not_recurse() -> None:
    nested_url: object = {"@id": ARTICLE_URL}
    for _ in range(200):
        nested_url = {"mainEntityOfPage": nested_url}
    article = {
        "@type": "NewsArticle",
        "headline": ARTICLE_TITLE,
        "articleBody": ARTICLE_TEXT,
        "mainEntityOfPage": nested_url,
    }

    candidates = _extract(
        f'<script type="application/ld+json">{json.dumps(article)}</script>'.encode()
    )
    candidate = next(item for item in candidates if item.method is ExtractionMethod.JSON_LD)

    assert candidate.canonical_url is None


def test_deep_json_input_is_rejected_without_recursion_error() -> None:
    deeply_nested = ("[" * 1_200) + "0" + ("]" * 1_200)

    assert _extract(deeply_nested.encode(), content_type="application/json") == ()


def test_huge_input_is_rejected_before_decoding() -> None:
    huge = b"x" * (25 * 1024 * 1024 + 1)

    assert _extract(huge, content_type="text/plain") == ()


@pytest.mark.parametrize(
    ("marker", "quality_flag"),
    [
        ("CAPTCHA: verify you are human", "challenge_detected"),
        ("Subscribe to continue reading", "paywall_detected"),
    ],
)
def test_challenge_and_paywall_candidates_are_rejected(marker: str, quality_flag: str) -> None:
    source = _html("<article>" + _paragraph_markup() + f"<p>{marker}</p></article>")
    candidates = _extract(source)

    assert candidates
    assert getattr(candidates[0].quality, quality_flag)
    assert (
        select_best_candidate(
            candidates, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_duplicate_paragraphs_are_penalized_and_rejected() -> None:
    repeated = (
        "The repeated synthetic report describes a controlled laboratory observation. " * 5
    ).strip()
    source = _html(
        f"<article><h1>{ARTICLE_TITLE}</h1><p>{repeated}</p><p>{repeated}</p><p>{repeated}</p></article>"
    )

    candidates = _extract(source)
    candidate = next(item for item in candidates if item.method is ExtractionMethod.HTML_ARTICLE)

    assert candidate.quality.duplicate_ratio >= 0.5
    assert candidate.quality.score < 0.55


def test_truncation_marker_is_reported() -> None:
    source = _html(
        "<article><h1>" + ARTICLE_TITLE + "</h1><p>" + ARTICLE_TEXT + " ...</p></article>"
    )

    candidate = next(
        item for item in _extract(source) if item.method is ExtractionMethod.HTML_ARTICLE
    )

    assert candidate.quality.truncated
    assert "possibly_truncated" in candidate.reasons


def test_language_mismatch_is_reported_and_penalizes_score() -> None:
    candidates = _extract(
        _html("<article><h1>" + ARTICLE_TITLE + "</h1>" + _paragraph_markup() + "</article>"),
        expected_languages=("fr",),
    )

    candidate = next(item for item in candidates if item.method is ExtractionMethod.HTML_ARTICLE)

    assert candidate.quality.language == "en"
    assert "language_mismatch" in candidate.reasons


def test_single_word_never_determines_language() -> None:
    source = _html(
        "<article><h1>"
        + ARTICLE_TITLE
        + "</h1><p>the "
        + ("synthetic observation " * 100)
        + "</p></article>"
    )

    candidate = next(
        item for item in _extract(source) if item.method is ExtractionMethod.HTML_ARTICLE
    )

    assert candidate.quality.language is None


@pytest.mark.parametrize(
    "indicator",
    [
        "MiXeD.Example.COM",
        "192.0.2.44",
        "SHA256: AbCdEf0123456789",
        "[MiXeD.Example.COM]",
    ],
)
def test_ioc_literals_keep_exact_case_and_punctuation(indicator: str) -> None:
    source = _html(f"<article><h1>{ARTICLE_TITLE}</h1><p>{ARTICLE_TEXT} {indicator}</p></article>")

    candidate = next(
        item for item in _extract(source) if item.method is ExtractionMethod.HTML_ARTICLE
    )

    assert indicator in candidate.text


def test_html_entities_are_decoded_once_without_changing_ioc_literals() -> None:
    source = _html(
        "<article><h1>"
        + ARTICLE_TITLE
        + "</h1><p>"
        + ARTICLE_TEXT
        + " MiXeD[.]Example.COM &amp; keep &amp;lt;literal&amp;gt;</p></article>"
    )

    candidate = next(
        item for item in _extract(source) if item.method is ExtractionMethod.HTML_ARTICLE
    )

    assert "MiXeD[.]Example.COM" in candidate.text
    assert "& keep &lt;literal&gt;" in candidate.text


def test_malformed_json_returns_no_candidates() -> None:
    assert _extract(b'{"title":', content_type="application/json") == ()


@pytest.mark.parametrize(
    "source",
    [
        b"<rss><channel><item>",
        b"<!DOCTYPE rss [<!ENTITY x 'boom'>]><rss/>",
        b'<!DOCTYPE rss [<!ENTITY x "expanded payload">]><rss>'
        b"<channel><title>&x;</title></channel></rss>",
    ],
)
def test_malformed_or_entity_xml_is_rejected(source: bytes) -> None:
    assert extract_feed_candidates(source, url=ARTICLE_URL, expected_title=ARTICLE_TITLE) == ()


def test_pdf_static_fixture_extracts_text_and_metadata() -> None:
    candidates = extract_pdf_candidates(
        PDF_FIXTURE, url=ARTICLE_URL, expected_title="Synthetic incident report"
    )

    assert len(candidates) == 1
    assert candidates[0].method is ExtractionMethod.PDF_TEXT
    assert "MiXeD.Example.COM" in candidates[0].text
    assert candidates[0].quality.text_chars >= 200


def test_expected_language_is_applied_to_pdf_candidates() -> None:
    candidates = extract_candidates(
        PDF_FIXTURE,
        content_type="application/pdf",
        url=ARTICLE_URL,
        expected_title="Synthetic incident report",
        expected_languages=("fr",),
    )

    assert candidates[0].quality.language == "en"
    assert "language_mismatch" in candidates[0].reasons


def test_malformed_pdf_returns_no_candidate() -> None:
    assert (
        extract_pdf_candidates(
            b"%PDF-1.4\nnot a document", url=ARTICLE_URL, expected_title=ARTICLE_TITLE
        )
        == ()
    )


def test_encrypted_pdf_returns_no_candidate() -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    writer.encrypt("offline-test-password")
    output = BytesIO()
    writer.write(output)

    assert (
        extract_pdf_candidates(
            output.getvalue(), url=ARTICLE_URL, expected_title=ARTICLE_TITLE
        )
        == ()
    )


def test_oversized_pdf_is_rejected_without_starting_parser(monkeypatch) -> None:
    def unexpected_parser_call(*args, **kwargs):
        pytest.fail("oversized PDF reached the parser")

    monkeypatch.setattr(article_extraction_module, "parse_document", unexpected_parser_call)
    oversized = b"%PDF-" + (b"x" * (25 * 1024 * 1024))

    assert extract_pdf_candidates(oversized, url=ARTICLE_URL, expected_title=ARTICLE_TITLE) == ()


def test_pdf_expansion_is_limited_by_isolated_parser(monkeypatch) -> None:
    monkeypatch.setattr(article_extraction_module, "_MAX_PDF_TEXT_CHARS", 100)

    assert extract_pdf_candidates(PDF_FIXTURE, url=ARTICLE_URL, expected_title=ARTICLE_TITLE) == ()


def test_excessive_boilerplate_is_penalized() -> None:
    links = "".join(f'<a href="/{index}">navigation item {index}</a>' for index in range(80))
    source = _html(f"<article><h1>{ARTICLE_TITLE}</h1>{links}</article>")

    candidate = next(
        item for item in _extract(source) if item.method is ExtractionMethod.HTML_ARTICLE
    )

    assert candidate.quality.boilerplate_ratio > 0.6
    assert candidate.quality.score < 0.55


def test_long_repetitive_region_block_page_is_rejected() -> None:
    notice = "This content is unavailable in your country. Access is denied. "
    repeated_notice = "".join(f"<p>{notice}</p>" for _ in range(40))
    candidates = _extract(_html(repeated_notice))

    assert candidates
    assert any("error_page_detected" in item.reasons for item in candidates)
    assert (
        select_best_candidate(
            candidates, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_long_varied_region_block_notice_is_rejected() -> None:
    details = [
        "Readers in the northern district receive this notice under the publisher's "
        "licensing rules.",
        "The regional catalog does not include this report for accounts registered overseas.",
        "Local access settings were updated after the distribution agreement changed "
        "this morning.",
        "The publisher directs visitors to the regional edition for material available "
        "in that area.",
        "This restriction applies to browser requests and cached copies served from the "
        "same market.",
        "Support staff can explain which editions are available under each territory's "
        "current terms.",
        "The notice remains visible while the publisher reviews its distribution configuration.",
        "Visitors can return later to check whether the regional publication settings "
        "have changed.",
    ]
    paragraphs = [
        f"This content is unavailable in your country. {detail} "
        f"Request reference region {index} is included for support follow-up."
        for index, detail in enumerate(details)
    ]
    candidates = _extract(_html("".join(f"<p>{item}</p>" for item in paragraphs)))

    assert candidates
    assert all(item.title == ARTICLE_TITLE for item in candidates)
    assert all(item.quality.text_chars > 1_200 for item in candidates)
    assert all(item.quality.duplicate_ratio < 0.35 for item in candidates)
    assert any("error_page_detected" in item.reasons for item in candidates)
    assert (
        select_best_candidate(
            candidates, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is None
    )


def test_genuine_article_is_not_rejected_for_mentioning_access_denied() -> None:
    paragraphs = (
        ARTICLE_TEXT,
        "The appendix describes access denied notices used by regional filters.",
    )
    candidates = _extract(
        _html(
            "<article><h1>"
            + ARTICLE_TITLE
            + "</h1>"
            + _paragraph_markup(paragraphs)
            + "</article>"
        )
    )
    article = next(item for item in candidates if item.method is ExtractionMethod.HTML_ARTICLE)

    assert "error_page_detected" not in article.reasons
    assert not article.quality.challenge_detected
    assert (
        select_best_candidate(
            (article,), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is article
    )


def test_plain_text_ocr_candidate_is_supported() -> None:
    ocr_text = f"{ARTICLE_TITLE}\n{ARTICLE_TEXT}"

    candidates = _extract(ocr_text.encode(), content_type="text/plain")

    assert len(candidates) == 1
    assert candidates[0].method is ExtractionMethod.PLAIN_TEXT
    assert candidates[0].title == ARTICLE_TITLE
    assert (
        select_best_candidate(
            candidates, expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is candidates[0]
    )


def test_tie_break_uses_stable_method_order() -> None:
    from dataclasses import replace

    candidates = _extract(
        _html("<article><h1>" + ARTICLE_TITLE + "</h1>" + _paragraph_markup() + "</article>")
    )
    generic = next(item for item in candidates if item.method is ExtractionMethod.HTML_GENERIC)
    article = replace(
        next(item for item in candidates if item.method is ExtractionMethod.HTML_ARTICLE),
        quality=generic.quality,
    )

    assert (
        select_best_candidate(
            (article, generic), expected_title=ARTICLE_TITLE, expected_url=ARTICLE_URL
        )
        is generic
    )
