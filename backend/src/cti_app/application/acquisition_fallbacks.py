"""Pure helpers for safe, same-publication acquisition fallbacks."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import os
import re
import shlex
import time
from asyncio.subprocess import DEVNULL, PIPE
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import TYPE_CHECKING, Any, Protocol, cast
from urllib.parse import parse_qsl, quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

from cti_app.application.browser_acquisition import (
    BrowserUnavailableError,
    BrowserUnconfiguredError,
)
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
    _detect_mime,
)
from cti_app.domain.acquisition import (
    AcquisitionCapabilityState,
    AcquisitionObservationKind,
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    AcquisitionPolicyDecision,
    AcquisitionQuality,
    EvidenceClass,
    SourceEquivalenceClass,
    evidence_class_for_equivalence,
)
from cti_app.domain.classification import TLP
from cti_app.domain.collection import DetectedMimeType, SourceAcquisitionMethod
from cti_app.domain.discovery import canonicalize_http_url

if TYPE_CHECKING:
    from cti_app.application.acquisition_orchestrator import DomainConcurrencyLimiter

_LINK_RELATIONS = frozenset({"amphtml", "alternate", "canonical", "print"})
_LINK_HEADER_RE = re.compile(r"<([^>]+)>\s*;([^,]*)")
_REFUSAL_RE = re.compile(
    r"(?is)^\s*(?:i\s+(?:cannot|can't|could not|couldn't|am unable to)\b|"
    r"(?:the\s+)?article\s+(?:is\s+)?(?:unavailable|not available)\b|"
    r"(?:i\s+)?(?:cannot|can't|could not|couldn't)\s+(?:access|retrieve|view)\b)"
)
_UNACCEPTABLE_PAGE_RE = re.compile(
    r"(?i)\b(?:captcha|verify you are human|robot check|access denied|subscribe to continue|"
    r"subscribe to read|subscription required|sign in to continue|accept all cookies|"
    r"manage cookie preferences|security verification|unusual traffic|checking your browser|"
    r"privacy preference center|enable javascript to continue|page not found|"
    r"temporarily unavailable|service unavailable|subscriber-only|premium content|"
    r"403 forbidden|404 not found|502 bad gateway)\b"
)
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
MAX_RETRY_AFTER_SECONDS = 3.0
MAX_STRATEGY_CANDIDATES = 24
_EVIDENCE_CLASS_STRENGTH = {
    EvidenceClass.PRIMARY_OBSERVED: 7,
    EvidenceClass.HISTORICAL_PRIMARY: 6,
    EvidenceClass.VERIFIED_EQUIVALENT: 5,
    EvidenceClass.AUTHORIZED_REPUBLICATION: 4,
    EvidenceClass.INDEPENDENT_CORROBORATION: 3,
    EvidenceClass.OCR_DERIVED: 2,
    EvidenceClass.MODEL_CONTEXTUAL_UNVERIFIED: 1,
    EvidenceClass.UNKNOWN: 0,
}
_MAX_DERIVED_RAW_BYTES = 25 * 1024 * 1024
_MAX_DERIVED_BODY_BYTES = 5 * 1024 * 1024
_MAX_DERIVED_BODY_CHARS = 2_000_000


def derived_collected_response(
    *,
    requested_url: str,
    final_url: str,
    raw_body: bytes,
    raw_content_type: str | None,
    derived_body: bytes,
    derived_content_type: str,
    status: int,
    headers: dict[str, str],
    redirect_chain: tuple[str, ...] = (),
    acquired_at: datetime | None = None,
) -> CollectedResponse:
    """Build a bounded response that preserves observed and derived bytes separately."""
    if len(raw_body) > _MAX_DERIVED_RAW_BYTES:
        raise ValueError("observed_artifact_exceeds_limit")
    if len(derived_body) > _MAX_DERIVED_BODY_BYTES:
        raise ValueError("derived_article_exceeds_limit")
    if derived_content_type.casefold().split(";", 1)[0].strip() not in {
        "text/plain",
        "text/html",
        "application/xhtml+xml",
    }:
        raise ValueError("unsupported_derived_content_type")
    timestamp = acquired_at or datetime.now(UTC)
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)
    else:
        timestamp = timestamp.astimezone(UTC)
    detected = _detect_mime(derived_body)
    if detected not in {DetectedMimeType.TEXT, DetectedMimeType.HTML}:
        raise ValueError("derived_body_is_not_article_text")
    return CollectedResponse(
        requested_url=requested_url,
        final_url=final_url,
        redirect_chain=redirect_chain,
        status=status,
        headers=dict(headers),
        declared_content_type=raw_content_type,
        detected_content_type=detected,
        encoded_body=raw_body,
        decoded_body=derived_body,
        encoded_size=len(raw_body),
        encoded_sha256=hashlib.sha256(raw_body).hexdigest(),
        decoded_size=len(derived_body),
        decoded_sha256=hashlib.sha256(derived_body).hexdigest(),
        content_encoding="identity",
        acquired_at=timestamp,
    )


class _ArticleLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
        self.meta_title_parts: list[str] = []
        self.html_title_parts: list[str] = []
        self.in_title = False
        self.noindex = False
        self.description: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        if tag.casefold() == "title":
            self.in_title = True
        if tag.casefold() == "meta" and values.get("name", "").casefold() in {
            "robots",
            "googlebot",
        }:
            self.noindex = "noindex" in values.get("content", "").casefold()
        if tag.casefold() == "meta" and values.get("property", "").casefold() in {
            "og:title",
            "twitter:title",
        }:
            self.title_parts.append(values.get("content", ""))
            self.meta_title_parts.append(values.get("content", ""))
        if tag.casefold() != "link":
            return
        rels = set(values.get("rel", "").casefold().split())
        href = values.get("href", "").strip()
        if href and rels.intersection(_LINK_RELATIONS):
            self.links.append((href, " ".join(sorted(rels.intersection(_LINK_RELATIONS)))))

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self.in_title = False

    def handle_data(self, data: str) -> None:
        if self.in_title:
            self.title_parts.append(data)
            self.html_title_parts.append(data)

    def page_title(self) -> str:
        preferred = self.meta_title_parts or self.html_title_parts
        return " ".join(" ".join(preferred).split())


def discover_same_publication_variants(
    *,
    original_url: str,
    response_body: bytes | None,
    link_header: str | None,
    max_variants: int,
) -> tuple[tuple[str, SourceAcquisitionMethod], ...]:
    """Find only explicit canonical/AMP/print/feed links exposed by the page."""
    discovered: list[tuple[str, str]] = []
    if link_header:
        for match in _LINK_HEADER_RE.finditer(link_header):
            relations = set(re.findall(r"(?:^|;)\s*rel\s*=\s*\"?([^;\"]+)", match.group(2)))
            flattened = {part.casefold() for value in relations for part in value.split()}
            if flattened.intersection(_LINK_RELATIONS):
                discovered.append((match.group(1), " ".join(flattened)))

    if response_body:
        try:
            parser = _ArticleLinkParser()
            parser.feed(response_body.decode("utf-8", errors="replace"))
            discovered.extend(parser.links)
        except Exception:
            # A malformed anti-bot or error page contributes no candidate URLs.
            pass

    result: list[tuple[str, SourceAcquisitionMethod]] = []
    seen = {canonicalize_http_url(original_url)}
    for raw_url, _rel in discovered:
        try:
            candidate = canonicalize_http_url(urljoin(original_url, raw_url))
        except (TypeError, ValueError):
            continue
        if candidate in seen:
            continue
        seen.add(candidate)
        result.append((candidate, SourceAcquisitionMethod.HTTP_VARIANT))
        if len(result) >= max_variants:
            break
    return tuple(result)


def web_archive_snapshot_url(original_url: str) -> str:
    """Return the public Wayback replay address for the same canonical URL."""
    return f"https://web.archive.org/web/2id_/{canonicalize_http_url(original_url)}"


def bridge_transcription_prompt(*, url: str, title: str | None) -> str:
    safe_title = title.strip() if title and not _contains_credential_marker(title) else None
    title_line = f"EXPECTED_TITLE: {safe_title}\n" if safe_title else ""
    return (
        "Transcribe the public article exactly as visible. Do not invent, infer, or "
        "reconstruct missing paragraphs or IOCs. If the article or any requested detail "
        "cannot be accessed, write UNAVAILABLE for that field. Return only this format:\n"
        "<<<ARTICLE_TRANSCRIPTION>>>\n"
        "TITLE: ...\nSOURCE_URL: ...\nPUBLISHER: ...\nCONTENT:\n"
        "<exact article text>\n<<<END_ARTICLE_TRANSCRIPTION>>>\n"
        "<<<IOC_CANDIDATES>>>\n"
        "type | value | source_excerpt\n"
        "<one literal candidate per line, only when visible in the article>\n"
        "<<<END_IOC_CANDIDATES>>>\n\n"
        f"URL: {url}\n{title_line}"
    )


@dataclass(frozen=True, slots=True)
class BridgeIocCandidate:
    type: str
    value: str
    excerpt: str


@dataclass(frozen=True, slots=True)
class BridgeTranscription:
    article_text: str
    title: str | None
    source_url: str | None
    publisher: str | None
    ioc_candidates: tuple[BridgeIocCandidate, ...]
    raw_text: str
    parsed: bool
    reason: str | None


@dataclass(frozen=True, slots=True)
class BridgeSourceLocation:
    title: str | None
    publisher: str | None
    published_at: str | None
    url: str


def _unavailable_or_refusal(value: str) -> bool:
    normalized = value.strip()
    if not normalized or _REFUSAL_RE.search(normalized):
        return True
    return (
        re.fullmatch(
            r"(?is)(?:\[?\s*)?(?:unavailable|not available|no access|n/?a)(?:\s*\]?)\W*",
            normalized,
        )
        is not None
    )


def _parse_bridge_iocs(value: str, *, legacy: bool) -> tuple[BridgeIocCandidate, ...] | None:
    candidates: list[BridgeIocCandidate] = []
    for raw_line in value.splitlines():
        line = raw_line.strip()
        if line.startswith("```"):
            continue
        if not line or line.casefold() in {"none", "no ioc candidates", "n/a"}:
            continue
        if line.startswith("|") and line.endswith("|"):
            line = line[1:-1].strip()
        if re.fullmatch(r"[|\-:\s]+", line):
            continue
        if re.fullmatch(r"(?i)type\s*\|\s*value\s*\|\s*(?:source_excerpt|excerpt)", line):
            continue
        if "|" in line:
            columns = line.split("|", 2)
            if len(columns) != 3:
                return None
            candidate_type, candidate_value, excerpt = (part.strip() for part in columns)
        elif legacy:
            match = re.match(
                r"^\s*[-*]?\s*([^:]+):\s*(.*?)(?:\s+[\u2014\u2013-]\s+|\s*\|\s*)(.*)?$",
                raw_line,
            )
            if match is None:
                return None
            candidate_type = match.group(1).strip()
            candidate_value = match.group(2).strip()
            excerpt = (match.group(3) or "").strip()
        else:
            return None
        if not candidate_type or not candidate_value:
            return None
        candidates.append(BridgeIocCandidate(candidate_type, candidate_value, excerpt))
    return tuple(candidates)


def parse_bridge_transcription(text: str) -> BridgeTranscription:
    """Parse a fenced Bridge transcript while keeping model IOC notes separate."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    article_text = ""
    title: str | None = None
    source_url: str | None = None
    publisher: str | None = None
    candidates: tuple[BridgeIocCandidate, ...] = ()

    block = re.search(
        r"(?is)<<<ARTICLE_TRANSCRIPTION>>>\s*(.*?)\s*<<<END_ARTICLE_TRANSCRIPTION>>>",
        normalized,
    )
    ioc_block = re.search(
        r"(?is)<<<IOC_CANDIDATES>>>\s*(.*?)\s*<<<END_IOC_CANDIDATES>>>", normalized
    )
    if "<<<ARTICLE_TRANSCRIPTION>>>" in normalized:
        if block is None:
            return BridgeTranscription(
                "", None, None, None, (), text, False, "malformed_article_block"
            )
        fields: dict[str, str] = {}
        lines = block.group(1).splitlines()
        content_index: int | None = None
        for index, line in enumerate(lines):
            match = re.match(r"^\s*(TITLE|SOURCE_URL|PUBLISHER)\s*:\s*(.*)$", line, re.I)
            if match:
                fields.setdefault(match.group(1).upper(), match.group(2).strip())
            elif re.match(r"^\s*CONTENT\s*:\s*$", line, re.I):
                content_index = index + 1
                break
        if content_index is None:
            return BridgeTranscription(
                "", None, None, None, (), text, False, "missing_content_field"
            )
        article_text = "\n".join(lines[content_index:]).strip()
        title = _optional_location_value(fields.get("TITLE", ""))
        source_url = _optional_location_value(fields.get("SOURCE_URL", ""))
        publisher = _optional_location_value(fields.get("PUBLISHER", ""))
        if "<<<IOC_CANDIDATES>>>" in normalized and ioc_block is None:
            return BridgeTranscription(
                article_text, title, source_url, publisher, (), text, False, "malformed_ioc_block"
            )
        if ioc_block is not None:
            parsed_candidates = _parse_bridge_iocs(ioc_block.group(1), legacy=False)
            if parsed_candidates is None:
                return BridgeTranscription(
                    article_text,
                    title,
                    source_url,
                    publisher,
                    (),
                    text,
                    False,
                    "malformed_ioc_candidate",
                )
            candidates = parsed_candidates
    else:
        # Historical callers used ARTICLE:/IOCS: labels. Keep that wire format
        # readable, but require the article label so surrounding prose cannot
        # accidentally become an archived transcript.
        article_match = re.search(r"(?im)^\s*ARTICLE\s*:\s*", normalized)
        if article_match is None:
            return BridgeTranscription(
                "", None, None, None, (), text, False, "missing_article_label"
            )
        ioc_match = re.search(
            r"(?im)^\s*(?:IOCS|INDICATORS)\s*:\s*", normalized[article_match.end() :]
        )
        article_end = (
            article_match.end() + ioc_match.start() if ioc_match is not None else len(normalized)
        )
        article_text = normalized[article_match.end() : article_end].strip()
        if ioc_match is not None:
            ioc_start = article_match.end() + ioc_match.end()
            parsed_candidates = _parse_bridge_iocs(normalized[ioc_start:], legacy=True)
            if parsed_candidates is None:
                return BridgeTranscription(
                    article_text, None, None, None, (), text, False, "malformed_ioc_candidate"
                )
            candidates = parsed_candidates

    if _unavailable_or_refusal(article_text) or re.match(r"(?i)^\s*UNAVAILABLE\b", article_text):
        return BridgeTranscription(
            article_text,
            title,
            source_url,
            publisher,
            candidates,
            text,
            False,
            "article_unavailable",
        )
    if not article_text:
        return BridgeTranscription(
            "", title, source_url, publisher, candidates, text, False, "empty_article"
        )
    return BridgeTranscription(
        article_text, title, source_url, publisher, candidates, text, True, None
    )


def bridge_source_discovery_prompt(
    *,
    url: str,
    title: str | None,
    publisher: str | None,
    published_at: date | None,
) -> str:
    """Ask only for public locations; never include source bytes in discovery."""

    def safe(value: str | None) -> str:
        return value.strip() if value and not _contains_credential_marker(value) else "UNAVAILABLE"

    return (
        "Find potentially verifiable public locations or copies of this exact article. "
        "Return locations only; do not quote, summarize, or reproduce article/source bytes. "
        "Do not invent URLs. Use UNAVAILABLE for unknown metadata. Return this format:\n"
        "<<<PUBLIC_ARTICLE_LOCATIONS>>>\n"
        "title | publisher | date | URL\n"
        "<one public location per line>\n"
        "<<<END_PUBLIC_ARTICLE_LOCATIONS>>>\n\n"
        f"TITLE: {safe(title)}\nPUBLISHER: {safe(publisher)}\n"
        f"DATE: {published_at.isoformat() if published_at else 'UNAVAILABLE'}\nURL: {url}"
    )


def parse_bridge_source_locations(text: str) -> tuple[BridgeSourceLocation, ...]:
    """Extract candidate public URLs from the Bridge's untrusted suggestions."""
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    block = re.search(
        r"(?is)<<<PUBLIC_ARTICLE_LOCATIONS>>>\s*(.*?)\s*<<<END_PUBLIC_ARTICLE_LOCATIONS>>>",
        normalized,
    )
    body = block.group(1) if block is not None else normalized
    locations: list[BridgeSourceLocation] = []
    for line in body.splitlines():
        row = line.strip().strip("|").strip()
        if not row or re.fullmatch(r"[-|\s]+", row):
            continue
        if re.match(r"(?i)^title\s*\|\s*publisher\s*\|\s*date\s*\|\s*url$", row):
            continue
        if "|" in row:
            columns = [part.strip().strip("`\"'") for part in row.split("|", 3)]
            if len(columns) == 4:
                title_value, publisher_value, date_value, url_value = columns
                url_value = re.sub(r"\s*\[.*$", "", url_value)
                if re.match(r"(?i)^https?://", url_value):
                    locations.append(
                        BridgeSourceLocation(
                            _optional_location_value(title_value),
                            _optional_location_value(publisher_value),
                            _optional_location_value(date_value),
                            url_value.rstrip(".,;)]}"),
                        )
                    )
                    continue
        urls = re.findall(r"https?://[^\s<>\"'|]+", line, flags=re.I)
        for raw_url in urls:
            locations.append(BridgeSourceLocation(None, None, None, raw_url.rstrip(".,;)]}")))
    return tuple(locations)


def _optional_location_value(value: str) -> str | None:
    return value if value and value.casefold() not in {"unavailable", "n/a", "none"} else None


def deterministic_acquisition_quality(
    text: str,
    *,
    expected_title: str | None,
    expected_languages: tuple[str, ...] = (),
    reason: str,
) -> AcquisitionQuality:
    """Build a stable compact quality summary without consulting a model."""
    words = _WORD_RE.findall(text)
    title_words = {
        word.casefold() for word in _WORD_RE.findall(expected_title or "") if len(word) >= 3
    }
    body_words = {word.casefold() for word in words if len(word) >= 3}
    similarity = (
        len(title_words.intersection(body_words)) / len(title_words) if title_words else 1.0
    )
    paragraphs = [item.strip() for item in re.split(r"\n\s*\n", text) if item.strip()]
    duplicate_ratio = 1.0 - len(set(paragraphs)) / len(paragraphs) if paragraphs else 0.0
    challenge = bool(_UNACCEPTABLE_PAGE_RE.search(text))
    paywall = bool(re.search(r"(?i)\b(?:paywall|subscribe to read|subscription required)\b", text))
    truncated = bool(re.search(r"(?:\.{3}|…|\[truncated\])\s*$", text, re.I))
    length_score = min(1.0, len(words) / 200)
    score = max(0.0, min(1.0, 0.7 * length_score + 0.3 * similarity))
    return AcquisitionQuality(
        text_chars=len(text),
        word_count=len(words),
        title_similarity=similarity,
        language=expected_languages[0] if expected_languages else None,
        boilerplate_ratio=1.0 if challenge else 0.0,
        duplicate_ratio=duplicate_ratio,
        truncated=truncated,
        challenge_detected=challenge,
        paywall_detected=paywall,
        score=score,
        reasons=(reason,),
    )


def bridge_url_is_credential_free(url: str) -> bool:
    """Refuse signed or credential-like query parameters before model submission."""
    return _bridge_url_is_credential_free(url, depth=0)


def _bridge_url_is_credential_free(url: str, *, depth: int) -> bool:
    if depth > 3:
        return False
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if parsed.username or parsed.password:
        return False
    sensitive = re.compile(
        r"(?:^|[_-])(?:token|secret|signature|sig|auth|apikey|api_key|password|credential)"
        r"(?:$|[_-])",
        re.I,
    )
    if any(sensitive.search(unquote(key)) for key in re.findall(r"(?:^|&)([^=&]+)=", parsed.query)):
        return False
    if _contains_credential_marker(parsed.path):
        return False
    nested_urls = re.findall(r"https?%3A%2F%2F[^&]+", parsed.query, flags=re.I)
    return all(
        _bridge_url_is_credential_free(unquote(value), depth=depth + 1) for value in nested_urls
    )


def _contains_credential_marker(value: str) -> bool:
    return bool(
        re.search(
            r"(?i)(?:token|secret|signature|sig|auth|api[_-]?key|password|credential)"
            r"(?:[=/:-])[^/?&\s]{8,}",
            value,
        )
    )


def safe_fallback_url(url: str) -> str:
    """Keep only a request origin when persisting strategy target diagnostics."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            return "invalid-url"
        hostname = parsed.hostname
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        default_port = 443 if parsed.scheme == "https" else 80
        suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parsed.scheme}://{hostname}{suffix}/"
    except ValueError:
        return "invalid-url"


def article_rejection_reason(
    *,
    body: bytes,
    content_type: str | None,
    requested_url: str,
    expected_title: str | None,
    min_text_chars: int = 200,
    headers: dict[str, str] | None = None,
    expected_languages: tuple[str, ...] = (),
) -> str | None:
    """Reject empty, challenge, consent, paywall, and unrelated page captures."""
    try:
        decoded = body.decode("utf-8-sig", errors="replace")
    except (AttributeError, UnicodeError):
        return "content_not_text"
    title = ""
    if (content_type or "").casefold().split(";", 1)[0] in {
        "text/html",
        "application/xhtml+xml",
    } or "<html" in decoded[:1024].casefold():
        parser = _ArticleLinkParser()
        try:
            parser.feed(decoded)
        except Exception:
            pass
        if parser.noindex or "noindex" in (headers or {}).get("x-robots-tag", "").casefold():
            return "robots_noindex"
        title = " ".join(" ".join(parser.title_parts).split())
        text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", decoded)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
    else:
        text = decoded
    normalized_text = " ".join(text.split())
    if len(normalized_text) < min_text_chars:
        return "content_too_short"
    if _UNACCEPTABLE_PAGE_RE.search(f"{title}\n{normalized_text[:2000]}"):
        return "challenge_or_interstitial_page"
    words = _WORD_RE.findall(normalized_text)
    letters = sum(character.isalpha() for character in normalized_text)
    if len(words) < 20 or letters < min_text_chars // 2:
        return "content_language_or_text_check_failed"
    content_languages = {
        language.strip().casefold().split("-", 1)[0]
        for language in (headers or {}).get("content-language", "").split(",")
        if language.strip()
    }
    allowed_languages = {
        language.strip().casefold().split("-", 1)[0]
        for language in expected_languages
        if language.strip()
    }
    if (
        content_languages
        and allowed_languages
        and not content_languages.intersection(allowed_languages)
    ):
        return "content_language_mismatch"

    title_words = {
        word.casefold() for word in _WORD_RE.findall(expected_title or "") if len(word) >= 3
    }
    page_title_words = {word.casefold() for word in _WORD_RE.findall(title) if len(word) >= 3}
    if title_words and page_title_words and not title_words.intersection(page_title_words):
        return "title_mismatch"
    path = urlsplit(requested_url).path
    url_words = {
        word.casefold() for word in _WORD_RE.findall(path.replace("-", " ")) if len(word) >= 4
    }
    consistency_words = title_words or url_words
    if (
        consistency_words
        and page_title_words
        and not consistency_words.intersection(page_title_words)
    ):
        return "url_title_mismatch"
    if consistency_words and not page_title_words:
        content_words = {word.casefold() for word in words if len(word) >= 4}
        if not consistency_words.intersection(content_words):
            return "url_content_mismatch"
    return None


def bridge_location_metadata_rejection(
    location: BridgeSourceLocation, request: AcquisitionRequest
) -> str | None:
    """Reject suggestions whose claimed metadata conflicts with the requested article."""
    expected_title = " ".join((request.title or "").split()).casefold()
    suggested_title = " ".join((location.title or "").split()).casefold()
    if expected_title and suggested_title != expected_title:
        return "suggested_title_mismatch"
    if request.publisher and location.publisher:
        if (
            " ".join(request.publisher.split()).casefold()
            != " ".join(location.publisher.split()).casefold()
        ):
            return "suggested_publisher_mismatch"
    if request.published_at and location.published_at:
        suggested_date = location.published_at.strip()
        if not re.match(r"^\d{4}-\d{2}-\d{2}(?:\b|T)", suggested_date):
            return "suggested_publication_date_invalid"
        if suggested_date[:10] != request.published_at.isoformat():
            return "suggested_publication_date_mismatch"
    return None


def bridge_article_response_rejection(
    response: CollectedResponse,
    *,
    location: BridgeSourceLocation,
    request: AcquisitionRequest,
) -> str | None:
    """Require an observed article title and content check before accepting a URL."""
    expected_title = location.title or request.title
    if not expected_title:
        return "article_title_unavailable"
    content_type = response.declared_content_type
    decoded = response.decoded_body.decode("utf-8-sig", errors="replace")
    normalized_title = " ".join(expected_title.split()).casefold()
    if (content_type or "").casefold().split(";", 1)[0] in {
        "text/html",
        "application/xhtml+xml",
    } or "<html" in decoded[:1024].casefold():
        parser = _ArticleLinkParser()
        try:
            parser.feed(decoded)
        except Exception:
            return "malformed_article_html"
        observed_title = parser.page_title().casefold()
    else:
        observed_title = " ".join(decoded.split()).casefold()
    if observed_title != normalized_title and normalized_title not in observed_title:
        return "observed_title_mismatch"
    return article_rejection_reason(
        body=response.decoded_body,
        content_type=content_type,
        requested_url=response.final_url,
        expected_title=expected_title,
        min_text_chars=request.min_text_chars,
        headers=response.headers,
        expected_languages=request.expected_languages,
    )


@dataclass(frozen=True, slots=True)
class AcquisitionProbe:
    """One public strategy request, suitable for durable attempt diagnostics."""

    strategy_name: str
    strategy_version: str
    method: SourceAcquisitionMethod
    requested_url: str
    outcome: AcquisitionOutcome
    reason_code: str | None
    response: CollectedResponse | None
    http_status: int | None
    encoded_size: int | None
    encoded_sha256: str | None
    duration_ms: int
    retry_after: str | None = None
    model_run_id: str | None = None
    consultation_id: str | None = None
    prompt_sha256: str | None = None
    response_sha256: str | None = None
    policy_decision: AcquisitionPolicyDecision = AcquisitionPolicyDecision.ALLOWED
    capability_state: AcquisitionCapabilityState = AcquisitionCapabilityState.IMPLEMENTED
    observation_kind: AcquisitionObservationKind = AcquisitionObservationKind.HTTP_RESPONSE
    equivalence_class: SourceEquivalenceClass | None = None
    evidence_class: EvidenceClass | None = None
    quality: AcquisitionQuality | None = None
    archive_snapshot_time: datetime | None = None
    publication_date: date | None = None
    publisher: str | None = None
    model_generated: bool = False
    primary_source_verified: bool = False
    ioc_primary_verified: bool = False

    def __post_init__(self) -> None:
        # StrEnum retains existing string comparisons and serialization. Keep
        # any unmodeled historical status strings usable by older callers.
        try:
            typed_outcome = AcquisitionOutcome(self.outcome)
        except ValueError:
            pass
        else:
            object.__setattr__(self, "outcome", typed_outcome)
        if self.evidence_class is not None:
            supported_class = evidence_class_for_equivalence(self.equivalence_class)
            if (
                not (
                    self.method
                    in {
                        SourceAcquisitionMethod.BRIDGE_SOURCE_DISCOVERY,
                        SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION,
                    }
                    and self.evidence_class is EvidenceClass.MODEL_CONTEXTUAL_UNVERIFIED
                    and self.equivalence_class is SourceEquivalenceClass.UNKNOWN_MATCH
                )
                and _EVIDENCE_CLASS_STRENGTH[self.evidence_class]
                > _EVIDENCE_CLASS_STRENGTH[supported_class]
            ):
                raise ValueError(
                    "evidence_class cannot be stronger than the equivalence class supports"
                )


def _default_policy_context() -> AcquisitionPolicyContext:
    """A deny-by-default policy used only by legacy request constructors."""
    return AcquisitionPolicyContext(
        allowed_domains=frozenset(),
        blocked_domains=frozenset(),
        external_llm_allowed=False,
        tlp=TLP.RED,
        do_not_submit=True,
        expected_languages=(),
        allowed_syndication_domains=frozenset(),
        third_party_provider_allowed=False,
        provider_configured=False,
    )


@dataclass(frozen=True, slots=True)
class AcquisitionRequest:
    requested_url: str
    title: str | None
    published_at: date | None
    response_body: bytes | None
    response_status: int | None
    response_content_type: str | None
    response_headers: dict[str, str]
    link_header: str | None
    fetch: Callable[[str, dict[str, str], float, bool], Awaitable[CollectedResponse]]
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep
    bridge: Callable[[], Awaitable[AcquisitionProbe]] | None = None
    bridge_discovery: Callable[[int], Awaitable[Sequence[AcquisitionProbe]]] | None = None
    publisher: str | None = None
    attempted_strategy_urls: frozenset[str] = frozenset()
    min_text_chars: int = 200
    expected_languages: tuple[str, ...] = ()
    policy: AcquisitionPolicyContext = field(default_factory=_default_policy_context)


class AcquisitionStrategy(Protocol):
    """An ordered, pluggable acquisition port. Implementations never bypass safe fetch.

    ``capability_state`` and ``description`` remain optional runtime metadata.
    PEP 544 cannot express optional structural members; declaring them as
    properties would require every legacy frozen strategy to implement them.
    Callers that need either value should use ``getattr`` with a default.
    """

    @property
    def name(self) -> str: ...

    @property
    def version(self) -> str: ...

    @property
    def method(self) -> SourceAcquisitionMethod: ...

    @property
    def enabled(self) -> bool: ...

    @property
    def timeout_seconds(self) -> float: ...

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]: ...


def _typed_probe_outcome(outcome: AcquisitionOutcome | str) -> AcquisitionOutcome:
    try:
        return AcquisitionOutcome(outcome)
    except ValueError:
        # Legacy metadata-only probe outcomes are not acquisition outcomes.
        return cast(AcquisitionOutcome, outcome)


@dataclass(frozen=True, slots=True)
class BrowserRenderResult:
    rendered_html: str


class BrowserRenderAdapter(Protocol):
    async def render(
        self, *, url: str, html: str, timeout_seconds: float
    ) -> BrowserRenderResult: ...


class BrowserSidecarUnavailable(RuntimeError):
    """The isolated renderer is not reachable or did not return a render."""


@dataclass(frozen=True, slots=True)
class SidecarPlaywrightAdapter:
    """Call the isolated renderer over its private Compose network."""

    service_url: str

    def __post_init__(self) -> None:
        parsed = urlsplit(self.service_url)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Playwright sidecar URL must be a credential-free service URL")

    async def render(self, *, url: str, html: str, timeout_seconds: float) -> BrowserRenderResult:
        import httpx

        try:
            async with httpx.AsyncClient(
                timeout=timeout_seconds,
                trust_env=False,
            ) as client:
                response = await client.post(
                    f"{self.service_url.rstrip('/')}/render",
                    json={
                        "url": url,
                        "html": html,
                        "timeout_ms": max(500, min(round(timeout_seconds * 1000), 45_000)),
                    },
                )
        except httpx.HTTPError as error:
            raise BrowserSidecarUnavailable("Playwright sidecar is unavailable") from error
        if response.status_code in {502, 503, 504}:
            raise BrowserSidecarUnavailable("Playwright sidecar is unavailable")
        response.raise_for_status()
        try:
            payload = response.json()
        except ValueError as error:
            raise ValueError("Playwright sidecar returned invalid JSON") from error
        rendered_html = payload.get("rendered_html") if isinstance(payload, dict) else None
        if not isinstance(rendered_html, str) or not rendered_html.strip():
            raise ValueError("Playwright sidecar returned no rendered HTML")
        return BrowserRenderResult(rendered_html=rendered_html)


@dataclass(frozen=True, slots=True)
class PlaywrightStrategy:
    """Render only HTML collected through the ordinary pinned HTTP transport.

    Chromium receives an already-fetched document. The sidecar blocks all
    secondary requests, so JavaScript in the article cannot create a new
    network path around collection policy.
    """

    adapter: BrowserRenderAdapter | None
    name: str = "playwright"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PLAYWRIGHT_RENDERED
    enabled: bool = True
    timeout_seconds: float = 45.0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        if self.adapter is None:
            return (
                _empty_probe(
                    self.name,
                    self.version,
                    self.method,
                    request.requested_url,
                    "playwright_sidecar_unavailable",
                ),
            )
        body = request.response_body
        status = request.response_status
        headers = dict(request.response_headers or {})
        started = time.monotonic()
        if not body:
            try:
                response = await request.fetch(
                    request.requested_url, {}, self.timeout_seconds, False
                )
                body = response.decoded_body
                status = response.status
                headers = response.headers
            except CollectionError as error:
                body = error.response_body
                status = error.http_status
                headers = error.headers
        if not body:
            return (
                AcquisitionProbe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    requested_url=request.requested_url,
                    outcome=_typed_probe_outcome("failed"),
                    reason_code="no_renderable_html_available",
                    response=None,
                    http_status=status,
                    encoded_size=None,
                    encoded_sha256=None,
                    duration_ms=round((time.monotonic() - started) * 1000),
                ),
            )
        content_type = headers.get("content-type", "") or "text/html"
        if request.response_content_type:
            content_type = request.response_content_type
        if not content_type.casefold().startswith(("text/html", "application/xhtml+xml")):
            return (
                AcquisitionProbe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    requested_url=request.requested_url,
                    outcome=_typed_probe_outcome("rejected"),
                    reason_code="browser_render_requires_html",
                    response=None,
                    http_status=status,
                    encoded_size=len(body),
                    encoded_sha256=hashlib.sha256(body).hexdigest(),
                    duration_ms=round((time.monotonic() - started) * 1000),
                ),
            )
        try:
            html = body.decode("utf-8-sig", errors="replace")
            rendered = await self.adapter.render(
                url=safe_fallback_url(request.requested_url),
                html=html,
                timeout_seconds=self.timeout_seconds,
            )
        except BrowserSidecarUnavailable:
            return (
                AcquisitionProbe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    requested_url=request.requested_url,
                    outcome=_typed_probe_outcome("skipped"),
                    reason_code="playwright_sidecar_unavailable",
                    response=None,
                    http_status=status,
                    encoded_size=len(body),
                    encoded_sha256=hashlib.sha256(body).hexdigest(),
                    duration_ms=round((time.monotonic() - started) * 1000),
                ),
            )
        rendered_body = rendered.rendered_html.encode("utf-8")
        reason = article_rejection_reason(
            body=rendered_body,
            content_type="text/html",
            requested_url=request.requested_url,
            expected_title=request.title,
            min_text_chars=request.min_text_chars,
            headers=headers,
            expected_languages=request.expected_languages,
        )
        if reason is not None:
            return (
                AcquisitionProbe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    requested_url=request.requested_url,
                    outcome=_typed_probe_outcome("rejected"),
                    reason_code=reason,
                    response=None,
                    http_status=status,
                    encoded_size=len(rendered_body),
                    encoded_sha256=hashlib.sha256(rendered_body).hexdigest(),
                    duration_ms=round((time.monotonic() - started) * 1000),
                ),
            )
        digest = hashlib.sha256(rendered_body).hexdigest()
        acquired = datetime.now(UTC)
        rendered_response = CollectedResponse(
            requested_url=request.requested_url,
            final_url=request.requested_url,
            redirect_chain=(),
            status=200,
            headers={"content-type": "text/html; charset=utf-8"},
            declared_content_type="text/html",
            detected_content_type=DetectedMimeType.HTML,
            encoded_body=rendered_body,
            decoded_body=rendered_body,
            encoded_size=len(rendered_body),
            encoded_sha256=digest,
            decoded_size=len(rendered_body),
            decoded_sha256=digest,
            content_encoding="identity",
            acquired_at=acquired,
        )
        return (
            AcquisitionProbe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                requested_url=request.requested_url,
                outcome=_typed_probe_outcome("succeeded"),
                reason_code=None,
                response=rendered_response,
                http_status=status,
                encoded_size=len(rendered_body),
                encoded_sha256=digest,
                duration_ms=round((time.monotonic() - started) * 1000),
            ),
        )


async def _probe_url(
    request: AcquisitionRequest,
    *,
    strategy_name: str,
    strategy_version: str,
    method: SourceAcquisitionMethod,
    url: str,
    headers: dict[str, str] | None = None,
    timeout_seconds: float,
    validate_content: bool = True,
    allow_binary: bool = False,
) -> AcquisitionProbe:
    attempt_key = hashlib.sha256(f"{strategy_name}\0{url}".encode()).hexdigest()
    if attempt_key in request.attempted_strategy_urls:
        return _empty_probe(
            strategy_name,
            strategy_version,
            method,
            url,
            "already_attempted_for_strategy",
        )
    started = time.monotonic()
    try:
        response = await request.fetch(url, headers or {}, timeout_seconds, allow_binary)
    except CollectionError as error:
        body = error.response_body
        return AcquisitionProbe(
            strategy_name=strategy_name,
            strategy_version=strategy_version,
            method=method,
            requested_url=url,
            outcome=_typed_probe_outcome("failed"),
            reason_code=error.reason_code.value,
            response=None,
            http_status=error.http_status,
            encoded_size=error.encoded_size,
            encoded_sha256=hashlib.sha256(body).hexdigest() if body else None,
            duration_ms=round((time.monotonic() - started) * 1000),
            retry_after=error.headers.get("retry-after"),
        )
    reason = (
        article_rejection_reason(
            body=response.decoded_body,
            content_type=response.declared_content_type,
            requested_url=request.requested_url,
            expected_title=request.title,
            min_text_chars=request.min_text_chars,
            headers=response.headers,
            expected_languages=request.expected_languages,
        )
        if validate_content
        else None
    )
    return AcquisitionProbe(
        strategy_name=strategy_name,
        strategy_version=strategy_version,
        method=method,
        requested_url=url,
        outcome=_typed_probe_outcome("succeeded" if reason is None else "rejected"),
        reason_code=reason,
        response=response if reason is None else None,
        http_status=response.status,
        encoded_size=response.encoded_size,
        encoded_sha256=response.encoded_sha256,
        duration_ms=round((time.monotonic() - started) * 1000),
    )


@dataclass(frozen=True, slots=True)
class PinnedHttpProfileStrategy:
    name: str
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.HTTP_VARIANT
    enabled: bool = True
    timeout_seconds: float = 15.0
    headers: dict[str, str] | None = None
    retries: int = 0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        probes: list[AcquisitionProbe] = []
        for retry in range(self.retries + 1):
            probe = await _probe_url(
                request,
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=request.requested_url,
                headers={
                    name: request.requested_url if value == "$requested_url" else value
                    for name, value in (self.headers or {}).items()
                },
                timeout_seconds=self.timeout_seconds,
            )
            probes.append(probe)
            if probe.outcome == "succeeded":
                break
            if (
                probe.http_status not in {408, 425, 429, 500, 502, 503, 504}
                or retry >= self.retries
            ):
                break
            delay = retry_after_seconds(probe.retry_after, retry=retry)
            await request.sleep(delay)
        return tuple(probes)


@dataclass(frozen=True, slots=True)
class UnavailableTransportStrategy:
    name: str
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.HTTP_VARIANT
    version: str = "1"
    enabled: bool = True
    timeout_seconds: float = 1.0
    reason: str = "transport_not_supported_by_pinned_adapter"

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return AcquisitionCapabilityState.NOT_IMPLEMENTED

    @property
    def capability_reason(self) -> str:
        return self.reason

    @property
    def capability_outcome(self) -> AcquisitionOutcome:
        return AcquisitionOutcome.SKIPPED

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        return (
            AcquisitionProbe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                requested_url=request.requested_url,
                outcome=_typed_probe_outcome("skipped"),
                reason_code=self.reason,
                response=None,
                http_status=None,
                encoded_size=None,
                encoded_sha256=None,
                duration_ms=0,
                capability_state=self.capability_state,
            ),
        )


@dataclass(frozen=True, slots=True)
class AlternateRepresentationStrategy:
    name: str = "representations"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.HTTP_VARIANT
    enabled: bool = True
    timeout_seconds: float = 15.0
    max_candidates: int = 24

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        candidates = same_publication_variant_urls(
            request.requested_url,
            request.response_body,
            request.link_header,
            request.title,
            max_candidates=self.max_candidates,
        )
        probes: list[AcquisitionProbe] = []
        for url in candidates:
            probe = await _probe_url(
                request,
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=url,
                headers={
                    "accept": (
                        "text/html, application/xhtml+xml, application/atom+xml;q=0.9, "
                        "application/rss+xml;q=0.9, application/json;q=0.8"
                    )
                },
                timeout_seconds=self.timeout_seconds,
                validate_content=not url.endswith(("/sitemap.xml", "/news-sitemap.xml")),
            )
            if (
                url.endswith(("/sitemap.xml", "/news-sitemap.xml"))
                and probe.response is not None
                and probe.outcome == "succeeded"
            ):
                probe = _metadata_probe(
                    probe,
                    response=probe.response,
                    outcome="lookup_succeeded",
                    reason_code=None,
                )
            probes.append(probe)
            if probe.outcome == "succeeded":
                break
            if url.endswith(("/sitemap.xml", "/news-sitemap.xml")) and probe.response:
                discovered = sitemap_article_urls(
                    probe.response.decoded_body,
                    original_url=request.requested_url,
                    title=request.title,
                    max_candidates=max(1, self.max_candidates - len(probes)),
                )
                for article_url in discovered:
                    article_probe = await _probe_url(
                        request,
                        strategy_name=self.name,
                        strategy_version=self.version,
                        method=self.method,
                        url=article_url,
                        timeout_seconds=self.timeout_seconds,
                    )
                    probes.append(article_probe)
                    if article_probe.outcome == "succeeded":
                        break
                if probes[-1].outcome == "succeeded":
                    break
        if not probes:
            return (
                AcquisitionProbe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    requested_url=request.requested_url,
                    outcome=_typed_probe_outcome("skipped"),
                    reason_code="no_public_representation_discovered",
                    response=None,
                    http_status=None,
                    encoded_size=None,
                    encoded_sha256=None,
                    duration_ms=0,
                ),
            )
        return tuple(probes)


def _strategy_probe(
    *,
    name: str,
    version: str,
    method: SourceAcquisitionMethod,
    url: str,
    outcome: AcquisitionOutcome,
    reason: str | None = None,
    response: CollectedResponse | None = None,
    observed_response: CollectedResponse | None = None,
    duration_ms: int = 0,
    capability: AcquisitionCapabilityState = AcquisitionCapabilityState.IMPLEMENTED,
    policy_decision: AcquisitionPolicyDecision = AcquisitionPolicyDecision.ALLOWED,
    observation: AcquisitionObservationKind = AcquisitionObservationKind.HTTP_RESPONSE,
    equivalence: SourceEquivalenceClass | None = None,
    evidence: EvidenceClass | None = None,
    quality: AcquisitionQuality | None = None,
    publisher: str | None = None,
    publication_date: date | None = None,
    primary_source_verified: bool = False,
) -> AcquisitionProbe:
    metadata = observed_response or response
    return AcquisitionProbe(
        strategy_name=name,
        strategy_version=version,
        method=method,
        requested_url=url,
        outcome=outcome,
        reason_code=reason,
        response=response,
        http_status=metadata.status if metadata is not None else None,
        encoded_size=metadata.encoded_size if metadata is not None else None,
        encoded_sha256=metadata.encoded_sha256 if metadata is not None else None,
        duration_ms=max(0, duration_ms),
        capability_state=capability,
        policy_decision=policy_decision,
        observation_kind=observation,
        equivalence_class=equivalence,
        evidence_class=evidence,
        quality=quality,
        publication_date=publication_date,
        publisher=publisher,
        primary_source_verified=primary_source_verified,
    )


def _policy_skipped_probe(
    *,
    name: str,
    version: str,
    method: SourceAcquisitionMethod,
    url: str,
    decision: AcquisitionPolicyDecision,
    reason: str = "destination_policy_denied",
) -> AcquisitionProbe:
    return _strategy_probe(
        name=name,
        version=version,
        method=method,
        url=safe_fallback_url(url),
        outcome=AcquisitionOutcome.POLICY_SKIPPED,
        reason=reason,
        capability=AcquisitionCapabilityState.BLOCKED_BY_POLICY,
        policy_decision=decision,
    )


def _attempt_key(strategy_name: str, url: str) -> str:
    return hashlib.sha256(f"{strategy_name}\0{url}".encode()).hexdigest()


async def _fetch_for_strategy(
    request: AcquisitionRequest,
    *,
    name: str,
    version: str,
    method: SourceAcquisitionMethod,
    url: str,
    timeout_seconds: float,
    headers: dict[str, str] | None = None,
    allow_binary: bool = False,
) -> tuple[CollectedResponse | None, AcquisitionProbe | None]:
    if _attempt_key(name, url) in request.attempted_strategy_urls:
        return None, _strategy_probe(
            name=name,
            version=version,
            method=method,
            url=safe_fallback_url(url),
            outcome=AcquisitionOutcome.SKIPPED,
            reason="already_attempted_for_strategy",
        )
    decision = request.policy.check_url(url)
    if decision is not AcquisitionPolicyDecision.ALLOWED:
        return None, _policy_skipped_probe(
            name=name,
            version=version,
            method=method,
            url=url,
            decision=decision,
        )
    started = time.monotonic()
    try:
        response = await request.fetch(url, headers or {}, timeout_seconds, allow_binary)
    except CollectionError as error:
        return None, _strategy_probe(
            name=name,
            version=version,
            method=method,
            url=safe_fallback_url(url),
            outcome=AcquisitionOutcome.FAILED,
            reason=getattr(error.reason_code, "value", "collection_failed"),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except TimeoutError:
        return None, _strategy_probe(
            name=name,
            version=version,
            method=method,
            url=safe_fallback_url(url),
            outcome=AcquisitionOutcome.FAILED,
            reason="timeout",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except Exception:
        return None, _strategy_probe(
            name=name,
            version=version,
            method=method,
            url=safe_fallback_url(url),
            outcome=AcquisitionOutcome.FAILED,
            reason="fetch_failed",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    return response, None


def _host_for_acquisition(url: str) -> str | None:
    try:
        hostname = urlsplit(url).hostname
        return (
            hostname.encode("idna").decode("ascii").casefold().removeprefix("www.")
            if hostname
            else None
        )
    except (AttributeError, UnicodeError, TypeError, ValueError):
        return None


def _same_acquisition_publisher(left: str, right: str) -> bool:
    first, second = _host_for_acquisition(left), _host_for_acquisition(right)
    return first is not None and first == second


def _equivalence_for_observed_url(
    requested_url: str,
    observed_url: str,
) -> SourceEquivalenceClass:
    try:
        if canonicalize_http_url(requested_url) == canonicalize_http_url(observed_url):
            return SourceEquivalenceClass.EXACT_ORIGINAL
    except (TypeError, ValueError):
        return SourceEquivalenceClass.UNKNOWN_MATCH
    if _same_acquisition_publisher(requested_url, observed_url):
        return SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
    return SourceEquivalenceClass.UNKNOWN_MATCH


def _candidate_article(
    body: bytes,
    *,
    content_type: str | None,
    url: str,
    request: AcquisitionRequest,
    allowed_methods: frozenset[Any],
) -> Any | None:
    from cti_app.application.article_extraction import extract_candidates, select_best_candidate

    candidates = extract_candidates(
        body,
        content_type=content_type,
        url=url,
        expected_title=request.title,
        expected_languages=request.expected_languages,
        min_text_chars=request.min_text_chars,
    )
    preferred = tuple(candidate for candidate in candidates if candidate.method in allowed_methods)
    return select_best_candidate(
        preferred,
        expected_title=request.title,
        expected_url=url,
    )


def _feed_article_candidate(
    body: bytes,
    *,
    content_type: str | None,
    feed_url: str,
    request: AcquisitionRequest,
) -> Any | None:
    from cti_app.application.article_extraction import extract_candidates, select_best_candidate

    candidates = extract_candidates(
        body,
        content_type=content_type,
        url=feed_url,
        expected_title=request.title,
        expected_languages=request.expected_languages,
        min_text_chars=request.min_text_chars,
    )
    allowed = _methods(_FEED_METHODS)
    accepted = []
    for candidate in candidates:
        if candidate.method not in allowed or not candidate.canonical_url:
            continue
        # Feed entries identify article URLs separately from the feed endpoint.
        # Validate each entry against its own published URL, then classify the
        # feed response against the originally requested publication URL.
        if not _same_acquisition_publisher(request.requested_url, candidate.canonical_url):
            continue
        selected = select_best_candidate(
            (candidate,),
            expected_title=request.title,
            expected_url=candidate.canonical_url,
        )
        if selected is not None:
            accepted.append(selected)
    return max(accepted, key=lambda candidate: candidate.quality.score, default=None)


def _article_text_response(
    observed: CollectedResponse,
    candidate: Any,
    *,
    request: AcquisitionRequest,
    method: SourceAcquisitionMethod,
) -> CollectedResponse:
    del method  # The persisted acquisition method belongs to AcquisitionProbe.
    if len(candidate.text) > _MAX_DERIVED_BODY_CHARS:
        raise ValueError("derived_article_exceeds_limit")
    derived_body = candidate.text.encode("utf-8")
    return derived_collected_response(
        requested_url=request.requested_url,
        final_url=observed.final_url,
        raw_body=observed.encoded_body,
        raw_content_type=observed.declared_content_type,
        derived_body=derived_body,
        derived_content_type="text/plain",
        status=observed.status,
        headers=observed.headers,
        redirect_chain=observed.redirect_chain,
        acquired_at=observed.acquired_at,
    )


_STRUCTURED_METHODS = frozenset(
    {
        "json_ld",
        "embedded_state",
        "cms_json",
        "oembed",
        "html_article",
    }
)
_FEED_METHODS = frozenset({"rss_atom", "json_feed"})
_API_METHODS = frozenset({"cms_json", "oembed", "plain_text"})
_BROWSER_EMBEDDED_METHODS = frozenset({"json_ld", "embedded_state", "cms_json", "oembed"})


def _methods(values: frozenset[str]) -> frozenset[Any]:
    from cti_app.application.article_extraction import ExtractionMethod

    return frozenset(ExtractionMethod(value) for value in values)


@dataclass(frozen=True, slots=True)
class StructuredDataStrategy:
    name: str = "embedded_structured_data"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.EMBEDDED_STRUCTURED_DATA
    enabled: bool = True
    timeout_seconds: float = 15.0
    max_candidates: int = 12

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.enabled
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        candidates = (
            request.requested_url,
            *same_publication_variant_urls(
                request.requested_url,
                request.response_body,
                request.link_header,
                request.title,
                max_candidates=self.max_candidates,
            ),
        )
        probes: list[AcquisitionProbe] = []
        for url in tuple(dict.fromkeys(candidates))[: self.max_candidates + 1]:
            observed, issue = await _fetch_for_strategy(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=url,
                headers={"accept": "text/html, application/xhtml+xml, application/json"},
                timeout_seconds=self.timeout_seconds,
            )
            if issue is not None:
                probes.append(issue)
                continue
            assert observed is not None
            if observed.status != 200:
                probes.append(
                    _strategy_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="article_http_status_not_200",
                        observed_response=observed,
                    )
                )
                continue
            candidate = _candidate_article(
                observed.decoded_body,
                content_type=observed.declared_content_type,
                url=observed.final_url,
                request=request,
                allowed_methods=_methods(_STRUCTURED_METHODS),
            )
            if candidate is None:
                probes.append(
                    _strategy_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="no_accepted_structured_article",
                        observed_response=observed,
                    )
                )
                continue
            try:
                derived = _article_text_response(
                    observed, candidate, request=request, method=self.method
                )
            except (UnicodeEncodeError, ValueError):
                probes.append(
                    _strategy_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="derived_article_exceeds_limit",
                        observed_response=observed,
                    )
                )
                continue
            equivalence = _equivalence_for_observed_url(request.requested_url, observed.final_url)
            direct_publisher = (
                equivalence is SourceEquivalenceClass.EXACT_ORIGINAL
                and _same_acquisition_publisher(request.requested_url, observed.final_url)
            )
            probes.append(
                _strategy_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=url,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=derived,
                    observed_response=observed,
                    duration_ms=0,
                    observation=AcquisitionObservationKind.EMBEDDED_STRUCTURED_DATA,
                    equivalence=equivalence,
                    evidence=evidence_class_for_equivalence(equivalence),
                    quality=candidate.quality,
                    publisher=candidate.publisher,
                    publication_date=candidate.published_at,
                    primary_source_verified=direct_publisher,
                )
            )
            break
        if not probes:
            return (
                _strategy_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="no_structured_data_candidates",
                ),
            )
        return tuple(probes)


def _is_feed_url(url: str) -> bool:
    try:
        path = urlsplit(url).path.casefold().rstrip("/")
    except ValueError:
        return False
    return bool(
        re.search(r"(?:^|/)(?:feed|rss|atom)(?:\.xml|\.json|\.rss|\.atom)?$", path)
        or path.endswith((".rss", ".atom", ".jsonfeed"))
    )


@dataclass(frozen=True, slots=True)
class FeedStrategy:
    name: str = "rss_atom_jsonfeed"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.RSS_ATOM_JSONFEED
    enabled: bool = True
    timeout_seconds: float = 15.0
    max_candidates: int = 12

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.enabled
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        discovered = same_publication_variant_urls(
            request.requested_url,
            request.response_body,
            request.link_header,
            request.title,
            max_candidates=self.max_candidates,
        )
        parsed = urlsplit(canonicalize_http_url(request.requested_url))
        path = parsed.path or "/"
        known_feeds = tuple(
            urlunsplit((parsed.scheme, parsed.netloc, feed_path, "", ""))
            for feed_path in (
                "/feed",
                "/feed/",
                "/rss.xml",
                "/feed.xml",
                "/feed.json",
                "/atom.xml",
                path.rstrip("/") + "/feed/",
            )
        )
        urls = tuple(
            dict.fromkeys(url for url in (*known_feeds, *discovered) if _is_feed_url(url))
        )[: self.max_candidates]
        probes: list[AcquisitionProbe] = []
        for url in urls:
            observed, issue = await _fetch_for_strategy(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=url,
                headers={
                    "accept": "application/rss+xml, application/atom+xml, application/feed+json"
                },
                timeout_seconds=self.timeout_seconds,
            )
            if issue is not None:
                probes.append(issue)
                continue
            assert observed is not None
            candidate = None
            if observed.status == 200:
                candidate = _feed_article_candidate(
                    observed.decoded_body,
                    content_type=observed.declared_content_type,
                    feed_url=observed.final_url,
                    request=request,
                )
            if candidate is None:
                probes.append(
                    _strategy_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=(
                            "article_http_status_not_200"
                            if observed.status != 200
                            else "no_matching_full_text_feed_entry"
                        ),
                        observed_response=observed,
                    )
                )
                continue
            try:
                derived = _article_text_response(
                    observed, candidate, request=request, method=self.method
                )
            except (UnicodeEncodeError, ValueError):
                probes.append(
                    _strategy_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="derived_article_exceeds_limit",
                        observed_response=observed,
                    )
                )
                continue
            equivalence = (
                SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
                if _same_acquisition_publisher(request.requested_url, observed.final_url)
                else SourceEquivalenceClass.UNKNOWN_MATCH
            )
            probes.append(
                _strategy_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=url,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=derived,
                    observed_response=observed,
                    observation=AcquisitionObservationKind.HTTP_RESPONSE,
                    equivalence=equivalence,
                    evidence=evidence_class_for_equivalence(equivalence),
                    quality=candidate.quality,
                    publisher=candidate.publisher,
                    publication_date=candidate.published_at,
                )
            )
            break
        return tuple(probes) or (
            _strategy_probe(
                name=self.name,
                version=self.version,
                method=self.method,
                url=request.requested_url,
                outcome=AcquisitionOutcome.SKIPPED,
                reason="no_feed_urls_discovered",
            ),
        )


@dataclass(frozen=True, slots=True)
class PublisherApiStrategy:
    name: str = "publisher_public_api"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PUBLISHER_PUBLIC_API
    enabled: bool = True
    timeout_seconds: float = 15.0
    max_candidates: int = 8

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.enabled
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        discovered = same_publication_variant_urls(
            request.requested_url,
            request.response_body,
            request.link_header,
            request.title,
            max_candidates=self.max_candidates,
        )
        urls = tuple(
            dict.fromkeys(
                (
                    request.requested_url,
                    *(
                        url
                        for url in discovered
                        if any(
                            marker in urlsplit(url).path.casefold()
                            for marker in ("/wp-json/", "/oembed")
                        )
                        or urlsplit(url).path.casefold().endswith(".json")
                    ),
                )
            )
        )[: self.max_candidates + 1]
        probes: list[AcquisitionProbe] = []
        for url in urls:
            observed, issue = await _fetch_for_strategy(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=url,
                headers={"accept": "application/json, text/html;q=0.8"},
                timeout_seconds=self.timeout_seconds,
            )
            if issue is not None:
                probes.append(issue)
                continue
            assert observed is not None
            candidate = None
            if observed.status == 200:
                candidate = _candidate_article(
                    observed.decoded_body,
                    content_type=observed.declared_content_type,
                    url=observed.final_url,
                    request=request,
                    allowed_methods=_methods(_API_METHODS),
                )
            if candidate is None:
                probes.append(
                    _strategy_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=(
                            "article_http_status_not_200"
                            if observed.status != 200
                            else "no_coherent_public_api_article"
                        ),
                        observed_response=observed,
                    )
                )
                continue
            try:
                derived = _article_text_response(
                    observed, candidate, request=request, method=self.method
                )
            except (UnicodeEncodeError, ValueError):
                probes.append(
                    _strategy_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="derived_article_exceeds_limit",
                        observed_response=observed,
                    )
                )
                continue
            equivalence = _equivalence_for_observed_url(request.requested_url, observed.final_url)
            direct_publisher = (
                equivalence is SourceEquivalenceClass.EXACT_ORIGINAL
                and _same_acquisition_publisher(request.requested_url, observed.final_url)
            )
            probes.append(
                _strategy_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=url,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=derived,
                    observed_response=observed,
                    observation=AcquisitionObservationKind.HTTP_RESPONSE,
                    equivalence=equivalence,
                    evidence=evidence_class_for_equivalence(equivalence),
                    quality=candidate.quality,
                    publisher=candidate.publisher,
                    publication_date=candidate.published_at,
                    primary_source_verified=direct_publisher,
                )
            )
            break
        return tuple(probes)


def _browser_failure_probe(
    strategy: _BrowserStrategy,
    request: AcquisitionRequest,
    *,
    reason: str,
    outcome: AcquisitionOutcome = AcquisitionOutcome.FAILED,
    capability: AcquisitionCapabilityState | None = None,
) -> AcquisitionProbe:
    return _strategy_probe(
        name=strategy.name,
        version=strategy.version,
        method=strategy.method,
        url=safe_fallback_url(request.requested_url),
        outcome=outcome,
        reason=reason,
        capability=capability or strategy.capability_state,
        observation=(
            AcquisitionObservationKind.BROWSER_NETWORK_JSON
            if strategy.mode == "network"
            else AcquisitionObservationKind.BROWSER_DOM
        ),
    )


def _browser_unavailable_probe(
    strategy: _BrowserStrategy,
    request: AcquisitionRequest,
    *,
    reason: str,
) -> AcquisitionProbe:
    return _strategy_probe(
        name=strategy.name,
        version=strategy.version,
        method=strategy.method,
        url=safe_fallback_url(request.requested_url),
        outcome=AcquisitionOutcome.POLICY_SKIPPED,
        reason=reason,
        capability=AcquisitionCapabilityState.BLOCKED_BY_SECURITY,
        policy_decision=AcquisitionPolicyDecision.DENIED_PRIVATE_DESTINATION,
        observation=(
            AcquisitionObservationKind.BROWSER_NETWORK_JSON
            if strategy.mode == "network"
            else AcquisitionObservationKind.BROWSER_DOM
        ),
    )


@dataclass(frozen=True, slots=True)
class _BrowserStrategy:
    adapter: Any | None = None
    navigation_enabled: bool = False
    enabled: bool = True
    timeout_seconds: float = 45.0
    name: str = "browser"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PLAYWRIGHT_CHROMIUM_DOM
    mode: str = "dom"
    domain_limiter: DomainConcurrencyLimiter | None = None

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        if not self.enabled:
            return AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        if not self.navigation_enabled or self.adapter is None:
            return AcquisitionCapabilityState.BLOCKED_BY_SECURITY
        return AcquisitionCapabilityState.IMPLEMENTED

    @property
    def capability_reason(self) -> str:
        if not self.navigation_enabled or self.adapter is None:
            return "browser_navigation_disabled"
        return self.capability_state.value

    @property
    def capability_outcome(self) -> AcquisitionOutcome:
        return AcquisitionOutcome.SKIPPED

    @property
    def capability_policy_decision(self) -> AcquisitionPolicyDecision:
        return AcquisitionPolicyDecision.NOT_CONFIGURED

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        capability = self.capability_state
        if capability is not AcquisitionCapabilityState.IMPLEMENTED:
            return (
                _browser_failure_probe(
                    self,
                    request,
                    reason=self.capability_reason,
                    outcome=AcquisitionOutcome.SKIPPED,
                    capability=capability,
                ),
            )
        decision = request.policy.check_url(request.requested_url)
        if decision is not AcquisitionPolicyDecision.ALLOWED:
            return (
                _policy_skipped_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    decision=decision,
                ),
            )
        adapter = self.adapter
        if adapter is None:
            return (
                _browser_failure_probe(
                    self,
                    request,
                    reason="browser_navigation_disabled",
                    outcome=AcquisitionOutcome.SKIPPED,
                    capability=AcquisitionCapabilityState.BLOCKED_BY_SECURITY,
                ),
            )
        started = time.monotonic()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                if self.domain_limiter is None:
                    observation = await adapter.navigate(request.requested_url)
                else:
                    async with self.domain_limiter.semaphore_for(request.requested_url):
                        observation = await adapter.navigate(request.requested_url)
        except BrowserUnconfiguredError:
            return (_browser_unavailable_probe(self, request, reason="browser_unconfigured"),)
        except BrowserUnavailableError:
            return (_browser_unavailable_probe(self, request, reason="browser_unavailable"),)
        except TimeoutError:
            return (_browser_failure_probe(self, request, reason="browser_timeout"),)
        except Exception:
            return (_browser_failure_probe(self, request, reason="browser_unavailable"),)
        if observation.challenge_detected or observation.paywall_detected:
            return (
                _browser_failure_probe(
                    self,
                    request,
                    reason=(
                        "browser_challenge_detected"
                        if observation.challenge_detected
                        else "browser_paywall_detected"
                    ),
                    outcome=AcquisitionOutcome.REJECTED,
                ),
            )
        if self.mode != "network" and observation.status is not None and observation.status != 200:
            return (
                _browser_failure_probe(
                    self,
                    request,
                    reason="browser_http_status_not_200",
                    outcome=AcquisitionOutcome.REJECTED,
                ),
            )
        final_decision = request.policy.check_url(observation.final_url)
        if final_decision is not AcquisitionPolicyDecision.ALLOWED:
            return (
                _policy_skipped_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=observation.final_url,
                    decision=final_decision,
                    reason="browser_final_url_policy_denied",
                ),
            )

        selected: tuple[Any, bytes, str | None, str, Any | None] | None = None
        denied_capture: tuple[str, AcquisitionPolicyDecision] | None = None
        if self.mode == "network":
            allowed = _methods(_API_METHODS | frozenset({"json_ld", "embedded_state"}))
            for captured in observation.captured_json:
                capture_decision = request.policy.check_url(captured.url)
                if capture_decision is not AcquisitionPolicyDecision.ALLOWED:
                    denied_capture = (captured.url, capture_decision)
                    continue
                if captured.status != 200:
                    continue
                candidate = _candidate_article(
                    captured.body,
                    content_type=captured.content_type,
                    url=captured.url,
                    request=request,
                    allowed_methods=allowed,
                )
                if candidate is not None:
                    selected = (
                        candidate,
                        captured.body,
                        captured.content_type,
                        captured.url,
                        captured,
                    )
                    break
        else:
            html_body = observation.rendered_html.encode("utf-8")
            methods = (
                _methods(_BROWSER_EMBEDDED_METHODS)
                if self.mode == "embedded"
                else _methods(frozenset({"html_article", "html_generic"}))
            )
            candidate = _candidate_article(
                html_body,
                content_type="text/html",
                url=observation.final_url,
                request=request,
                allowed_methods=methods,
            )
            if candidate is not None:
                selected = (candidate, html_body, "text/html", observation.final_url, None)
        if selected is None:
            if denied_capture is not None:
                return (
                    _policy_skipped_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=denied_capture[0],
                        decision=denied_capture[1],
                        reason="browser_captured_json_url_policy_denied",
                    ),
                )
            return (
                _browser_failure_probe(
                    self,
                    request,
                    reason="no_matching_browser_article",
                    outcome=AcquisitionOutcome.REJECTED,
                ),
            )
        candidate, candidate_body, candidate_type, candidate_url, captured_response = selected
        if captured_response is not None:
            raw_body = captured_response.body
            raw_type = captured_response.content_type
            status = captured_response.status
            headers = captured_response.headers
            observed_final_url = candidate_url
        else:
            raw_body = (
                request.response_body if request.response_body is not None else candidate_body
            )
            raw_type = (
                request.response_content_type
                if request.response_body is not None
                else candidate_type
            )
            status = observation.status or request.response_status or 200
            headers = dict(request.response_headers)
            observed_final_url = observation.final_url
        observed = CollectedResponse(
            requested_url=request.requested_url,
            final_url=observed_final_url,
            redirect_chain=(),
            status=status,
            headers=headers,
            declared_content_type=raw_type,
            detected_content_type=_detect_mime(raw_body),
            encoded_body=raw_body,
            decoded_body=raw_body,
            encoded_size=len(raw_body),
            encoded_sha256=hashlib.sha256(raw_body).hexdigest(),
            decoded_size=len(raw_body),
            decoded_sha256=hashlib.sha256(raw_body).hexdigest(),
            content_encoding="identity",
            acquired_at=datetime.now(UTC),
        )
        try:
            response = _article_text_response(
                observed, candidate, request=request, method=self.method
            )
        except (UnicodeEncodeError, ValueError):
            return (
                _browser_failure_probe(
                    self,
                    request,
                    reason="derived_article_exceeds_limit",
                    outcome=AcquisitionOutcome.REJECTED,
                ),
            )
        equivalence = _equivalence_for_observed_url(request.requested_url, observed.final_url)
        duration = round((time.monotonic() - started) * 1000)
        return (
            _strategy_probe(
                name=self.name,
                version=self.version,
                method=self.method,
                url=candidate_url,
                outcome=AcquisitionOutcome.SUCCEEDED,
                response=response,
                observed_response=observed,
                duration_ms=duration,
                observation=(
                    AcquisitionObservationKind.BROWSER_NETWORK_JSON
                    if self.mode == "network"
                    else AcquisitionObservationKind.BROWSER_DOM
                ),
                equivalence=equivalence,
                evidence=evidence_class_for_equivalence(equivalence),
                quality=candidate.quality,
                publisher=candidate.publisher,
                publication_date=candidate.published_at,
                primary_source_verified=(
                    equivalence is SourceEquivalenceClass.EXACT_ORIGINAL
                    and _same_acquisition_publisher(request.requested_url, observed.final_url)
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class BrowserDomStrategy(_BrowserStrategy):
    name: str = "playwright_chromium_dom"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PLAYWRIGHT_CHROMIUM_DOM
    mode: str = "dom"


@dataclass(frozen=True, slots=True)
class BrowserNetworkStrategy(_BrowserStrategy):
    name: str = "playwright_network_response"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PLAYWRIGHT_NETWORK_RESPONSE
    mode: str = "network"


@dataclass(frozen=True, slots=True)
class BrowserEmbeddedStateStrategy(_BrowserStrategy):
    name: str = "playwright_embedded_state"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PLAYWRIGHT_EMBEDDED_STATE
    mode: str = "embedded"


@dataclass(frozen=True, slots=True)
class CapabilityPlaceholderStrategy:
    name: str
    method: SourceAcquisitionMethod
    capability: AcquisitionCapabilityState
    reason: str
    enabled: bool = True
    version: str = "1"
    timeout_seconds: float = 0.1

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return self.capability

    @property
    def capability_reason(self) -> str:
        return self.reason

    @property
    def capability_outcome(self) -> AcquisitionOutcome:
        return AcquisitionOutcome.NOT_CONFIGURED

    @property
    def capability_policy_decision(self) -> AcquisitionPolicyDecision:
        return AcquisitionPolicyDecision.NOT_CONFIGURED

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        policy_block = self.capability is AcquisitionCapabilityState.BLOCKED_BY_SECURITY
        return (
            _strategy_probe(
                name=self.name,
                version=self.version,
                method=self.method,
                url=safe_fallback_url(request.requested_url),
                outcome=(
                    AcquisitionOutcome.POLICY_SKIPPED
                    if policy_block
                    else AcquisitionOutcome.NOT_CONFIGURED
                ),
                reason=self.reason,
                capability=self.capability,
                policy_decision=(
                    AcquisitionPolicyDecision.DENIED_PRIVATE_DESTINATION
                    if policy_block
                    else AcquisitionPolicyDecision.NOT_CONFIGURED
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _CommandOcrEngine:
    command: tuple[str, ...]
    timeout_seconds: float = 20.0

    async def recognize(self, artifact: Any) -> str:
        if len(artifact.encoded_body) > _MAX_DERIVED_RAW_BYTES:
            raise ValueError("ocr_input_exceeds_limit")
        process = await asyncio.create_subprocess_exec(
            *self.command,
            stdin=PIPE,
            stdout=PIPE,
            stderr=DEVNULL,
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "LANG": "C.UTF-8",
                "HOME": "/tmp",
            },
        )
        input_task = asyncio.create_task(_write_ocr_input(process, artifact.encoded_body))
        output = bytearray()
        try:
            async with asyncio.timeout(self.timeout_seconds):
                stdout = process.stdout
                if stdout is None:
                    raise RuntimeError("ocr_command_stdout_unavailable")
                while True:
                    remaining = _MAX_DERIVED_BODY_BYTES - len(output)
                    chunk = await stdout.read(min(64 * 1024, remaining + 1))
                    if not chunk:
                        break
                    if len(output) + len(chunk) > _MAX_DERIVED_BODY_BYTES:
                        raise _OcrOutputLimitError
                    output.extend(chunk)
                await input_task
                await process.wait()
        except _OcrOutputLimitError as exc:
            await _kill_and_wait(process)
            await _cancel_ocr_input(input_task)
            raise RuntimeError("ocr_output_exceeds_limit") from exc
        except TimeoutError:
            await _kill_and_wait(process)
            await _cancel_ocr_input(input_task)
            raise
        except asyncio.CancelledError:
            await _kill_and_wait(process)
            await _cancel_ocr_input(input_task)
            raise
        except BaseException:
            await _kill_and_wait(process)
            await _cancel_ocr_input(input_task)
            raise
        if process.returncode != 0:
            raise RuntimeError("ocr_command_failed_or_output_exceeded_limit")
        return output.decode("utf-8", errors="replace")


class _OcrOutputLimitError(Exception):
    pass


async def _write_ocr_input(process: Any, body: bytes) -> None:
    stdin = process.stdin
    if stdin is None:
        return
    try:
        stdin.write(body)
        await stdin.drain()
    except (BrokenPipeError, ConnectionError, OSError, ValueError):
        pass
    finally:
        stdin.close()
        wait_closed = getattr(stdin, "wait_closed", None)
        if wait_closed is not None:
            try:
                await wait_closed()
            except (BrokenPipeError, ConnectionError, OSError, ValueError):
                pass


async def _cancel_ocr_input(task: asyncio.Task[None]) -> None:
    if not task.done():
        task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _kill_and_wait(process: Any) -> None:
    try:
        process.kill()
    except ProcessLookupError:
        pass
    await process.wait()


@dataclass(frozen=True, slots=True)
class _PolicyAwareStrategy:
    inner: Any
    configured_enabled: bool
    timeout_seconds: float

    @property
    def name(self) -> str:
        return str(self.inner.name)

    @property
    def version(self) -> str:
        return str(self.inner.version)

    @property
    def method(self) -> SourceAcquisitionMethod:
        return cast(SourceAcquisitionMethod, self.inner.method)

    @property
    def enabled(self) -> bool:
        return self.configured_enabled

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        if not self.configured_enabled:
            return AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        return getattr(
            self.inner,
            "capability_state",
            AcquisitionCapabilityState.IMPLEMENTED,
        )

    @property
    def capability_reason(self) -> str:
        if not self.configured_enabled:
            return "strategy_disabled"
        if self.name == "exact_title_search" and not self.inner.search_endpoint:
            return "search_endpoint_not_configured"
        if self.name == "official_newsletter_archive" and not self.inner.archive_endpoint:
            return "official_archive_not_configured"
        if self.name == "third_party_extraction" and not self.inner.enabled:
            return "provider_not_configured"
        return self.capability_state.value

    @property
    def capability_outcome(self) -> AcquisitionOutcome:
        return (
            AcquisitionOutcome.SKIPPED
            if not self.configured_enabled
            else AcquisitionOutcome.NOT_CONFIGURED
        )

    @property
    def capability_policy_decision(self) -> AcquisitionPolicyDecision:
        return AcquisitionPolicyDecision.NOT_CONFIGURED

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        if not self.configured_enabled:
            return (
                _strategy_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_fallback_url(request.requested_url),
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="strategy_disabled",
                    capability=AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION,
                ),
            )
        if self.name == "exact_title_search" and self.inner.search_endpoint:
            decision = request.policy.check_destination(
                self.inner.search_endpoint, external_llm=True
            )
            if decision is not AcquisitionPolicyDecision.ALLOWED:
                return (
                    _policy_skipped_probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=self.inner.search_endpoint,
                        decision=decision,
                        reason="external_search_policy_denied",
                    ),
                )

        denied: dict[str, AcquisitionPolicyDecision] = {}

        async def guarded_fetch(
            url: str,
            headers: dict[str, str],
            timeout_seconds: float,
            allow_binary: bool,
        ) -> CollectedResponse:
            decision = (
                AcquisitionPolicyDecision.DENIED_CREDENTIALS
                if not bridge_url_is_credential_free(url)
                else request.policy.check_url(url)
            )
            if self.name == "exact_title_search" and decision is AcquisitionPolicyDecision.ALLOWED:
                decision = request.policy.check_destination(url, external_llm=True)
            if decision is not AcquisitionPolicyDecision.ALLOWED:
                denied[url] = decision
                raise PermissionError("acquisition destination policy denied")
            return await request.fetch(url, headers, timeout_seconds, allow_binary)

        attempted = request.attempted_strategy_urls
        if (
            self.name == "third_party_extraction"
            and self.inner.enabled
            and self.inner.provider_url
            and request.title
        ):
            from cti_app.application.acquisition_discovery import safe_external_url

            endpoint = urlsplit(self.inner.provider_url)
            query = parse_qsl(endpoint.query, keep_blank_values=True)
            query.extend(
                (
                    ("url", canonicalize_http_url(request.requested_url)),
                    ("title", request.title),
                )
            )
            target = urlunsplit(
                (endpoint.scheme, endpoint.netloc, endpoint.path, urlencode(query), "")
            )
            if _attempt_key(self.name, safe_external_url(target)) in attempted:
                attempted = attempted | frozenset({_attempt_key(self.name, target)})
        guarded_request = replace(request, fetch=guarded_fetch, attempted_strategy_urls=attempted)
        probes = tuple(await self.inner.acquire(guarded_request))
        if not denied:
            return probes
        result: list[AcquisitionProbe] = []
        for probe in probes:
            denied_decision = denied.get(probe.requested_url)
            if denied_decision is None or probe.outcome is not AcquisitionOutcome.FAILED:
                result.append(probe)
                continue
            result.append(
                replace(
                    probe,
                    outcome=AcquisitionOutcome.POLICY_SKIPPED,
                    reason_code="destination_policy_denied",
                    response=None,
                    policy_decision=denied_decision,
                    capability_state=AcquisitionCapabilityState.BLOCKED_BY_POLICY,
                )
            )
        return tuple(result)


@dataclass(frozen=True, slots=True)
class _PublisherPdfAdapter:
    enabled: bool = True
    timeout_seconds: float = 20.0
    min_text_chars: int = 200
    expected_languages: tuple[str, ...] = ()

    name: str = "publisher_pdf"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PUBLISHER_PDF

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.enabled
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        if not self.enabled:
            return (
                _strategy_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="strategy_disabled",
                    capability=AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION,
                ),
            )
        from cti_app.application.acquisition_discovery import PublisherPdfStrategy
        from cti_app.application.article_extraction import extract_candidates, select_best_candidate

        pdf_urls: dict[str, str] = {}

        async def capture_pdf_url(
            url: str,
            headers: dict[str, str],
            timeout_seconds: float,
            allow_binary: bool,
        ) -> CollectedResponse:
            decision = request.policy.check_url(url)
            if (
                decision is not AcquisitionPolicyDecision.ALLOWED
                or not bridge_url_is_credential_free(url)
            ):
                raise PermissionError("acquisition destination policy denied")
            observed = await request.fetch(url, headers, timeout_seconds, allow_binary)
            pdf_urls[hashlib.sha256(observed.decoded_body).hexdigest()] = observed.final_url
            return observed

        def extract_pdf(raw_pdf: bytes) -> str:
            source_url = pdf_urls.get(hashlib.sha256(raw_pdf).hexdigest(), request.requested_url)
            candidates = extract_candidates(
                raw_pdf,
                content_type="application/pdf",
                url=source_url,
                expected_title=request.title,
                expected_languages=request.expected_languages,
                min_text_chars=self.min_text_chars,
            )
            selection_title = (
                request.title if any(candidate.title for candidate in candidates) else None
            )
            selected = select_best_candidate(
                candidates,
                expected_title=selection_title,
                expected_url=source_url,
                min_score=0.2,
            )
            if selected is None:
                return ""
            if request.title:
                expected_title = re.findall(r"[a-z0-9]+", request.title.casefold())
                extracted_prefix = re.findall(r"[a-z0-9]+", selected.text.casefold())
                if not expected_title or extracted_prefix[: len(expected_title)] != expected_title:
                    return ""
            return selected.text

        strategy = PublisherPdfStrategy(
            extractor=extract_pdf,
            timeout_seconds=self.timeout_seconds,
        )
        # The wrapper above validates the requested title as a normalized
        # prefix because PDF extractors often return the title and first body
        # sentence on one line. The C strategy still enforces PDF structure,
        # bounds, text quality, and publisher URL policy.
        return tuple(await strategy.acquire(replace(request, fetch=capture_pdf_url, title=None)))


@dataclass(frozen=True, slots=True)
class _VisualOcrAdapter:
    engine: Any | None
    enabled: bool = True
    timeout_seconds: float = 20.0
    name: str = "visual_ocr"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.VISUAL_OCR

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        if not self.enabled:
            return AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.engine is not None
            else AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE
        )

    @property
    def capability_reason(self) -> str:
        return "ocr_not_configured" if self.engine is None else self.capability_state.value

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        if self.engine is None:
            return (
                _strategy_probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.NOT_CONFIGURED,
                    reason="ocr_not_configured",
                    capability=AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE,
                ),
            )
        from cti_app.application.acquisition_discovery import CapturedArtifact, VisualOcrStrategy

        body = request.response_body
        content_type = request.response_content_type or _detect_mime(body or b"").value
        if body is None:
            artifact = None
        else:
            artifact = CapturedArtifact(
                encoded_body=body,
                content_type=content_type,
                source_url=request.requested_url,
                sha256=hashlib.sha256(body).hexdigest(),
            )
        strategy = VisualOcrStrategy(
            artifact=artifact,
            engine=self.engine,
            timeout_seconds=self.timeout_seconds,
        )
        return tuple(await strategy.acquire(request))


@dataclass(frozen=True, slots=True)
class WaybackStrategy:
    name: str = "wayback"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.WAYBACK
    enabled: bool = True
    timeout_seconds: float = 20.0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        probes: list[AcquisitionProbe] = []
        for variant in archive_url_variants(request.requested_url):
            query = urlencode(
                {
                    "url": variant,
                    "output": "json",
                    "filter": "statuscode:200",
                    "fl": "timestamp,original,statuscode",
                    "limit": "20",
                }
            )
            lookup = f"https://web.archive.org/cdx/search/cdx?{query}"
            probe = await _probe_url(
                request,
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=lookup,
                timeout_seconds=self.timeout_seconds,
                validate_content=False,
            )
            # The CDX payload is metadata, so it is intentionally not passed
            # through article validation. Preserve the request outcome/hash.
            if probe.response is None:
                probes.append(probe)
                continue
            lookup_response = probe.response
            probes.append(
                _metadata_probe(
                    probe,
                    response=lookup_response,
                    outcome="lookup_succeeded",
                    reason_code=None,
                )
            )
            snapshot = _wayback_snapshot(
                lookup_response.decoded_body,
                original_url=variant,
                published_at=request.published_at,
            )
            if snapshot is None:
                probes.append(
                    _empty_probe(
                        self.name,
                        self.version,
                        self.method,
                        lookup,
                        "no_matching_snapshot",
                    )
                )
                continue
            result = await _probe_url(
                request,
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=snapshot,
                timeout_seconds=self.timeout_seconds,
            )
            probes.append(result)
            if result.outcome == "succeeded":
                break
        return tuple(probes)


@dataclass(frozen=True, slots=True)
class ArchivePhStrategy:
    name: str = "archive_ph"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.ARCHIVE_PH
    enabled: bool = True
    timeout_seconds: float = 15.0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        probes: list[AcquisitionProbe] = []
        for host in ("archive.ph", "archive.today", "archive.is", "archive.md"):
            for variant in archive_url_variants(request.requested_url):
                probe = await _probe_url(
                    request,
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=f"https://{host}/{quote(variant, safe=':/?=&%')}",
                    timeout_seconds=self.timeout_seconds,
                )
                probes.append(probe)
                if probe.outcome == "succeeded":
                    return tuple(probes)
        return tuple(probes)


@dataclass(frozen=True, slots=True)
class CommonCrawlStrategy:
    name: str = "common_crawl"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.COMMON_CRAWL
    enabled: bool = True
    timeout_seconds: float = 20.0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        probes: list[AcquisitionProbe] = []
        lookup_url = "https://index.commoncrawl.org/collinfo.json"
        lookup = await _probe_url(
            request,
            strategy_name=self.name,
            strategy_version=self.version,
            method=self.method,
            url=lookup_url,
            timeout_seconds=self.timeout_seconds,
            validate_content=False,
        )
        if lookup.response is None:
            return (lookup,)
        probes.append(
            _metadata_probe(
                lookup, response=lookup.response, outcome="lookup_succeeded", reason_code=None
            )
        )
        index_id = _latest_common_crawl_index(lookup.response.decoded_body)
        if index_id is None:
            probes.append(
                _empty_probe(self.name, self.version, self.method, lookup_url, "index_unavailable")
            )
            return tuple(probes)
        for variant in archive_url_variants(request.requested_url):
            query = urlencode({"url": variant, "output": "json"})
            index_url = f"https://index.commoncrawl.org/{index_id}-index?{query}"
            index_probe = await _probe_url(
                request,
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=index_url,
                timeout_seconds=self.timeout_seconds,
                validate_content=False,
            )
            if index_probe.response is None:
                probes.append(index_probe)
                continue
            probes.append(
                _metadata_probe(
                    index_probe,
                    response=index_probe.response,
                    outcome="lookup_succeeded",
                    reason_code=None,
                )
            )
            record = _common_crawl_record(index_probe.response.decoded_body)
            if record is None:
                probes.append(
                    _empty_probe(
                        self.name, self.version, self.method, index_url, "no_matching_snapshot"
                    )
                )
                continue
            offset, length, filename = record
            warc_url = f"https://data.commoncrawl.org/{filename}"
            range_probe = await _probe_url(
                request,
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=warc_url,
                headers={"range": f"bytes={offset}-{offset + length - 1}"},
                timeout_seconds=self.timeout_seconds,
                validate_content=False,
                allow_binary=True,
            )
            if range_probe.response is None:
                probes.append(range_probe)
                continue
            article = _warc_article_response(range_probe.response, request.requested_url)
            if article is None:
                probes.append(
                    _metadata_probe(
                        range_probe,
                        response=range_probe.response,
                        outcome=_typed_probe_outcome("rejected"),
                        reason_code="invalid_warc_record",
                    )
                )
                continue
            reason = article_rejection_reason(
                body=article.decoded_body,
                content_type=article.declared_content_type,
                requested_url=request.requested_url,
                expected_title=request.title,
                headers=article.headers,
            )
            probes.append(
                AcquisitionProbe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    requested_url=warc_url,
                    outcome=_typed_probe_outcome("succeeded" if reason is None else "rejected"),
                    reason_code=reason,
                    response=article if reason is None else None,
                    http_status=range_probe.http_status,
                    encoded_size=article.encoded_size,
                    encoded_sha256=article.encoded_sha256,
                    duration_ms=range_probe.duration_ms,
                )
            )
            if reason is None:
                break
        return tuple(probes)


@dataclass(frozen=True, slots=True)
class MementoStrategy:
    name: str = "memento"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.MEMENTO
    enabled: bool = True
    timeout_seconds: float = 15.0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        target_date = (request.published_at or datetime.now(UTC).date()).isoformat()
        url = (
            "https://timetravel.mementoweb.org/api/json/"
            f"{target_date}/{quote(canonicalize_http_url(request.requested_url), safe=':/?=&%')}"
        )
        lookup = await _probe_url(
            request,
            strategy_name=self.name,
            strategy_version=self.version,
            method=self.method,
            url=url,
            timeout_seconds=self.timeout_seconds,
            validate_content=False,
        )
        if lookup.response is None:
            return (lookup,)
        probes = [
            _metadata_probe(
                lookup, response=lookup.response, outcome="lookup_succeeded", reason_code=None
            )
        ]
        snapshot = _memento_url(lookup.response.decoded_body)
        if snapshot is None:
            probes.append(
                _empty_probe(self.name, self.version, self.method, url, "no_matching_snapshot")
            )
            return tuple(probes)
        result = await _probe_url(
            request,
            strategy_name=self.name,
            strategy_version=self.version,
            method=self.method,
            url=snapshot,
            timeout_seconds=self.timeout_seconds,
        )
        probes.append(result)
        return tuple(probes)


@dataclass(frozen=True, slots=True)
class SearchCacheStrategy:
    name: str = "search_cache"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.SEARCH_CACHE
    enabled: bool = True
    timeout_seconds: float = 10.0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        # Google ended its public cache endpoint. Keep a separately configurable
        # probe so operators can see this path is unavailable; never substitute
        # a third-party reader proxy or login-backed cache.
        return (
            _empty_probe(
                self.name,
                self.version,
                self.method,
                request.requested_url,
                "no_supported_public_search_cache_endpoint",
            ),
        )


@dataclass(frozen=True, slots=True)
class BridgeTranscriptionStrategy:
    name: str = "bridge_transcription"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.BRIDGE_TRANSCRIPTION
    enabled: bool = True
    timeout_seconds: float = 90.0

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        if request.bridge is None:
            return (
                _empty_probe(
                    self.name,
                    self.version,
                    self.method,
                    request.requested_url,
                    "bridge_unavailable",
                ),
            )
        return (await request.bridge(),)


@dataclass(frozen=True, slots=True)
class BridgeSourceDiscoveryStrategy:
    name: str = "bridge_source_discovery"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.BRIDGE_SOURCE_DISCOVERY
    enabled: bool = True
    timeout_seconds: float = 90.0
    max_candidates: int = MAX_STRATEGY_CANDIDATES

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.enabled
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    async def acquire(self, request: AcquisitionRequest) -> Sequence[AcquisitionProbe]:
        if request.bridge_discovery is None:
            return (
                _empty_probe(
                    self.name,
                    self.version,
                    self.method,
                    request.requested_url,
                    "bridge_unavailable",
                ),
            )
        return tuple(await request.bridge_discovery(self.max_candidates))


def default_acquisition_strategies(
    *,
    flags: dict[str, bool] | None = None,
    timeouts: dict[str, float] | None = None,
    max_candidates: int = 24,
    playwright_adapter: BrowserRenderAdapter | None = None,
    browser_adapter: Any | None = None,
    domain_limiter: DomainConcurrencyLimiter | None = None,
    browser_service_url: str | None = None,
    browser_control_socket: str = "",
    browser_navigation_enabled: bool = False,
    exact_title_search_endpoint: str = "",
    syndication_domains: frozenset[str] = frozenset(),
    third_party_provider_url: str = "",
    third_party_provider_token: str = "",
    third_party_provider_enabled: bool = False,
    newsletter_archive_endpoint: str = "",
    ocr_command: str = "",
    min_text_chars: int = 200,
    expected_languages: tuple[str, ...] = (),
) -> tuple[AcquisitionStrategy, ...]:
    """Ordered default strategy registry. All HTTP calls remain SafeHttpCollector calls."""
    enabled = flags if flags is not None else {}
    timeout = timeouts if timeouts is not None else {}

    def configured(name: str, default: float) -> float:
        return max(0.1, min(float(timeout.get(name, default)), 300.0))

    def on(name: str) -> bool:
        return enabled.get(name, True)

    def on_any(*names: str) -> bool:
        return next((enabled[name] for name in names if name in enabled), True)

    if browser_adapter is None and (browser_service_url or browser_control_socket):
        from cti_app.application.browser_acquisition import SidecarBrowserAdapter

        browser_adapter = SidecarBrowserAdapter(
            service_url=browser_service_url or None,
            socket_path=browser_control_socket or None,
            timeout_seconds=configured("playwright_chromium_dom", 45),
        )
    # The legacy renderer only renders already fetched HTML and cannot provide
    # navigation or captured network JSON. Keep its argument for callers that
    # instantiate this registry directly, but do not route navigation through it.
    del playwright_adapter

    from cti_app.application.acquisition_archives import (
        CommonCrawlWarcStrategy,
        MementoArchiveStrategy,
        WaybackSnapshotStrategy,
    )
    from cti_app.application.acquisition_discovery import (
        AuthorizedSyndicationStrategy,
        ExactTitleSearchStrategy,
        OfficialNewsletterArchiveStrategy,
        PublisherAlternativeUrlStrategy,
        ThirdPartyExtractionStrategy,
    )

    try:
        ocr_args = tuple(shlex.split(ocr_command)) if ocr_command.strip() else ()
    except ValueError:
        ocr_args = ()
    ocr_engine = (
        _CommandOcrEngine(ocr_args, timeout_seconds=configured("visual_ocr", 20))
        if ocr_args
        else None
    )

    def guarded(inner: Any, key: str, default_timeout: float) -> _PolicyAwareStrategy:
        return _PolicyAwareStrategy(
            inner=inner,
            configured_enabled=on(key),
            timeout_seconds=configured(key, default_timeout),
        )

    newsletter_host = _host_for_acquisition(newsletter_archive_endpoint)
    newsletter_domains = frozenset(
        (*syndication_domains, *((newsletter_host,) if newsletter_host else ()))
    )
    provider = ThirdPartyExtractionStrategy(
        provider_url=third_party_provider_url or None,
        provider_token=third_party_provider_token or None,
        # SafeHttpCollector currently strips Authorization; never forward the token.
        auth_transport_supported=False,
    )

    return (
        PinnedHttpProfileStrategy(
            "http_plain", timeout_seconds=configured("http_plain", 15), enabled=on("http_plain")
        ),
        PinnedHttpProfileStrategy(
            "http_standard_headers",
            timeout_seconds=configured("http_standard_headers", 15),
            enabled=on("http_standard_headers"),
            headers={
                "accept": "text/html, application/xhtml+xml, application/pdf;q=0.9, */*;q=0.8",
                "accept-language": "en-US,en;q=0.8",
            },
        ),
        PinnedHttpProfileStrategy(
            "http_same_site_referer",
            timeout_seconds=configured("http_same_site_referer", 15),
            enabled=on("http_same_site_referer"),
            headers={"referer": "$requested_url"},
        ),
        PinnedHttpProfileStrategy(
            "http_retry_after",
            timeout_seconds=configured("http_retry_after", 20),
            enabled=on("http_retry_after"),
            retries=2,
        ),
        UnavailableTransportStrategy(
            "http2", enabled=on("http2"), timeout_seconds=configured("http2", 1)
        ),
        UnavailableTransportStrategy(
            "curl",
            enabled=on("curl"),
            timeout_seconds=configured("curl", 1),
            reason="curl_adapter_not_available_with_same_dns_pinning",
        ),
        AlternateRepresentationStrategy(
            enabled=on("representations"),
            timeout_seconds=configured("representations", 20),
            max_candidates=max_candidates,
        ),
        FeedStrategy(
            enabled=on("rss_atom_jsonfeed"),
            timeout_seconds=configured("rss_atom_jsonfeed", 15),
            max_candidates=max_candidates,
        ),
        PublisherApiStrategy(
            enabled=on("publisher_public_api"),
            timeout_seconds=configured("publisher_public_api", 15),
            max_candidates=max_candidates,
        ),
        StructuredDataStrategy(
            enabled=on("embedded_structured_data"),
            timeout_seconds=configured("embedded_structured_data", 15),
            max_candidates=max_candidates,
        ),
        BrowserDomStrategy(
            adapter=browser_adapter,
            domain_limiter=domain_limiter,
            navigation_enabled=browser_navigation_enabled,
            enabled=on("playwright_chromium_dom"),
            timeout_seconds=configured("playwright_chromium_dom", 45),
        ),
        BrowserNetworkStrategy(
            adapter=browser_adapter,
            domain_limiter=domain_limiter,
            navigation_enabled=browser_navigation_enabled,
            enabled=on("playwright_network_response"),
            timeout_seconds=configured("playwright_network_response", 45),
        ),
        BrowserEmbeddedStateStrategy(
            adapter=browser_adapter,
            domain_limiter=domain_limiter,
            navigation_enabled=browser_navigation_enabled,
            enabled=on("playwright_embedded_state"),
            timeout_seconds=configured("playwright_embedded_state", 45),
        ),
        CapabilityPlaceholderStrategy(
            name="playwright_firefox",
            method=SourceAcquisitionMethod.PLAYWRIGHT_FIREFOX,
            capability=AcquisitionCapabilityState.NOT_IMPLEMENTED,
            reason="browser_engine_not_implemented",
            enabled=on("playwright_firefox"),
        ),
        CapabilityPlaceholderStrategy(
            name="playwright_webkit",
            method=SourceAcquisitionMethod.PLAYWRIGHT_WEBKIT,
            capability=AcquisitionCapabilityState.NOT_IMPLEMENTED,
            reason="browser_engine_not_implemented",
            enabled=on("playwright_webkit"),
        ),
        guarded(
            WaybackSnapshotStrategy(timeout_seconds=configured("wayback_snapshot", 20)),
            "wayback_snapshot" if "wayback_snapshot" in enabled else "wayback",
            20,
        ),
        guarded(
            CommonCrawlWarcStrategy(timeout_seconds=configured("commoncrawl_warc", 25)),
            "commoncrawl_warc" if "commoncrawl_warc" in enabled else "common_crawl",
            25,
        ),
        guarded(
            MementoArchiveStrategy(timeout_seconds=configured("memento_archive", 15)),
            "memento_archive" if "memento_archive" in enabled else "memento",
            15,
        ),
        ArchivePhStrategy(enabled=on("archive_ph"), timeout_seconds=configured("archive_ph", 15)),
        guarded(
            ExactTitleSearchStrategy(
                search_endpoint=exact_title_search_endpoint or None,
                timeout_seconds=configured("exact_title_search", 15),
            ),
            "exact_title_search",
            15,
        ),
        guarded(
            PublisherAlternativeUrlStrategy(
                timeout_seconds=configured("publisher_alternative_url", 15),
                max_candidates=max_candidates,
            ),
            "publisher_alternative_url",
            15,
        ),
        guarded(
            AuthorizedSyndicationStrategy(timeout_seconds=configured("authorized_syndication", 15)),
            "authorized_syndication",
            15,
        ),
        _PublisherPdfAdapter(
            enabled=on("publisher_pdf"),
            timeout_seconds=configured("publisher_pdf", 20),
            min_text_chars=min_text_chars,
            expected_languages=expected_languages,
        ),
        _VisualOcrAdapter(
            engine=ocr_engine,
            enabled=on("visual_ocr"),
            timeout_seconds=configured("visual_ocr", 20),
        ),
        guarded(
            OfficialNewsletterArchiveStrategy(
                archive_endpoint=newsletter_archive_endpoint or None,
                official_domains=newsletter_domains,
                timeout_seconds=configured("official_newsletter_archive", 15),
            ),
            "official_newsletter_archive",
            15,
        ),
        _PolicyAwareStrategy(
            inner=provider,
            configured_enabled=(on("third_party_extraction") and third_party_provider_enabled),
            timeout_seconds=configured("third_party_extraction", 20),
        ),
        BridgeSourceDiscoveryStrategy(
            enabled=on("bridge_source_discovery"),
            timeout_seconds=configured("bridge_source_discovery", 90),
            max_candidates=max_candidates,
        ),
        BridgeTranscriptionStrategy(
            enabled=on("bridge_transcription"),
            timeout_seconds=configured("bridge_transcription", 90),
        ),
    )


def same_publication_variant_urls(
    original_url: str,
    response_body: bytes | None,
    link_header: str | None,
    title: str | None,
    *,
    max_candidates: int = MAX_STRATEGY_CANDIDATES,
) -> tuple[str, ...]:
    """Explicit same-site links first, then common publication representations."""
    result = [
        url
        for url, _method in discover_same_publication_variants(
            original_url=original_url,
            response_body=response_body,
            link_header=link_header,
            max_variants=max_candidates,
        )
    ]
    canonical = canonicalize_http_url(original_url)
    parsed = urlsplit(canonical)
    path = parsed.path or "/"
    candidates = [
        urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/") + "/amp/", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, path.rstrip("/") + "/print/", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, path, "output=amp", "")),
        urlunsplit((parsed.scheme, parsed.netloc, path, "amp=1", "")),
        urlunsplit((parsed.scheme, parsed.netloc, path, "output=txt", "")),
        urlunsplit((parsed.scheme, parsed.netloc, path, "format=text", "")),
        urlunsplit((parsed.scheme, parsed.netloc, "/feed/", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, "/rss.xml", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, "/feed.xml", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, "/feed.json", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, "/atom.xml", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, "/text/", "", "")),
        urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/wp-json/wp/v2/posts",
                urlencode({"slug": path.rstrip("/").split("/")[-1]}),
                "",
            )
        ),
        urlunsplit(
            (
                parsed.scheme,
                parsed.netloc,
                "/wp-json/oembed/1.0/embed",
                urlencode({"url": canonical, "format": "json"}),
                "",
            )
        ),
        urlunsplit((parsed.scheme, parsed.netloc, "/sitemap.xml", "", "")),
        urlunsplit((parsed.scheme, parsed.netloc, "/news-sitemap.xml", "", "")),
    ]
    for url in candidates:
        try:
            normalized = canonicalize_http_url(url)
        except ValueError:
            continue
        if normalized != canonical and normalized not in result:
            result.append(normalized)
        if len(result) >= max_candidates:
            break
    return tuple(result[:max_candidates])


def sitemap_article_urls(
    body: bytes,
    *,
    original_url: str,
    title: str | None,
    max_candidates: int,
) -> tuple[str, ...]:
    """Return same-site sitemap entries that match the original slug or title."""
    text = body.decode("utf-8", errors="replace")
    original = urlsplit(canonicalize_http_url(original_url))
    title_words = {word.casefold() for word in _WORD_RE.findall(title or "") if len(word) >= 4}
    slug_words = {
        word.casefold()
        for word in _WORD_RE.findall(original.path.replace("-", " "))
        if len(word) >= 4
    }
    expected = title_words or slug_words
    found: list[str] = []
    for raw_url in re.findall(r"(?is)<loc>\s*([^<]+?)\s*</loc>", text):
        try:
            candidate = canonicalize_http_url(raw_url.strip())
            parsed = urlsplit(candidate)
        except ValueError:
            continue
        if (parsed.hostname or "").casefold() != (original.hostname or "").casefold():
            continue
        candidate_words = {
            word.casefold()
            for word in _WORD_RE.findall(parsed.path.replace("-", " "))
            if len(word) >= 4
        }
        if expected.intersection(candidate_words):
            found.append(candidate)
        if len(found) >= max_candidates:
            break
    return tuple(dict.fromkeys(found))


def archive_url_variants(original_url: str) -> tuple[str, ...]:
    canonical = canonicalize_http_url(original_url)
    parsed = urlsplit(canonical)
    path = re.sub(r"(?i)(?:utm_[^=&]*|fbclid|gclid)=[^&]*&?", "", parsed.query).strip("&")
    base = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, path, ""))
    host = parsed.hostname or ""
    variants = [base, base.rstrip("/") if parsed.path != "/" else base]
    if host.startswith("www."):
        variants.append(urlunsplit((parsed.scheme, host[4:], parsed.path, path, "")))
    else:
        variants.append(urlunsplit((parsed.scheme, f"www.{host}", parsed.path, path, "")))
    if parsed.scheme == "https":
        variants.append(urlunsplit(("http", parsed.netloc, parsed.path, path, "")))
    return tuple(dict.fromkeys(variants))


def retry_after_seconds(value: str | None, *, retry: int) -> float:
    fallback: float = min(0.25 * (2**retry), MAX_RETRY_AFTER_SECONDS)
    if not value:
        return fallback
    try:
        return max(0.0, min(float(value.strip()), MAX_RETRY_AFTER_SECONDS))
    except ValueError:
        try:
            from email.utils import parsedate_to_datetime

            when = parsedate_to_datetime(value)
            if when.tzinfo is None:
                when = when.replace(tzinfo=UTC)
            return max(
                0.0, min((when - datetime.now(UTC)).total_seconds(), MAX_RETRY_AFTER_SECONDS)
            )
        except (TypeError, ValueError, OverflowError):
            return fallback


def _metadata_probe(
    probe: AcquisitionProbe,
    *,
    response: CollectedResponse,
    outcome: str,
    reason_code: str | None,
) -> AcquisitionProbe:
    return AcquisitionProbe(
        strategy_name=probe.strategy_name,
        strategy_version=probe.strategy_version,
        method=probe.method,
        requested_url=probe.requested_url,
        outcome=_typed_probe_outcome(outcome),
        reason_code=reason_code,
        response=response,
        http_status=response.status,
        encoded_size=response.encoded_size,
        encoded_sha256=response.encoded_sha256,
        duration_ms=probe.duration_ms,
    )


def _empty_probe(
    name: str,
    version: str,
    method: SourceAcquisitionMethod,
    url: str,
    reason: str,
) -> AcquisitionProbe:
    return AcquisitionProbe(
        strategy_name=name,
        strategy_version=version,
        method=method,
        requested_url=url,
        outcome=_typed_probe_outcome("skipped"),
        reason_code=reason,
        response=None,
        http_status=None,
        encoded_size=None,
        encoded_sha256=None,
        duration_ms=0,
    )


def _wayback_snapshot(body: bytes, *, original_url: str, published_at: date | None) -> str | None:
    try:
        rows = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list) or len(rows) < 2:
        return None
    target = (published_at or datetime.now(UTC).date()).strftime("%Y%m%d")
    records: list[tuple[str, str]] = []
    for row in rows[1:]:
        if isinstance(row, list) and len(row) >= 2 and re.fullmatch(r"\d{14}", str(row[0])):
            records.append((str(row[0]), str(row[1])))
    if not records:
        return None
    timestamp, original = min(records, key=lambda row: abs(int(row[0][:8]) - int(target)))
    if canonicalize_http_url(original) != canonicalize_http_url(original_url):
        return None
    return f"https://web.archive.org/web/{timestamp}id_/{canonicalize_http_url(original)}"


def _latest_common_crawl_index(body: bytes) -> str | None:
    try:
        rows = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    ids = [
        str(row.get("id", ""))
        for row in rows
        if isinstance(row, dict) and re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", str(row.get("id", "")))
    ]
    return max(ids) if ids else None


def _common_crawl_record(body: bytes) -> tuple[int, int, str] | None:
    try:
        row = json.loads(body.splitlines()[0])
        offset = int(row["offset"])
        length = int(row["length"])
        filename = str(row["filename"])
    except (IndexError, KeyError, TypeError, ValueError, UnicodeError, json.JSONDecodeError):
        return None
    if offset < 0 or length < 1 or length > 10 * 1024 * 1024:
        return None
    if not filename.startswith("crawl-data/") or ".." in filename.split("/"):
        return None
    return offset, length, filename


def _warc_article_response(
    response: CollectedResponse, requested_url: str
) -> CollectedResponse | None:
    body = response.decoded_body
    try:
        record = gzip.decompress(body) if body.startswith(b"\x1f\x8b") else body
    except (OSError, EOFError):
        return None
    header_end = record.find(b"\r\n\r\n")
    if header_end < 0 or not record.startswith(b"WARC/1."):
        return None
    warc_headers = record[:header_end].decode("latin-1", errors="replace").casefold()
    if "warc-type: response" not in warc_headers:
        return None
    http_start = record.find(b"HTTP/1.", header_end)
    if http_start < 0:
        return None
    http_header_end = record.find(b"\r\n\r\n", http_start)
    if http_header_end < 0:
        return None
    article = record[http_header_end + 4 :].rstrip(b"\r\n")
    content_type_match = re.search(
        rb"(?im)^content-type:\s*([^\r\n]+)", record[http_start:http_header_end]
    )
    content_type = (
        content_type_match.group(1).decode("latin-1") if content_type_match else "text/html"
    )
    digest = hashlib.sha256(article).hexdigest()
    return CollectedResponse(
        requested_url=requested_url,
        final_url=requested_url,
        redirect_chain=(),
        status=200,
        headers={"content-type": content_type},
        declared_content_type=content_type.split(";", 1)[0],
        detected_content_type=_detect_mime(article, allow_images=False, allow_binary=False),
        encoded_body=article,
        decoded_body=article,
        encoded_size=len(article),
        encoded_sha256=digest,
        decoded_size=len(article),
        decoded_sha256=digest,
        content_encoding="identity",
        acquired_at=datetime.now(UTC),
    )


def _memento_url(body: bytes) -> str | None:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    candidates: list[tuple[str, str]] = []

    def walk(value: object) -> None:
        if isinstance(value, dict):
            url = value.get("uri") or value.get("url")
            rel = value.get("rel", "")
            if isinstance(url, str) and isinstance(rel, (str, list)):
                relation = " ".join(rel) if isinstance(rel, list) else rel
                candidates.append((url, relation.casefold()))
            for nested in value.values():
                walk(nested)
        elif isinstance(value, list):
            for nested in value:
                walk(nested)

    walk(document)
    return next((url for url, rel in candidates if "memento" in rel), None)
