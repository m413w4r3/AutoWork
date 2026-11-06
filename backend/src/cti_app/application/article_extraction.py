from __future__ import annotations

import difflib
import email.utils
import html
import json
import re
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any
from urllib.parse import urljoin, urlsplit, urlunsplit

from pypdf import PdfReader

from cti_app.domain.acquisition import AcquisitionQuality

_MAX_INPUT_BYTES = 25 * 1024 * 1024
_MAX_HTML_CHARS = 8_000_000
_MAX_JSON_CHARS = 2_000_000
_MAX_JSON_NODES = 20_000
_MAX_HTML_NODES = 100_000
_MAX_DEPTH = 128
_MAX_PDF_TEXT_CHARS = 2_000_000
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_TAG_RE = re.compile(r"(?s)<[^>]*>")
_HTML_TAG_RE = re.compile(
    r"(?is)<\s*/?\s*(?:a|article|aside|b|blockquote|br|code|dd|div|dl|dt|em|figcaption|figure|form|footer|h[1-6]|header|hr|i|li|main|nav|noscript|ol|p|pre|script|section|small|span|strong|style|sub|sup|svg|table|td|template|th|tr|u|ul)\b"
)
_CHALLENGE_RE = re.compile(
    r"(?i)\b(?:captcha|verify you are human|prove you are human|checking your browser|"
    r"unusual traffic|access denied|enable javascript and cookies|log in to continue|"
    r"sign in to continue)\b"
)
_PAYWALL_RE = re.compile(
    r"(?i)\b(?:paywall|subscription required|subscribe to continue|subscribe to read|"
    r"subscribe now to continue|to read the full article.{0,50}subscribe)\b"
)
_TRUNCATION_RE = re.compile(r"(?i)(?:\.\.\.|…|\[\s*truncated\s*\]|\[\.\.\.\])\s*$")
_DANGLING_ENDINGS = {
    "a",
    "an",
    "and",
    "as",
    "at",
    "because",
    "but",
    "by",
    "for",
    "from",
    "in",
    "into",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "which",
    "that",
    "et",
    "ou",
    "mais",
    "dans",
    "avec",
    "pour",
    "sur",
    "qui",
    "que",
    "un",
    "une",
    "des",
}
_EN_STOPWORDS = frozenset(
    "a an and are as at be been but by can could did do does for from had has have he her "
    "here hers him his how i if in into is it its may more most my no not of on one or our "
    "ours she should so than that the their theirs them then there these they this those "
    "through to was we were what when where which who will with would you your".split()
)
_FR_STOPWORDS = frozenset(
    "a au aux avec ce ces dans de des du elle en et eux il ils je la le les leur lui ma "
    "mais me même mes moi mon ne nos notre nous on ou par pas pour qu que quelle quelles "
    "qui sa se ses son sur ta te tes toi ton tu un une vos votre vous y est sont être".split()
)
_BLOCK_TAGS = frozenset(
    {
        "address",
        "article",
        "blockquote",
        "br",
        "dd",
        "div",
        "dl",
        "dt",
        "figcaption",
        "figure",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "main",
        "ol",
        "p",
        "pre",
        "section",
        "table",
        "td",
        "th",
        "tr",
        "ul",
    }
)
_SKIP_TAGS = frozenset(
    {"script", "style", "noscript", "template", "nav", "header", "footer", "aside", "form", "svg"}
)
_ARTICLE_TYPES = frozenset({"article", "newsarticle", "blogposting", "reportagenewsarticle"})
_BODY_KEYS = frozenset({"articlebody", "content", "body", "text", "html"})
_TITLE_KEYS = ("headline", "title", "name")
_URL_KEYS = ("canonicalurl", "mainentityofpage", "url", "link", "@id")


class ExtractionMethod(StrEnum):
    HTML_GENERIC = "html_generic"
    HTML_ARTICLE = "html_article"
    JSON_LD = "json_ld"
    EMBEDDED_STATE = "embedded_state"
    RSS_ATOM = "rss_atom"
    JSON_FEED = "json_feed"
    CMS_JSON = "cms_json"
    OEMBED = "oembed"
    PDF_TEXT = "pdf_text"


@dataclass(frozen=True, slots=True)
class ExtractionCandidate:
    text: str
    title: str | None
    publisher: str | None
    published_at: date | None
    canonical_url: str | None
    method: ExtractionMethod
    quality: AcquisitionQuality
    metadata: tuple[tuple[str, str], ...] = ()
    reasons: tuple[str, ...] = ()


@dataclass(slots=True)
class _Node:
    tag: str
    attrs: dict[str, str]
    parent: _Node | None
    excluded: bool = False
    children: list[_Node | str] = field(default_factory=list)


class _SafeHTMLParser(HTMLParser):
    def __init__(self, source_url: str) -> None:
        super().__init__(convert_charrefs=True)
        self.root = _Node("document", {}, None)
        self.stack = [self.root]
        self.source_url = source_url
        self.scripts: list[tuple[str, dict[str, str]]] = []
        self.meta: dict[str, str] = {}
        self.title_text: list[str] = []
        self._in_title = False
        self._script_attrs: dict[str, str] | None = None
        self._script_parts: list[str] = []
        self._script_length = 0
        self._node_count = 1
        self._stopped = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self._stopped:
            return
        tag = tag.casefold()
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag == "meta":
            key = (values.get("property") or values.get("name") or "").casefold()
            if key and values.get("content"):
                self.meta.setdefault(key, values["content"])
        if tag == "script":
            self._script_attrs = values
            self._script_parts = []
            self._script_length = 0
        node = self._new_node(tag, values)
        if not self._stopped and tag not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.stack.append(node)
        if tag == "title":
            self._in_title = True

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag.casefold() not in {
            "area",
            "base",
            "br",
            "col",
            "embed",
            "hr",
            "img",
            "input",
            "link",
            "meta",
            "param",
            "source",
            "track",
            "wbr",
        }:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        tag = tag.casefold()
        if tag == "title":
            self._in_title = False
        if tag == "script" and self._script_attrs is not None:
            kind = self._script_attrs.get("type", "").split(";", 1)[0].strip().casefold()
            script_id = self._script_attrs.get("id", "")
            if kind == "application/ld+json" or script_id == "__NEXT_DATA__":
                self.scripts.append((" ".join(self._script_parts), self._script_attrs))
            self._script_attrs = None
            self._script_parts = []
            self._script_length = 0
        for index in range(len(self.stack) - 1, 0, -1):
            if self.stack[index].tag == tag:
                del self.stack[index:]
                break

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title_text.append(data)
        if self._script_attrs is not None:
            remaining = _MAX_JSON_CHARS - self._script_length
            if remaining > 0:
                part = data[:remaining]
                self._script_parts.append(part)
                self._script_length += len(part)
            return
        if self.stack and not self.stack[-1].excluded:
            self.stack[-1].children.append(data)

    def close(self) -> None:
        try:
            super().close()
        except Exception:
            # HTMLParser can leave an unfinished raw-text element on malformed input.
            if self._script_attrs is not None:
                self._script_attrs = None
                self._script_parts = []

    def _new_node(self, tag: str, attrs: dict[str, str]) -> _Node:
        parent = self.stack[-1]
        excluded = parent.excluded or tag in _SKIP_TAGS
        node = _Node(tag, attrs, parent, excluded)
        self._node_count += 1
        if self._node_count > _MAX_HTML_NODES or len(self.stack) >= _MAX_DEPTH:
            self._stopped = True
            return parent
        parent.children.append(node)
        return node


def extract_candidates(
    body: bytes,
    *,
    content_type: str | None,
    url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...] = (),
    min_text_chars: int = 200,
) -> tuple[ExtractionCandidate, ...]:
    """Extract deterministic article candidates from an already-observed body."""
    if not isinstance(body, bytes) or len(body) > _MAX_INPUT_BYTES or min_text_chars < 0:
        return ()
    mime = (content_type or "").split(";", 1)[0].strip().casefold()
    if mime == "application/pdf" or body.startswith(b"%PDF-"):
        return _extract_pdf_candidates(
            body,
            url=url,
            expected_title=expected_title,
            min_text_chars=min_text_chars,
            max_pages=200,
            expected_languages=expected_languages,
        )
    if mime in {"application/rss+xml", "application/atom+xml", "application/feed+json"}:
        return _extract_feed_candidates(
            body,
            url=url,
            expected_title=expected_title,
            min_text_chars=min_text_chars,
            expected_languages=expected_languages,
        )
    decoded = body.decode("utf-8-sig", errors="replace")
    if len(decoded) > _MAX_HTML_CHARS:
        return ()
    stripped = decoded.lstrip()
    if mime in {"application/json", "text/json"} or stripped.startswith(("{", "[")):
        return _extract_json(decoded, url, expected_title, expected_languages, min_text_chars)
    if mime in {"application/xml", "text/xml"} or (
        stripped.startswith("<") and re.search(r"(?i)<(?:rss|feed)\b", decoded[:2048])
    ):
        return _extract_feed_candidates(
            body,
            url=url,
            expected_title=expected_title,
            min_text_chars=min_text_chars,
            expected_languages=expected_languages,
        )
    if mime and mime not in {"text/html", "application/xhtml+xml"} and "html" not in mime:
        return ()
    return _extract_html(decoded, url, expected_title, expected_languages, min_text_chars)


def select_best_candidate(
    candidates: tuple[ExtractionCandidate, ...] | list[ExtractionCandidate],
    *,
    expected_title: str | None,
    min_score: float = 0.55,
) -> ExtractionCandidate | None:
    if not 0.0 <= min_score <= 1.0:
        raise ValueError("min_score must be between 0 and 1")
    eligible = [
        candidate
        for candidate in candidates
        if candidate.text.strip()
        and candidate.quality.text_chars >= 200
        and not candidate.quality.challenge_detected
        and not candidate.quality.paywall_detected
        and _title_similarity(expected_title, candidate.title) >= (0.25 if expected_title else 0.0)
        and candidate.quality.score >= min_score
    ]
    if not eligible:
        return None
    method_order = {method: index for index, method in enumerate(ExtractionMethod)}
    return min(
        eligible,
        key=lambda candidate: (
            -candidate.quality.score,
            method_order[candidate.method],
            candidate.canonical_url or "",
        ),
    )


def extract_pdf_candidates(
    body: bytes,
    *,
    url: str,
    expected_title: str | None,
    min_text_chars: int = 200,
    max_pages: int = 200,
) -> tuple[ExtractionCandidate, ...]:
    return _extract_pdf_candidates(
        body,
        url=url,
        expected_title=expected_title,
        min_text_chars=min_text_chars,
        max_pages=max_pages,
        expected_languages=(),
    )


def _extract_pdf_candidates(
    body: bytes,
    *,
    url: str,
    expected_title: str | None,
    min_text_chars: int,
    max_pages: int,
    expected_languages: tuple[str, ...],
) -> tuple[ExtractionCandidate, ...]:
    if (
        not body.startswith(b"%PDF-")
        or len(body) > _MAX_INPUT_BYTES
        or max_pages < 1
        or min_text_chars < 0
    ):
        return ()
    try:
        from io import BytesIO

        reader = PdfReader(BytesIO(body), strict=False)
        if reader.is_encrypted or len(reader.pages) > max_pages:
            return ()
        parts: list[str] = []
        total = 0
        for page in reader.pages:
            page_text = page.extract_text() or ""
            total += len(page_text)
            if total > _MAX_PDF_TEXT_CHARS:
                return ()
            parts.append(page_text)
        raw_text = "\n\n".join(parts)
        metadata = reader.metadata or {}
        title = _metadata_value(metadata, "/Title")
        author = _metadata_value(metadata, "/Author")
        published = _pdf_date(_metadata_value(metadata, "/CreationDate"))
    except Exception:
        return ()
    text = _normalize_text(raw_text)
    if len(text) < min_text_chars:
        return ()
    return (
        _candidate(
            text=raw_text,
            title=title,
            publisher=author,
            published_at=published,
            canonical_url=url,
            method=ExtractionMethod.PDF_TEXT,
            expected_title=expected_title,
            expected_languages=expected_languages,
            min_text_chars=min_text_chars,
        ),
    )


def extract_feed_candidates(
    body: bytes,
    *,
    url: str,
    expected_title: str | None,
    min_text_chars: int = 200,
) -> tuple[ExtractionCandidate, ...]:
    return _extract_feed_candidates(
        body,
        url=url,
        expected_title=expected_title,
        min_text_chars=min_text_chars,
        expected_languages=(),
    )


def _extract_feed_candidates(
    body: bytes,
    *,
    url: str,
    expected_title: str | None,
    min_text_chars: int,
    expected_languages: tuple[str, ...],
) -> tuple[ExtractionCandidate, ...]:
    if not isinstance(body, bytes) or len(body) > _MAX_INPUT_BYTES or min_text_chars < 0:
        return ()
    decoded = body.decode("utf-8-sig", errors="replace")
    if re.search(r"<!\s*(?:doctype|entity)\b", decoded, flags=re.I):
        return ()
    if len(decoded) > _MAX_HTML_CHARS:
        return ()
    stripped = decoded.lstrip()
    if stripped.startswith(("{", "[")):
        try:
            value = json.loads(decoded)
        except (json.JSONDecodeError, RecursionError, ValueError):
            return ()
        return _json_feed_candidates(value, url, expected_title, min_text_chars, expected_languages)
    try:
        root = ET.fromstring(decoded)
    except (ET.ParseError, ValueError, RecursionError):
        return ()
    root_tag = _local_name(root.tag).casefold()
    if root_tag not in {"rss", "rdf", "feed"}:
        return ()
    entries_candidate: list[ExtractionCandidate] = []
    if root_tag == "feed":
        entries = [item for item in root if _local_name(item.tag).casefold() == "entry"]
        publisher = _child_text(root, "title")
        for entry in entries:
            title = _child_text(entry, "title")
            link_node = next(
                (
                    child
                    for child in entry
                    if _local_name(child.tag).casefold() == "link" and child.attrib.get("href")
                ),
                None,
            )
            canonical = urljoin(url, link_node.attrib["href"]) if link_node is not None else None
            body_node = next(
                (
                    child
                    for child in entry
                    if _local_name(child.tag).casefold() in {"content", "summary"}
                ),
                None,
            )
            text_value = _xml_content(body_node)
            published = _parse_date(
                _child_text(entry, "published") or _child_text(entry, "updated")
            )
            if canonical and _feed_entry_matches(canonical, title, url, expected_title):
                candidate = _feed_candidate(
                    text_value,
                    title,
                    publisher,
                    published,
                    canonical,
                    ExtractionMethod.RSS_ATOM,
                    expected_title,
                    min_text_chars,
                    expected_languages,
                )
                if candidate is not None:
                    entries_candidate.append(candidate)
        return tuple(entries_candidate) if entries_candidate else ()
    channel = next(
        (item for item in root.iter() if _local_name(item.tag).casefold() == "channel"), root
    )
    publisher = _child_text(channel, "title")
    for entry in root.iter():
        if _local_name(entry.tag).casefold() != "item":
            continue
        title = _child_text(entry, "title")
        link = _child_text(entry, "link") or _child_text(entry, "guid")
        canonical = urljoin(url, link) if link else None
        content_node = next(
            (child for child in entry if _local_name(child.tag).casefold() == "encoded"), None
        )
        description = _child_text(entry, "description")
        text_value = (
            _xml_content(content_node) if content_node is not None else _plain_or_html(description)
        )
        published = _parse_date(_child_text(entry, "pubdate") or _child_text(entry, "date"))
        if canonical and _feed_entry_matches(canonical, title, url, expected_title):
            candidate = _feed_candidate(
                text_value,
                title,
                publisher,
                published,
                canonical,
                ExtractionMethod.RSS_ATOM,
                expected_title,
                min_text_chars,
                expected_languages,
            )
            if candidate is not None:
                entries_candidate.append(candidate)
    return tuple(entries_candidate)


def _extract_html(
    source: str,
    url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
) -> tuple[ExtractionCandidate, ...]:
    parser = _SafeHTMLParser(url)
    try:
        parser.feed(source)
        parser.close()
    except Exception:
        pass
    title = _first_nonempty(
        parser.meta.get("og:title"),
        parser.meta.get("twitter:title"),
        _normalize_text(" ".join(parser.title_text)),
    )
    # h1 is more article-specific than a site-wide <title>.
    h1 = _first_node_text(parser.root, "h1")
    title = _first_nonempty(
        parser.meta.get("og:title"), parser.meta.get("twitter:title"), h1, title
    )
    canonical = _first_nonempty(parser.meta.get("og:url"), _canonical_link(parser.root, url))
    if canonical:
        canonical = urljoin(url, canonical)
    publisher = parser.meta.get("og:site_name") or None
    candidates: list[ExtractionCandidate] = []
    preferred = _preferred_nodes(parser.root)
    seen_texts: set[tuple[str, ExtractionMethod]] = set()
    for node, method in preferred:
        raw_text, link_ratio, paragraphs = _node_content(node)
        text = _normalize_text(raw_text)
        identity = (text, method)
        if len(text) < min_text_chars or identity in seen_texts:
            continue
        seen_texts.add(identity)
        candidate = _candidate(
            text=text,
            title=title,
            publisher=publisher,
            published_at=None,
            canonical_url=canonical or url,
            method=method,
            expected_title=expected_title,
            expected_languages=expected_languages,
            min_text_chars=min_text_chars,
            boilerplate_ratio=link_ratio,
            paragraph_texts=paragraphs,
        )
        candidates.append(candidate)
    for script_text, attrs in parser.scripts:
        if attrs.get("type", "").split(";", 1)[0].strip().casefold() == "application/ld+json":
            candidates.extend(
                _json_ld_candidates(
                    script_text, url, expected_title, expected_languages, min_text_chars
                )
            )
        elif attrs.get("id") == "__NEXT_DATA__":
            candidates.extend(
                _embedded_state_candidates(
                    script_text, url, expected_title, expected_languages, min_text_chars
                )
            )
    for state_text in _state_assignments(source):
        candidates.extend(
            _embedded_state_candidates(
                state_text, url, expected_title, expected_languages, min_text_chars
            )
        )
    return _deduplicate_candidates(candidates)


def _extract_json(
    source: str,
    url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
) -> tuple[ExtractionCandidate, ...]:
    if len(source) > _MAX_JSON_CHARS:
        return ()
    try:
        value = json.loads(source)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return ()
    if _is_json_feed(value):
        return _json_feed_candidates(value, url, expected_title, min_text_chars, expected_languages)
    if (
        isinstance(value, dict)
        and value.get("type") in {"rich", "photo", "video", "link"}
        and "html" in value
    ):
        return _oembed_candidates(value, url, expected_title, expected_languages, min_text_chars)
    candidates = _cms_json_candidates(
        value, url, expected_title, expected_languages, min_text_chars
    )
    if not candidates:
        candidates = _generic_json_candidates(
            value, url, expected_title, expected_languages, min_text_chars
        )
    return _deduplicate_candidates(candidates)


def _json_ld_candidates(
    source: str,
    base_url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
) -> list[ExtractionCandidate]:
    if len(source) > _MAX_JSON_CHARS:
        return []
    try:
        value = json.loads(source)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return []
    output: list[ExtractionCandidate] = []
    for article in _walk_jsonld(value):
        raw_body = _first_nonempty(article.get("articleBody"), article.get("text"))
        if not raw_body:
            continue
        text = _plain_or_html(raw_body)
        if len(text) < min_text_chars:
            continue
        title = _string_value(article.get("headline") or article.get("name"))
        canonical = (
            _object_url(article.get("mainEntityOfPage"))
            or _string_value(article.get("url"))
            or _object_url(article.get("isBasedOn"))
            or base_url
        )
        publisher = _object_name(article.get("publisher"))
        author = _object_name(article.get("author"))
        published = _parse_date(_string_value(article.get("datePublished")))
        metadata = (("author", author),) if author else ()
        output.append(
            _candidate(
                text=text,
                title=title,
                publisher=publisher,
                published_at=published,
                canonical_url=urljoin(base_url, canonical),
                method=ExtractionMethod.JSON_LD,
                expected_title=expected_title,
                expected_languages=expected_languages,
                min_text_chars=min_text_chars,
                metadata=metadata,
            )
        )
    return output


def _embedded_state_candidates(
    source: str,
    base_url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
) -> list[ExtractionCandidate]:
    if len(source) > _MAX_JSON_CHARS:
        return []
    json_source = _unwrap_json_assignment(source)
    if json_source is None:
        return []
    try:
        value = json.loads(json_source)
    except (json.JSONDecodeError, RecursionError, ValueError):
        return []
    found: list[tuple[int, ExtractionCandidate]] = []
    visited = 0

    def visit(
        node: Any, depth: int, inherited_title: str | None, inherited_url: str | None
    ) -> None:
        nonlocal visited
        if depth > 20 or visited >= _MAX_JSON_NODES:
            return
        visited += 1
        if isinstance(node, dict):
            title = next(
                (
                    _string_value(node.get(key))
                    for key in _TITLE_KEYS
                    if _string_value(node.get(key))
                ),
                inherited_title,
            )
            canonical = next(
                (_object_url(node.get(key)) for key in _URL_KEYS if _object_url(node.get(key))),
                inherited_url,
            )
            for key, content in node.items():
                if (
                    key.casefold() in _BODY_KEYS
                    and isinstance(content, str)
                    and len(content) >= min_text_chars
                ):
                    text = _plain_or_html(content)
                    candidate_title = title
                    candidate_url = urljoin(base_url, canonical) if canonical else None
                    if len(text) >= min_text_chars and _state_is_coherent(
                        candidate_title, candidate_url, expected_title, base_url
                    ):
                        found.append(
                            (
                                len(text),
                                _candidate(
                                    text=text,
                                    title=candidate_title,
                                    publisher=_object_name(node.get("publisher")),
                                    published_at=_parse_date(
                                        _string_value(node.get("datePublished") or node.get("date"))
                                    ),
                                    canonical_url=candidate_url or base_url,
                                    method=ExtractionMethod.EMBEDDED_STATE,
                                    expected_title=expected_title,
                                    expected_languages=expected_languages,
                                    min_text_chars=min_text_chars,
                                ),
                            )
                        )
                elif isinstance(content, (dict, list)):
                    visit(content, depth + 1, title, canonical)
        elif isinstance(node, list):
            for item in node[:_MAX_JSON_NODES]:
                visit(item, depth + 1, inherited_title, inherited_url)

    visit(value, 0, None, None)
    found.sort(key=lambda pair: (-pair[1].quality.score, -pair[0], pair[1].canonical_url or ""))
    return [candidate for _, candidate in found[:3]]


def _cms_json_candidates(
    value: Any,
    url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
) -> list[ExtractionCandidate]:
    objects = value if isinstance(value, list) else [value]
    result: list[ExtractionCandidate] = []
    for item in objects[:100]:
        if not isinstance(item, dict):
            continue
        content_obj = item.get("content")
        title_obj = item.get("title")
        is_wordpress = isinstance(content_obj, dict) and "rendered" in content_obj
        if is_wordpress:
            raw = content_obj.get("rendered")
            title = (
                _string_value(title_obj.get("rendered"))
                if isinstance(title_obj, dict)
                else _string_value(title_obj)
            )
            canonical = _string_value(item.get("link"))
            text = _plain_or_html(raw) if isinstance(raw, str) else ""
            published = _parse_date(_string_value(item.get("date_gmt") or item.get("date")))
            if _cms_is_coherent(text, title, canonical, expected_title, url, min_text_chars):
                result.append(
                    _candidate(
                        text=text,
                        title=title,
                        publisher=None,
                        published_at=published,
                        canonical_url=urljoin(url, canonical or url),
                        method=ExtractionMethod.CMS_JSON,
                        expected_title=expected_title,
                        expected_languages=expected_languages,
                        min_text_chars=min_text_chars,
                    )
                )
            continue
        if isinstance(item.get("html"), str) and item.get("type") in {
            "rich",
            "photo",
            "video",
            "link",
        }:
            result.extend(
                _oembed_candidates(item, url, expected_title, expected_languages, min_text_chars)
            )
    if not result and isinstance(value, dict):
        result.extend(
            _generic_json_candidates(value, url, expected_title, expected_languages, min_text_chars)
        )
    return result


def _oembed_candidates(
    value: dict[str, Any],
    url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
) -> list[ExtractionCandidate]:
    raw = value.get("html")
    title = _string_value(value.get("title"))
    text = _plain_or_html(raw) if isinstance(raw, str) else ""
    if not _cms_is_coherent(text, title, None, expected_title, url, min_text_chars):
        return []
    return [
        _candidate(
            text=text,
            title=title,
            publisher=_string_value(value.get("author_name")),
            published_at=None,
            canonical_url=url,
            method=ExtractionMethod.OEMBED,
            expected_title=expected_title,
            expected_languages=expected_languages,
            min_text_chars=min_text_chars,
        )
    ]


def _generic_json_candidates(
    value: Any,
    url: str,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
) -> list[ExtractionCandidate]:
    result: list[ExtractionCandidate] = []
    visited = 0

    def visit(node: Any, depth: int) -> None:
        nonlocal visited
        if depth > 10 or visited >= 2_000:
            return
        visited += 1
        if isinstance(node, dict):
            title = _string_value(node.get("headline") or node.get("title"))
            canonical = _object_url(
                node.get("canonical_url") or node.get("url") or node.get("link")
            )
            for key in ("articleBody", "content", "body", "text"):
                raw = node.get(key)
                if isinstance(raw, str) and len(raw) >= min_text_chars:
                    text = _plain_or_html(raw)
                    if _cms_is_coherent(
                        text, title, canonical, expected_title, url, min_text_chars
                    ):
                        result.append(
                            _candidate(
                                text=text,
                                title=title,
                                publisher=_object_name(node.get("publisher")),
                                published_at=_parse_date(
                                    _string_value(node.get("datePublished") or node.get("date"))
                                ),
                                canonical_url=urljoin(url, canonical or url),
                                method=ExtractionMethod.CMS_JSON,
                                expected_title=expected_title,
                                expected_languages=expected_languages,
                                min_text_chars=min_text_chars,
                            )
                        )
                        break
            for child in node.values():
                if isinstance(child, (dict, list)):
                    visit(child, depth + 1)
        elif isinstance(node, list):
            for child in node[:100]:
                visit(child, depth + 1)

    visit(value, 0)
    return result


def _json_feed_candidates(
    value: Any,
    url: str,
    expected_title: str | None,
    min_text_chars: int,
    expected_languages: tuple[str, ...],
) -> tuple[ExtractionCandidate, ...]:
    if not _is_json_feed(value) or not isinstance(value, dict):
        return ()
    publisher = _string_value(value.get("title"))
    result: list[ExtractionCandidate] = []
    items = value.get("items")
    if not isinstance(items, list):
        return ()
    for item in items[:1000]:
        if not isinstance(item, dict):
            continue
        title = _string_value(item.get("title"))
        canonical = _string_value(item.get("url") or item.get("external_url"))
        if not _feed_entry_matches(canonical or "", title, url, expected_title):
            continue
        raw = item.get("content_html") or item.get("content_text") or item.get("summary")
        text = _plain_or_html(raw) if isinstance(raw, str) else ""
        candidate = _feed_candidate(
            text,
            title,
            publisher,
            _parse_date(_string_value(item.get("date_published"))),
            urljoin(url, canonical or url),
            ExtractionMethod.JSON_FEED,
            expected_title,
            min_text_chars,
            expected_languages,
        )
        if candidate is not None:
            result.append(candidate)
    return tuple(result)


def _feed_candidate(
    text: str,
    title: str | None,
    publisher: str | None,
    published_at: date | None,
    canonical_url: str,
    method: ExtractionMethod,
    expected_title: str | None,
    min_text_chars: int,
    expected_languages: tuple[str, ...],
) -> ExtractionCandidate | None:
    if len(_normalize_text(text)) < min_text_chars:
        return None
    return _candidate(
        text=text,
        title=title,
        publisher=publisher,
        published_at=published_at,
        canonical_url=canonical_url,
        method=method,
        expected_title=expected_title,
        expected_languages=expected_languages,
        min_text_chars=min_text_chars,
    )


def _candidate(
    *,
    text: str,
    title: str | None,
    publisher: str | None,
    published_at: date | None,
    canonical_url: str | None,
    method: ExtractionMethod,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
    boilerplate_ratio: float = 0.0,
    paragraph_texts: list[str] | None = None,
    metadata: tuple[tuple[str, str], ...] = (),
) -> ExtractionCandidate:
    raw_text = text
    normalized = _normalize_text(raw_text)
    quality = _quality(
        normalized,
        title,
        expected_title,
        expected_languages,
        min_text_chars,
        boilerplate_ratio=boilerplate_ratio,
        paragraph_texts=paragraph_texts or _paragraphs(raw_text),
    )
    reasons = quality.reasons
    return ExtractionCandidate(
        normalized,
        _optional_text(title),
        _optional_text(publisher),
        published_at,
        _optional_text(canonical_url),
        method,
        quality,
        metadata,
        reasons,
    )


def _quality(
    text: str,
    actual_title: str | None,
    expected_title: str | None,
    expected_languages: tuple[str, ...],
    min_text_chars: int,
    *,
    boilerplate_ratio: float,
    paragraph_texts: list[str],
) -> AcquisitionQuality:
    words = _WORD_RE.findall(text)
    similarity = _title_similarity(expected_title, actual_title)
    language = _detect_language(words)
    expected = {
        item.strip().casefold().split("-", 1)[0] for item in expected_languages if item.strip()
    }
    language_mismatch = bool(language and expected and language not in expected)
    duplicate_ratio = _duplicate_ratio(paragraph_texts)
    challenge = bool(_CHALLENGE_RE.search(text))
    paywall = bool(_PAYWALL_RE.search(text))
    truncated = _is_truncated(text)
    length_score = min(1.0, len(text) / max(min_text_chars * 1.5, 1))
    duplication_score = max(0.0, 1.0 - duplicate_ratio)
    boilerplate_score = max(0.0, 1.0 - boilerplate_ratio)
    language_score = 0.45 if language_mismatch else 1.0
    score = (
        0.24 * length_score
        + 0.32 * similarity
        + 0.14 * language_score
        + 0.13 * boilerplate_score
        + 0.12 * duplication_score
        + 0.05 * (0.45 if truncated else 1.0)
    )
    if challenge or paywall:
        score *= 0.15
    if similarity < 0.25 and expected_title:
        score *= 0.35
    if boilerplate_ratio > 0.6 or duplicate_ratio > 0.4:
        score *= 0.45
    reasons: list[str] = []
    if language_mismatch:
        reasons.append("language_mismatch")
    if challenge:
        reasons.append("challenge_detected")
    if paywall:
        reasons.append("paywall_detected")
    if truncated:
        reasons.append("possibly_truncated")
    if expected_title and similarity < 0.25:
        reasons.append("title_mismatch")
    if boilerplate_ratio > 0.6:
        reasons.append("excessive_boilerplate")
    if duplicate_ratio > 0.4:
        reasons.append("excessive_duplicate_paragraphs")
    return AcquisitionQuality(
        text_chars=len(text),
        word_count=len(words),
        title_similarity=similarity,
        language=language,
        boilerplate_ratio=_unit(boilerplate_ratio),
        duplicate_ratio=duplicate_ratio,
        truncated=truncated,
        challenge_detected=challenge,
        paywall_detected=paywall,
        score=_unit(score),
        reasons=tuple(reasons),
    )


def _title_similarity(expected: str | None, actual: str | None) -> float:
    if not expected:
        return 1.0
    if not actual:
        return 0.0
    expected = expected[:512]
    actual = actual[:512]
    left = _meaningful_title_words(expected)
    right = _meaningful_title_words(actual)
    if not left or not right:
        return 0.0
    overlap = left & right
    if not overlap:
        return 0.0
    jaccard = len(overlap) / len(left | right)
    ratio = difflib.SequenceMatcher(None, expected.casefold(), actual.casefold()).ratio()
    return _unit((jaccard + ratio) / 2)


def _meaningful_title_words(value: str) -> set[str]:
    return {
        word.casefold()
        for word in _WORD_RE.findall(value[:512])
        if len(word) >= 3 and word.casefold() not in (_EN_STOPWORDS | _FR_STOPWORDS)
    }


def _detect_language(words: list[str]) -> str | None:
    lowered = [word.casefold() for word in words]
    en = sum(word in _EN_STOPWORDS for word in lowered)
    fr = sum(word in _FR_STOPWORDS for word in lowered)
    if max(en, fr) < 3 or abs(en - fr) < 2:
        return None
    return "en" if en > fr else "fr"


def _is_truncated(text: str) -> bool:
    if _TRUNCATION_RE.search(text):
        return True
    words = _WORD_RE.findall(text)
    if not words:
        return False
    return words[-1].casefold() in _DANGLING_ENDINGS


def _duplicate_ratio(paragraphs: list[str]) -> float:
    cleaned = [
        _normalize_text(part).casefold() for part in paragraphs if len(_normalize_text(part)) >= 20
    ]
    if len(cleaned) < 2:
        return 0.0
    repeated = sum(count - 1 for count in Counter(cleaned).values() if count > 1)
    return _unit(repeated / len(cleaned))


def _preferred_nodes(root: _Node) -> list[tuple[_Node, ExtractionMethod]]:
    nodes = _descendant_nodes(root)
    scored: list[tuple[int, int, _Node]] = []
    for index, node in enumerate(nodes):
        if node.excluded:
            continue
        attrs = " ".join((node.attrs.get("class", ""), node.attrs.get("id", ""))).casefold()
        attrs_tokens = re.findall(r"[a-z0-9]+", attrs)
        itemprop = node.attrs.get("itemprop", "").casefold().split()
        if node.tag == "article":
            scored.append((5, index, node))
        elif "articlebody" in itemprop:
            scored.append((4, index, node))
        elif node.tag == "main":
            scored.append((3, index, node))
        elif any(
            marker in token
            for token in attrs_tokens
            for marker in ("article", "story", "post", "content", "body")
        ):
            scored.append((2, index, node))
    selected: list[tuple[_Node, ExtractionMethod]] = []
    if scored:
        best_score = max(item[0] for item in scored)
        for score, _, node in scored:
            if score == best_score:
                selected.append((node, ExtractionMethod.HTML_ARTICLE))
                if len(selected) == 4:
                    break
    body = next((node for node in nodes if node.tag == "body" and not node.excluded), None)
    generic = body or root
    selected.append((generic, ExtractionMethod.HTML_GENERIC))
    return selected


def _descendant_nodes(root: _Node) -> list[_Node]:
    nodes: list[_Node] = []
    stack: list[_Node] = [root]
    while stack:
        node = stack.pop()
        nodes.append(node)
        stack.extend(child for child in reversed(node.children or []) if isinstance(child, _Node))
    return nodes


def _node_content(node: _Node) -> tuple[str, float, list[str]]:
    text_parts: list[str] = []
    paragraph_parts: list[str] = []
    link_words = 0
    total_words = 0
    stack: list[tuple[_Node | str, bool]] = [(node, False)]
    while stack:
        item, in_link = stack.pop()
        if isinstance(item, str):
            if item.strip():
                text_parts.append(item)
                word_count = len(_WORD_RE.findall(item))
                total_words += word_count
                if in_link:
                    link_words += word_count
            continue
        if item.excluded:
            continue
        starts_paragraph = item.tag in {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}
        if starts_paragraph:
            text_parts.append("\n")
        next_link = in_link or item.tag == "a"
        children = item.children or []
        for child in reversed(children):
            stack.append((child, next_link))
        if item.tag in _BLOCK_TAGS:
            text_parts.append("\n")
    # Paragraph text is collected separately to retain repeated-block signals.
    paragraph_parts = []
    walk = [node]
    while walk:
        current = walk.pop()
        if current.excluded:
            continue
        if current.tag in {"p", "li", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"}:
            paragraph_parts.append(_node_plain_text(current))
        walk.extend(child for child in reversed(current.children or []) if isinstance(child, _Node))
    ratio = link_words / total_words if total_words else 0.0
    return "".join(text_parts), _unit(ratio), paragraph_parts


def _node_plain_text(node: _Node) -> str:
    parts: list[str] = []
    stack: list[_Node | str] = [node]
    while stack:
        item = stack.pop()
        if isinstance(item, str):
            parts.append(item)
        elif not item.excluded:
            stack.extend(reversed(item.children or []))
    return " ".join(parts)


def _first_node_text(root: _Node, tag: str) -> str | None:
    for node in _descendant_nodes(root):
        if node.tag == tag and not node.excluded:
            return _normalize_text(_node_plain_text(node)) or None
    return None


def _canonical_link(root: _Node, base_url: str) -> str | None:
    for node in _descendant_nodes(root):
        if node.tag == "link" and "canonical" in node.attrs.get("rel", "").casefold().split():
            href = node.attrs.get("href")
            if href:
                return urljoin(base_url, href)
    return None


def _state_assignments(source: str) -> list[str]:
    results: list[str] = []
    for match in re.finditer(
        r"(?is)(?:window\s*\.\s*)?__(?:NUXT|INITIAL_STATE|APOLLO_STATE)__\s*=", source
    ):
        start = match.end()
        while start < len(source) and source[start].isspace():
            start += 1
        end = _json_value_end(source, start)
        if end is not None and end - start <= _MAX_JSON_CHARS:
            results.append(source[start:end])
    return results


def _json_value_end(source: str, start: int) -> int | None:
    if start >= len(source) or source[start] not in "[{":
        return None
    opens = {"{": "}", "[": "]"}
    stack = [opens[source[start]]]
    in_string = False
    escaped = False
    for index in range(start + 1, min(len(source), start + _MAX_JSON_CHARS + 1)):
        char = source[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char in "[{":
            stack.append(opens[char])
        elif char in "]}":
            if not stack or stack.pop() != char:
                return None
            if not stack:
                return index + 1
    return None


def _unwrap_json_assignment(source: str) -> str | None:
    stripped = source.strip()
    if stripped.startswith(("{", "[")):
        return stripped
    end = _json_value_end(stripped, 0)
    return stripped[:end] if end is not None else None


def _walk_jsonld(value: Any) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    stack: list[tuple[Any, int]] = [(value, 0)]
    count = 0
    while stack and count < _MAX_JSON_NODES:
        node, depth = stack.pop()
        count += 1
        if depth > 20:
            continue
        if isinstance(node, list):
            stack.extend((item, depth + 1) for item in reversed(node[:_MAX_JSON_NODES]))
        elif isinstance(node, dict):
            types = node.get("@type", [])
            if isinstance(types, str):
                types = [types]
            if isinstance(types, list) and any(
                str(item).rsplit("/", 1)[-1].rsplit(":", 1)[-1].casefold() in _ARTICLE_TYPES
                for item in types
            ):
                result.append(node)
            graph = node.get("@graph")
            if isinstance(graph, (list, dict)):
                stack.append((graph, depth + 1))
    return result


def _feed_entry_matches(
    canonical: str, title: str | None, requested_url: str, expected_title: str | None
) -> bool:
    if _normalized_url(canonical) == _normalized_url(requested_url):
        return True
    if not expected_title or not title:
        return False
    similarity = _title_similarity(expected_title, title)
    expected_words = _meaningful_title_words(expected_title)
    found_words = _meaningful_title_words(title)
    return similarity >= 0.72 and bool(expected_words & found_words)


def _state_is_coherent(
    title: str | None, canonical: str | None, expected_title: str | None, requested_url: str
) -> bool:
    if canonical and _normalized_url(canonical) == _normalized_url(requested_url):
        return True
    if not title or not expected_title:
        return False
    words = _meaningful_title_words(expected_title)
    actual = _meaningful_title_words(title)
    return bool(words & actual) and _title_similarity(expected_title, title) >= 0.25


def _cms_is_coherent(
    text: str,
    title: str | None,
    canonical: str | None,
    expected_title: str | None,
    requested_url: str,
    minimum: int,
) -> bool:
    if len(text) < minimum:
        return False
    if canonical and _normalized_url(canonical) == _normalized_url(requested_url):
        return True
    if title and expected_title:
        expected = _meaningful_title_words(expected_title)
        actual = _meaningful_title_words(title)
        return bool(expected & actual) and _title_similarity(expected_title, title) >= 0.25
    return False


def _normalized_url(value: str) -> str:
    try:
        parsed = urlsplit(value)
        if not parsed.scheme or not parsed.hostname:
            return ""
        scheme = parsed.scheme.casefold()
        host = parsed.hostname.casefold()
        port = parsed.port
        netloc = (
            host
            if port is None or (scheme, port) in {("http", 80), ("https", 443)}
            else f"{host}:{port}"
        )
        path = parsed.path.rstrip("/") or "/"
        return urlunsplit((scheme, netloc, path, "", ""))
    except ValueError:
        return ""


class _TextOnlyParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.casefold() in _SKIP_TAGS:
            self.skip_depth += 1
        elif tag.casefold() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() in _SKIP_TAGS and self.skip_depth:
            self.skip_depth -= 1
        elif tag.casefold() in _BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip_depth:
            self.parts.append(data)


def _xml_content(node: ET.Element | None) -> str:
    if node is None:
        return ""
    value = "\n".join(node.itertext())
    return _plain_or_html(value)


def _plain_or_html(value: str | None) -> str:
    if not value:
        return ""
    # Keep angle-bracket IOC literals intact; parse only recognizable HTML tags.
    if not _HTML_TAG_RE.search(value):
        return html.unescape(value)
    parser = _TextOnlyParser()
    try:
        parser.feed(value)
        parser.close()
        text = "".join(parser.parts)
    except Exception:
        text = html.unescape(_TAG_RE.sub(" ", value))
    return text


def _normalize_text(value: str) -> str:
    return " ".join(value.split())


def _paragraphs(value: str) -> list[str]:
    return [part for part in re.split(r"\n\s*\n|\r?\n", value) if part.strip()]


def _local_name(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":")[-1]


def _child_text(parent: ET.Element, name: str) -> str | None:
    wanted = name.casefold()
    for child in parent:
        if _local_name(child.tag).casefold() == wanted:
            value = "\n".join(child.itertext()).strip()
            if value:
                return value
    return None


def _object_url(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _string_value(value.get("@id") or value.get("url"))
    if isinstance(value, list):
        for item in value:
            result = _object_url(item)
            if result:
                return result
    return None


def _object_name(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, dict):
        return _string_value(value.get("name") or value.get("@id"))
    if isinstance(value, list):
        for item in value:
            result = _object_name(item)
            if result:
                return result
    return None


def _string_value(value: Any) -> str | None:
    if isinstance(value, str):
        return value.strip() or None
    if isinstance(value, (int, float)):
        return str(value)
    return None


def _parse_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).date()
    except ValueError:
        try:
            parsed = email.utils.parsedate_to_datetime(value)
            return parsed.date() if parsed else None
        except (TypeError, ValueError, OverflowError):
            return None


def _pdf_date(value: str | None) -> date | None:
    if not value:
        return None
    match = re.match(r"D:(\d{4})(\d{2})?(\d{2})?", value)
    if match:
        try:
            return date(int(match.group(1)), int(match.group(2) or 1), int(match.group(3) or 1))
        except ValueError:
            return None
    return _parse_date(value)


def _metadata_value(metadata: Any, key: str) -> str | None:
    try:
        value = metadata.get(key)
    except (AttributeError, KeyError, TypeError):
        return None
    if value is None:
        return None
    rendered = str(value).strip()
    return rendered[:16_384] or None


def _first_nonempty(*values: str | None) -> str | None:
    return next((value.strip() for value in values if value and value.strip()), None)


def _optional_text(value: str | None) -> str | None:
    return value.strip() if value and value.strip() else None


def _unit(value: float) -> float:
    return max(0.0, min(1.0, value))


def _is_json_feed(value: Any) -> bool:
    return (
        isinstance(value, dict)
        and isinstance(value.get("version"), str)
        and "jsonfeed.org/version/" in value["version"]
        and isinstance(value.get("items"), list)
    )


def _deduplicate_candidates(
    candidates: list[ExtractionCandidate],
) -> tuple[ExtractionCandidate, ...]:
    result: list[ExtractionCandidate] = []
    seen: set[tuple[ExtractionMethod, str, str | None]] = set()
    for candidate in candidates:
        key = (candidate.method, candidate.text, candidate.canonical_url)
        if key not in seen:
            seen.add(key)
            result.append(candidate)
    return tuple(result)
