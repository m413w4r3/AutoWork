"""Publisher, search, syndication, PDF, OCR and provider recovery strategies."""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, date, datetime
from difflib import SequenceMatcher
from html.parser import HTMLParser
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

from cti_app.application.acquisition_fallbacks import (
    AcquisitionProbe,
    AcquisitionRequest,
    article_rejection_reason,
)
from cti_app.application.http_collection import (
    CollectedResponse,
    CollectionError,
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
    is_credential_free_url,
)
from cti_app.domain.collection import DetectedMimeType, SourceAcquisitionMethod
from cti_app.domain.discovery import canonicalize_http_url

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MULTI_LABEL_PUBLIC_SUFFIXES = frozenset(
    {
        "co.uk",
        "org.uk",
        "ac.uk",
        "gov.uk",
        "com.au",
        "net.au",
        "org.au",
        "co.jp",
        "ne.jp",
        "co.nz",
        "com.br",
        "com.cn",
        "com.sg",
        "co.in",
        "co.za",
        "com.mx",
        "com.tr",
        "com.tw",
        "com.hk",
        "com.ar",
    }
)
_MAX_PAGE_BYTES = 20 * 1024 * 1024
_TITLE_MATCH_THRESHOLD = 0.76
_STRONG_EQUIVALENCE_THRESHOLD = 0.86


class _PageParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.links: list[tuple[str, frozenset[str], str | None]] = []
        self.anchors: list[tuple[str, str]] = []
        self.json_ld: list[str] = []
        self._in_title = False
        self._in_anchor = False
        self._anchor_href = ""
        self._anchor_text: list[str] = []
        self._in_json_ld = False
        self._json_ld_text: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = True
        elif lowered == "link":
            rel = frozenset(values.get("rel", "").casefold().split())
            href = values.get("href", "").strip()
            if href and rel.intersection({"canonical", "alternate"}):
                self.links.append((href, rel, values.get("type")))
        elif lowered == "a":
            self._in_anchor = True
            self._anchor_href = values.get("href", "").strip()
            self._anchor_text = []
            rel = frozenset(values.get("rel", "").casefold().split())
            if self._anchor_href and rel.intersection({"canonical", "alternate"}):
                self.links.append((self._anchor_href, rel, values.get("type")))
        elif lowered == "script" and values.get("type", "").casefold() == "application/ld+json":
            self._in_json_ld = True
            self._json_ld_text = []

    def handle_endtag(self, tag: str) -> None:
        lowered = tag.casefold()
        if lowered == "title":
            self._in_title = False
        elif lowered == "a" and self._in_anchor:
            if self._anchor_href:
                self.anchors.append((self._anchor_href, " ".join(self._anchor_text).strip()))
            self._in_anchor = False
        elif lowered == "script" and self._in_json_ld:
            self.json_ld.append("".join(self._json_ld_text))
            self._in_json_ld = False

    def handle_data(self, data: str) -> None:
        if self._in_title:
            self.title.append(data)
        if self._in_anchor:
            self._anchor_text.append(data)
        if self._in_json_ld:
            self._json_ld_text.append(data)


def _parse_page(body: bytes | None) -> _PageParser:
    parser = _PageParser()
    if body:
        try:
            parser.feed(body.decode("utf-8-sig", errors="replace"))
        except Exception:
            pass
    return parser


def _normalized_title(value: str | None) -> str:
    return " ".join(word.casefold() for word in _WORD_RE.findall(value or ""))


def _title_similarity(expected: str | None, actual: str | None) -> float:
    left = _normalized_title(expected)
    right = _normalized_title(actual)
    if not left:
        return 1.0
    if not right:
        return 0.0
    sequence = SequenceMatcher(None, left, right, autojunk=False).ratio()
    left_words, right_words = set(left.split()), set(right.split())
    overlap = len(left_words & right_words) / max(1, len(left_words | right_words))
    return max(sequence, overlap)


def _strong_title_match(expected: str | None, actual: str | None) -> bool:
    return bool(
        expected and actual and _title_similarity(expected, actual) >= _TITLE_MATCH_THRESHOLD
    )


def _extracted_text_title_similarity(expected: str | None, text: str) -> float:
    if not expected:
        return 1.0
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    candidates = lines[:12] or [text[:240]]
    return max(
        (_title_similarity(expected, candidate[:240]) for candidate in candidates), default=0.0
    )


def _visible_text(body: bytes) -> str:
    text = body.decode("utf-8-sig", errors="replace")
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", text)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", text).split())


def _quality(
    body: bytes, title: str | None, *, reasons: tuple[str, ...] = ()
) -> AcquisitionQuality:
    visible = _visible_text(body)
    parser = _parse_page(body)
    actual_title = " ".join(parser.title).strip()
    if not actual_title and visible:
        actual_title = visible.splitlines()[0][:240]
    similarity = _title_similarity(title, actual_title) if title else 1.0
    return AcquisitionQuality(
        text_chars=len(visible),
        word_count=len(_WORD_RE.findall(visible)),
        title_similarity=similarity,
        language=None,
        boilerplate_ratio=0.0,
        duplicate_ratio=0.0,
        truncated=False,
        challenge_detected=False,
        paywall_detected=False,
        score=min(1.0, len(visible) / 1000.0),
        reasons=reasons,
    )


def _safe_http_url(url: str) -> bool:
    try:
        parsed = urlsplit(url)
        _ = parsed.port
        return (
            parsed.scheme.casefold() in {"http", "https"}
            and bool(parsed.hostname)
            and parsed.username is None
            and parsed.password is None
            and is_credential_free_url(url)
        )
    except (AttributeError, TypeError, ValueError):
        return False


def _host(url: str) -> str | None:
    try:
        host = urlsplit(url).hostname
        return host.encode("idna").decode("ascii").casefold().rstrip(".") if host else None
    except (AttributeError, UnicodeError, ValueError):
        return None


def _registrable_domain(url: str) -> str | None:
    hostname = _host(url)
    if hostname is None:
        return None
    labels = hostname.split(".")
    if len(labels) < 2:
        return hostname
    suffix2 = ".".join(labels[-2:])
    count = 3 if suffix2 in _MULTI_LABEL_PUBLIC_SUFFIXES and len(labels) >= 3 else 2
    return ".".join(labels[-count:])


def _same_publisher(left: str, right: str) -> bool:
    left_domain, right_domain = _registrable_domain(left), _registrable_domain(right)
    return left_domain is not None and left_domain == right_domain


def classify_equivalence(
    original_url: str,
    candidate_url: str,
    *,
    explicit_syndication: bool = False,
    same_publisher: bool = False,
    title_similarity: float = 0.0,
) -> SourceEquivalenceClass:
    """Classify a candidate without promoting a different URL to exact original."""
    try:
        original = canonicalize_http_url(original_url)
        candidate = canonicalize_http_url(candidate_url)
    except (TypeError, ValueError):
        return SourceEquivalenceClass.UNKNOWN_MATCH
    if original == candidate:
        return SourceEquivalenceClass.EXACT_ORIGINAL
    if not 0.0 <= title_similarity <= 1.0:
        return SourceEquivalenceClass.UNKNOWN_MATCH
    if title_similarity < _TITLE_MATCH_THRESHOLD:
        return SourceEquivalenceClass.UNKNOWN_MATCH
    if explicit_syndication and title_similarity >= _TITLE_MATCH_THRESHOLD:
        return SourceEquivalenceClass.AUTHORIZED_REPUBLICATION
    if same_publisher and title_similarity >= _TITLE_MATCH_THRESHOLD:
        return SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
    if not same_publisher and title_similarity >= _STRONG_EQUIVALENCE_THRESHOLD:
        return SourceEquivalenceClass.INDEPENDENT_CORROBORATION
    return SourceEquivalenceClass.UNKNOWN_MATCH


def _evidence_for_equivalence(equivalence: SourceEquivalenceClass) -> EvidenceClass:
    if equivalence is SourceEquivalenceClass.EXACT_ORIGINAL:
        return EvidenceClass.PRIMARY_OBSERVED
    if equivalence is SourceEquivalenceClass.HISTORICAL_ORIGINAL:
        return EvidenceClass.HISTORICAL_PRIMARY
    if equivalence is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT:
        return EvidenceClass.VERIFIED_EQUIVALENT
    if equivalence is SourceEquivalenceClass.AUTHORIZED_REPUBLICATION:
        return EvidenceClass.AUTHORIZED_REPUBLICATION
    if equivalence is SourceEquivalenceClass.INDEPENDENT_CORROBORATION:
        return EvidenceClass.INDEPENDENT_CORROBORATION
    return EvidenceClass.UNKNOWN


def safe_external_url(url: str) -> str:
    """Return a credential-free origin and safe path, with query values removed."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return "invalid-url"
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        default_port = 443 if parsed.scheme.casefold() == "https" else 80
        port_text = f":{port}" if port is not None and port != default_port else ""
        path = parsed.path or "/"
        safe_path_url = urlunsplit((parsed.scheme.casefold(), hostname, path, "", ""))
        if not is_credential_free_url(safe_path_url) or re.search(
            r"(?i)(?:token|secret|signature|auth|api[_-]?key|password|credential)", path
        ):
            path = "/"
        return f"{parsed.scheme.casefold()}://{hostname}{port_text}{path}"
    except (AttributeError, UnicodeError, TypeError, ValueError):
        return "invalid-url"


def external_payload_is_allowed(policy: AcquisitionPolicyContext, target: str) -> bool:
    """Apply provider disclosure policy to a credential-free public target."""
    if not _safe_http_url(target):
        return False
    return (
        policy.check_destination(target, third_party_provider=True)
        is AcquisitionPolicyDecision.ALLOWED
    )


def _probe(
    *,
    name: str,
    version: str,
    method: SourceAcquisitionMethod,
    url: str,
    outcome: AcquisitionOutcome,
    reason: str | None = None,
    response: CollectedResponse | None = None,
    duration_ms: int = 0,
    capability: AcquisitionCapabilityState = AcquisitionCapabilityState.IMPLEMENTED,
    policy_decision: AcquisitionPolicyDecision = AcquisitionPolicyDecision.ALLOWED,
    equivalence: SourceEquivalenceClass | None = None,
    evidence: EvidenceClass | None = None,
    observation: AcquisitionObservationKind = AcquisitionObservationKind.HTTP_RESPONSE,
    quality: AcquisitionQuality | None = None,
    archive_snapshot_time: datetime | None = None,
    publication_date: date | None = None,
    publisher: str | None = None,
) -> AcquisitionProbe:
    return AcquisitionProbe(
        strategy_name=name,
        strategy_version=version,
        method=method,
        requested_url=url,
        outcome=outcome,
        reason_code=reason,
        response=response,
        http_status=response.status if response is not None else None,
        encoded_size=response.encoded_size if response is not None else None,
        encoded_sha256=response.encoded_sha256 if response is not None else None,
        duration_ms=duration_ms,
        capability_state=capability,
        policy_decision=policy_decision,
        equivalence_class=equivalence,
        evidence_class=evidence,
        observation_kind=observation,
        quality=quality,
        archive_snapshot_time=archive_snapshot_time,
        publication_date=publication_date,
        publisher=publisher,
    )


def _attempt_key(name: str, url: str) -> str:
    return hashlib.sha256(f"{name}\0{url}".encode()).hexdigest()


async def _fetch(
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
        return None, _probe(
            name=name,
            version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.SKIPPED,
            reason="already_attempted_for_strategy",
        )
    started = time.monotonic()
    try:
        response = await request.fetch(url, headers or {}, timeout_seconds, allow_binary)
    except CollectionError as error:
        return None, _probe(
            name=name,
            version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.FAILED,
            reason=getattr(error.reason_code, "value", "collection_failed"),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except TimeoutError:
        return None, _probe(
            name=name,
            version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.FAILED,
            reason="timeout",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except Exception:
        return None, _probe(
            name=name,
            version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.FAILED,
            reason="fetch_failed",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    return response, None


def _page_title(body: bytes) -> str:
    return " ".join(" ".join(_parse_page(body).title).split())


def _validate_page(
    response: CollectedResponse,
    request: AcquisitionRequest,
    *,
    expected_candidate_title: str | None = None,
    min_text_chars: int | None = None,
) -> tuple[str | None, float]:
    if response.status != 200:
        return "article_http_status_not_200", 0.0
    content_type = (response.declared_content_type or "").casefold().split(";", 1)[0]
    if content_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/json"}:
        return "unsupported_article_content_type", 0.0
    reason = article_rejection_reason(
        body=response.decoded_body,
        content_type=response.declared_content_type,
        requested_url=request.requested_url,
        expected_title=request.title,
        min_text_chars=request.min_text_chars if min_text_chars is None else min_text_chars,
        headers=response.headers,
        expected_languages=request.expected_languages,
    )
    title = _page_title(response.decoded_body) or expected_candidate_title
    similarity = _title_similarity(request.title, title)
    if reason is not None:
        return reason, similarity
    if request.title and not _strong_title_match(request.title, title):
        return "title_mismatch", similarity
    return None, similarity


def _sitemap_entries(body: bytes, *, original_url: str, title: str | None) -> tuple[str, ...]:
    try:
        root = ElementTree.fromstring(body)
    except (ElementTree.ParseError, ValueError):
        return ()
    original = urlsplit(original_url)
    expected_slug = _slug(original.path)
    normalized_expected_title = _normalized_title(title)
    output: list[str] = []
    for node in root.iter():
        if node.tag.rsplit("}", 1)[-1].casefold() != "url":
            continue
        location: str | None = None
        entry_title: str | None = None
        for child in node.iter():
            local = child.tag.rsplit("}", 1)[-1].casefold()
            value = (child.text or "").strip()
            if local == "loc":
                location = value
            elif local == "title":
                entry_title = value
        if (
            not location
            or not _safe_http_url(location)
            or not _same_publisher(original_url, location)
        ):
            continue
        candidate_slug = _slug(urlsplit(location).path)
        slug_match = bool(expected_slug and candidate_slug == expected_slug)
        title_match = bool(
            normalized_expected_title
            and _normalized_title(entry_title) == normalized_expected_title
        )
        if slug_match or title_match:
            output.append(location)
    return tuple(dict.fromkeys(output))


def _slug(path: str) -> str:
    leaf = path.rstrip("/").rsplit("/", 1)[-1]
    return re.sub(r"(?i)\.(?:html?|php|amp)$", "", leaf).casefold()


def _same_publisher_candidate(request: AcquisitionRequest, candidate: str) -> bool:
    return _same_publisher(request.requested_url, candidate)


def _metadata_candidates(body: bytes | None, request: AcquisitionRequest) -> tuple[str, ...]:
    parser = _parse_page(body)
    candidates: list[str] = []
    for href, _rel, _kind in parser.links:
        candidate = urljoin(request.requested_url, href)
        if _safe_http_url(candidate) and _same_publisher_candidate(request, candidate):
            try:
                if canonicalize_http_url(candidate) != canonicalize_http_url(request.requested_url):
                    candidates.append(candidate)
            except ValueError:
                pass
    return tuple(dict.fromkeys(candidates))


@dataclass(frozen=True, slots=True)
class PublisherAlternativeUrlStrategy:
    name: str = "publisher_alternative_url"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PUBLISHER_ALTERNATIVE_URL
    enabled: bool = True
    timeout_seconds: float = 15.0
    max_candidates: int = 12

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return AcquisitionCapabilityState.IMPLEMENTED

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        if not request.title:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="expected_title_missing",
                ),
            )
        parsed = urlsplit(request.requested_url)
        if not parsed.hostname or not _safe_http_url(request.requested_url):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="invalid_original_url",
                ),
            )
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        root_domain = _registrable_domain(request.requested_url)
        origins = [origin]
        if root_domain and root_domain != parsed.hostname.casefold():
            origins.append(urlunsplit((parsed.scheme, root_domain, "", "", "")))
        sitemap_urls = tuple(
            f"{sitemap_origin}/{path}"
            for sitemap_origin in dict.fromkeys(origins)
            for path in ("sitemap.xml", "news-sitemap.xml")
        )
        candidates = list(_metadata_candidates(request.response_body, request))
        probes: list[AcquisitionProbe] = []
        seen = set(candidates)
        for sitemap_url in sitemap_urls:
            sitemap, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=sitemap_url,
                timeout_seconds=self.timeout_seconds,
            )
            if error is not None:
                probes.append(error)
                continue
            assert sitemap is not None
            if sitemap.status != 200 or (sitemap.declared_content_type or "").split(";", 1)[
                0
            ].casefold() not in {
                "application/xml",
                "text/xml",
                "application/rss+xml",
                "application/atom+xml",
                "text/plain",
                "text/html",
                "",
            }:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=sitemap_url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="invalid_sitemap_response",
                        response=sitemap,
                    )
                )
                continue
            for candidate in _sitemap_entries(
                sitemap.decoded_body, original_url=request.requested_url, title=request.title
            ):
                if candidate not in seen:
                    candidates.append(candidate)
                    seen.add(candidate)
        for candidate in candidates[: max(1, min(self.max_candidates, 24))]:
            try:
                if canonicalize_http_url(candidate) == canonicalize_http_url(request.requested_url):
                    continue
            except ValueError:
                continue
            if not _same_publisher_candidate(request, candidate):
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="different_publisher_domain",
                    )
                )
                continue
            response, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=candidate,
                timeout_seconds=self.timeout_seconds,
            )
            if error is not None:
                probes.append(error)
                continue
            assert response is not None
            reason, similarity = _validate_page(response, request)
            if reason is not None:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=reason,
                        response=response,
                    )
                )
                continue
            equivalence = classify_equivalence(
                request.requested_url,
                candidate,
                same_publisher=True,
                title_similarity=similarity,
            )
            probes.append(
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=candidate,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=response,
                    equivalence=equivalence,
                    evidence=(
                        EvidenceClass.VERIFIED_EQUIVALENT
                        if equivalence is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
                        else EvidenceClass.UNKNOWN
                    ),
                    quality=_quality(response.decoded_body, request.title),
                    publisher=None,
                )
            )
            break
        if not probes:
            probes.append(
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="no_matching_publisher_alternative",
                )
            )
        return tuple(probes)


def _result_rows(document: object) -> list[dict[str, object]]:
    if isinstance(document, list):
        return [row for row in document if isinstance(row, dict)]
    if isinstance(document, dict):
        for key in ("results", "items"):
            rows = document.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
    return []


def _search_results(
    body: bytes, content_type: str | None, request: AcquisitionRequest
) -> tuple[tuple[str, str], ...]:
    results: list[tuple[str, str]] = []
    if "json" in (content_type or "").casefold() or body.lstrip().startswith((b"{", b"[")):
        try:
            document = json.loads(body)
        except (UnicodeError, json.JSONDecodeError):
            document = None
        for row in _result_rows(document):
            url, title = row.get("url"), row.get("title")
            if isinstance(url, str) and isinstance(title, str):
                results.append((urljoin(request.requested_url, url), title))
    else:
        parser = _parse_page(body)
        results.extend(
            (urljoin(request.requested_url, href), title) for href, title in parser.anchors
        )
    return tuple(dict.fromkeys(results))


@dataclass(frozen=True, slots=True)
class ExactTitleSearchStrategy:
    search_endpoint: str | None = None
    name: str = "exact_title_search"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.EXACT_TITLE_SEARCH
    timeout_seconds: float = 15.0
    query_parameter: str = "q"
    max_candidates: int = 8

    @property
    def enabled(self) -> bool:
        return bool(self.search_endpoint)

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.search_endpoint
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        endpoint = self.search_endpoint
        if not endpoint:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.NOT_CONFIGURED,
                    reason="search_endpoint_not_configured",
                    capability=AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION,
                ),
            )
        if not request.title:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=endpoint,
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="expected_title_missing",
                ),
            )
        if not _safe_http_url(endpoint):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=endpoint,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="invalid_search_endpoint",
                ),
            )
        split = urlsplit(endpoint)
        query = parse_qsl(split.query, keep_blank_values=True)
        query.append((self.query_parameter, request.title))
        search_url = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), ""))
        listing, error = await _fetch(
            request,
            name=self.name,
            version=self.version,
            method=self.method,
            url=search_url,
            timeout_seconds=self.timeout_seconds,
            headers={"accept": "application/json, text/html;q=0.9"},
        )
        if error is not None:
            return (error,)
        assert listing is not None
        if listing.status != 200:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=search_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason="search_http_status_not_200",
                    response=listing,
                ),
            )
        probes = [
            _probe(
                name=self.name,
                version=self.version,
                method=self.method,
                url=search_url,
                outcome=AcquisitionOutcome.LOOKUP_SUCCEEDED,
                response=listing,
            )
        ]
        candidates = _search_results(listing.decoded_body, listing.declared_content_type, request)
        seen: set[str] = set()
        for candidate, title in candidates:
            if candidate in seen or not _safe_http_url(candidate):
                continue
            seen.add(candidate)
            if not _strong_title_match(request.title, title):
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="search_result_title_mismatch",
                    )
                )
                continue
            if request.policy.check_url(candidate) is not AcquisitionPolicyDecision.ALLOWED:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.POLICY_SKIPPED,
                        reason="search_result_url_policy_denied",
                        policy_decision=request.policy.check_url(candidate),
                    )
                )
                continue
            page, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=candidate,
                timeout_seconds=self.timeout_seconds,
            )
            if error is not None:
                probes.append(error)
                continue
            assert page is not None
            reason, similarity = _validate_page(page, request, expected_candidate_title=title)
            if reason is not None:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=reason,
                        response=page,
                    )
                )
                continue
            same_publisher = _same_publisher_candidate(request, candidate)
            equivalence = classify_equivalence(
                request.requested_url,
                candidate,
                same_publisher=same_publisher,
                title_similarity=similarity,
            )
            probes.append(
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=candidate,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=page,
                    equivalence=equivalence,
                    evidence=_evidence_for_equivalence(equivalence),
                    quality=_quality(page.decoded_body, request.title),
                    publisher=None,
                )
            )
            break
        return tuple(probes)


def _json_ld_values(parser: _PageParser) -> list[object]:
    values: list[object] = []
    for text in parser.json_ld:
        try:
            values.append(json.loads(text))
        except (json.JSONDecodeError, TypeError):
            continue
    return values


def _points_to_original(body: bytes, original_url: str) -> bool:
    parser = _parse_page(body)
    try:
        original = canonicalize_http_url(original_url)
    except ValueError:
        return False
    for href, rel, _kind in parser.links:
        if rel.intersection({"canonical", "alternate"}):
            candidate = urljoin(original_url, href)
            try:
                if canonicalize_http_url(candidate) == original:
                    return True
            except ValueError:
                continue
    found = False

    def visit(value: object) -> None:
        nonlocal found
        if isinstance(value, dict):
            for key, nested in value.items():
                if key.casefold() in {"isbasedon", "sameas"}:
                    candidates = nested if isinstance(nested, list) else [nested]
                    for candidate in candidates:
                        url = candidate.get("url") if isinstance(candidate, dict) else candidate
                        if isinstance(url, str):
                            try:
                                if canonicalize_http_url(url) == original:
                                    found = True
                            except ValueError:
                                pass
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    for value in _json_ld_values(parser):
        visit(value)
    return found


@dataclass(frozen=True, slots=True)
class AuthorizedSyndicationStrategy:
    candidate_urls: tuple[str, ...] = ()
    name: str = "authorized_syndication"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.AUTHORIZED_SYNDICATION
    enabled: bool = True
    timeout_seconds: float = 15.0

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return AcquisitionCapabilityState.IMPLEMENTED

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        parser = _parse_page(request.response_body)
        candidates = list(self.candidate_urls)
        for href, rel, _kind in parser.links:
            if rel.intersection({"canonical", "alternate"}):
                candidates.append(urljoin(request.requested_url, href))
        for href, _text in parser.anchors:
            linked = urljoin(request.requested_url, href)
            linked_host = _host(linked) or ""
            if any(
                linked_host == domain or linked_host.endswith(f".{domain}")
                for domain in request.policy.allowed_syndication_domains
            ):
                candidates.append(linked)

        def add_json_urls(value: object) -> None:
            if isinstance(value, dict):
                for key, nested in value.items():
                    if key.casefold() in {"isbasedon", "sameas"}:
                        for item in nested if isinstance(nested, list) else [nested]:
                            url = item.get("url") if isinstance(item, dict) else item
                            if isinstance(url, str):
                                candidates.append(urljoin(request.requested_url, url))
                    add_json_urls(nested)
            elif isinstance(value, list):
                for item in value:
                    add_json_urls(item)

        for document in _json_ld_values(parser):
            add_json_urls(document)
        configured_domains = request.policy.allowed_syndication_domains
        try:
            original = canonicalize_http_url(request.requested_url)
        except ValueError:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(request.requested_url),
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="invalid_original_url",
                ),
            )
        eligible_candidates: list[str] = []
        for candidate in candidates:
            if not _safe_http_url(candidate):
                continue
            try:
                if canonicalize_http_url(candidate) == original:
                    continue
            except ValueError:
                continue
            eligible_candidates.append(candidate)
        unique = tuple(dict.fromkeys(eligible_candidates))
        if not unique:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="no_explicit_syndication_candidate",
                ),
            )
        probes: list[AcquisitionProbe] = []
        for candidate in unique:
            response, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=candidate,
                timeout_seconds=self.timeout_seconds,
            )
            if error is not None:
                probes.append(error)
                continue
            assert response is not None
            reason, similarity = _validate_page(response, request)
            if reason is not None:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=reason,
                        response=response,
                    )
                )
                continue
            host = _host(candidate) or ""
            domain_configured = any(
                host == domain or host.endswith(f".{domain}") for domain in configured_domains
            )
            explicit = domain_configured or _points_to_original(
                response.decoded_body, request.requested_url
            )
            equivalence = (
                classify_equivalence(
                    request.requested_url,
                    candidate,
                    explicit_syndication=True,
                    same_publisher=_same_publisher_candidate(request, candidate),
                    title_similarity=similarity,
                )
                if explicit
                else SourceEquivalenceClass.UNKNOWN_MATCH
            )
            evidence = _evidence_for_equivalence(equivalence)
            probes.append(
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=candidate,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=response,
                    equivalence=equivalence,
                    evidence=evidence,
                    quality=_quality(response.decoded_body, request.title),
                    publisher=None,
                )
            )
            break
        return tuple(probes)


@dataclass(frozen=True, slots=True)
class CapturedArtifact:
    """Already-captured artifact supplied by the caller to OCR; never fetched here."""

    encoded_body: bytes
    content_type: str
    source_url: str
    sha256: str | None = None

    def digest(self) -> str:
        actual = hashlib.sha256(self.encoded_body).hexdigest()
        if self.sha256 is not None and self.sha256.casefold() != actual:
            raise ValueError("artifact_hash_mismatch")
        return actual


class OcrEngine(Protocol):
    async def recognize(self, artifact: CapturedArtifact) -> str: ...


@dataclass(frozen=True, slots=True)
class PdfArtifact:
    encoded_body: bytes
    extracted_text: str
    source_url: str
    content_type: str = "application/pdf"


PdfTextExtractor = Callable[[bytes], str | Awaitable[str]]


def _derived_response(
    source: CollectedResponse,
    *,
    encoded: bytes,
    decoded: bytes,
    declared_content_type: str | None,
    detected_content_type: DetectedMimeType,
) -> CollectedResponse:
    encoded_hash = hashlib.sha256(encoded).hexdigest()
    decoded_hash = hashlib.sha256(decoded).hexdigest()
    return replace(
        source,
        declared_content_type=declared_content_type,
        detected_content_type=detected_content_type,
        encoded_body=encoded,
        decoded_body=decoded,
        encoded_size=len(encoded),
        encoded_sha256=encoded_hash,
        decoded_size=len(decoded),
        decoded_sha256=decoded_hash,
    )


@dataclass(frozen=True, slots=True)
class PublisherPdfStrategy:
    extractor: PdfTextExtractor | None = None
    name: str = "publisher_pdf"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.PUBLISHER_PDF
    enabled: bool = True
    timeout_seconds: float = 20.0
    max_pdf_bytes: int = _MAX_PAGE_BYTES

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.extractor
            else AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE
        )

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        extractor = self.extractor
        if extractor is None:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.NOT_CONFIGURED,
                    reason="pdf_extractor_not_configured",
                    capability=AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE,
                ),
            )
        parser = _parse_page(request.response_body)
        links: list[str] = []
        for href, _rel, kind in parser.links:
            if kind and kind.casefold().split(";", 1)[0].strip() == "application/pdf":
                links.append(urljoin(request.requested_url, href))
        for href, _text in parser.anchors:
            if urlsplit(href).path.casefold().endswith(".pdf"):
                links.append(urljoin(request.requested_url, href))
        candidates = tuple(
            url
            for url in dict.fromkeys(links)
            if _safe_http_url(url) and _same_publisher_candidate(request, url)
        )
        if not candidates:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="publisher_pdf_link_not_found",
                ),
            )
        probes: list[AcquisitionProbe] = []
        max_bytes = max(1, min(self.max_pdf_bytes, _MAX_PAGE_BYTES))
        for candidate in candidates:
            response, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=candidate,
                timeout_seconds=self.timeout_seconds,
                allow_binary=True,
            )
            if error is not None:
                probes.append(error)
                continue
            assert response is not None
            raw_pdf = response.decoded_body
            if len(raw_pdf) > max_bytes:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="pdf_exceeds_parse_limit",
                        response=response,
                    )
                )
                continue
            if not raw_pdf.startswith(b"%PDF-") or b"%%EOF" not in raw_pdf[-2048:]:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="invalid_pdf_magic_or_trailer",
                        response=response,
                    )
                )
                continue
            try:
                extraction = extractor(raw_pdf)
                extracted = extraction if isinstance(extraction, str) else await extraction
            except Exception:
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="pdf_parse_failed",
                        response=response,
                    )
                )
                continue
            if not isinstance(extracted, str) or not extracted.strip():
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason="pdf_text_empty",
                        response=response,
                    )
                )
                continue
            text = extracted.encode("utf-8")
            derived = _derived_response(
                response,
                encoded=raw_pdf,
                decoded=text,
                declared_content_type="application/pdf",
                detected_content_type=DetectedMimeType.TEXT,
            )
            reason = article_rejection_reason(
                body=text,
                content_type="text/plain",
                requested_url=request.requested_url,
                expected_title=request.title,
                min_text_chars=request.min_text_chars,
                expected_languages=request.expected_languages,
            )
            similarity = _extracted_text_title_similarity(request.title, extracted)
            if reason is not None or (request.title and similarity < _TITLE_MATCH_THRESHOLD):
                probes.append(
                    _probe(
                        name=self.name,
                        version=self.version,
                        method=self.method,
                        url=candidate,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=reason or "pdf_title_mismatch",
                        response=derived,
                    )
                )
                continue
            equivalence = classify_equivalence(
                request.requested_url,
                candidate,
                same_publisher=True,
                title_similarity=similarity,
            )
            probes.append(
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=candidate,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=derived,
                    equivalence=equivalence,
                    evidence=(
                        EvidenceClass.PRIMARY_OBSERVED
                        if equivalence is SourceEquivalenceClass.EXACT_ORIGINAL
                        else EvidenceClass.VERIFIED_EQUIVALENT
                        if equivalence is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
                        else EvidenceClass.UNKNOWN
                    ),
                    observation=AcquisitionObservationKind.PUBLISHER_PDF,
                    quality=_quality(text, request.title),
                )
            )
            break
        return tuple(probes)


@dataclass(frozen=True, slots=True)
class VisualOcrStrategy:
    artifact: CapturedArtifact | None = None
    engine: OcrEngine | None = None
    name: str = "visual_ocr"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.VISUAL_OCR
    enabled: bool = True
    timeout_seconds: float = 20.0

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.engine
            else AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE
        )

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        engine = self.engine
        if engine is None:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.NOT_CONFIGURED,
                    reason="ocr_not_configured",
                    capability=AcquisitionCapabilityState.DEPENDENCY_UNAVAILABLE,
                ),
            )
        if self.artifact is None:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="ocr_artifact_not_supplied",
                ),
            )
        media_type = self.artifact.content_type.casefold().split(";", 1)[0].strip()
        if media_type not in {
            "application/pdf",
            "image/png",
            "image/jpeg",
            "image/gif",
            "image/webp",
        }:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=self.artifact.source_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="unsupported_ocr_artifact_type",
                ),
            )
        try:
            artifact_hash = self.artifact.digest()
        except ValueError:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=self.artifact.source_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="artifact_hash_mismatch",
                ),
            )
        if len(self.artifact.encoded_body) > _MAX_PAGE_BYTES:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=self.artifact.source_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="ocr_artifact_exceeds_limit",
                ),
            )
        try:
            text = await asyncio.wait_for(
                engine.recognize(self.artifact), timeout=self.timeout_seconds
            )
        except TimeoutError:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=self.artifact.source_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason="ocr_timeout",
                ),
            )
        except Exception:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=self.artifact.source_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason="ocr_failed",
                ),
            )
        if not isinstance(text, str) or not text.strip():
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=self.artifact.source_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="ocr_text_empty",
                ),
            )
        decoded = text.encode("utf-8")
        acquired_at = datetime.now(UTC)
        response = CollectedResponse(
            requested_url=self.artifact.source_url,
            final_url=self.artifact.source_url,
            redirect_chain=(),
            status=200,
            headers={"content-type": self.artifact.content_type},
            declared_content_type=self.artifact.content_type,
            detected_content_type=DetectedMimeType.TEXT,
            encoded_body=self.artifact.encoded_body,
            decoded_body=decoded,
            encoded_size=len(self.artifact.encoded_body),
            encoded_sha256=artifact_hash,
            decoded_size=len(decoded),
            decoded_sha256=hashlib.sha256(decoded).hexdigest(),
            content_encoding="identity",
            acquired_at=acquired_at,
        )
        reason = article_rejection_reason(
            body=decoded,
            content_type="text/plain",
            requested_url=request.requested_url,
            expected_title=request.title,
            min_text_chars=request.min_text_chars,
            expected_languages=request.expected_languages,
        )
        similarity = _extracted_text_title_similarity(request.title, text)
        if reason is not None or (request.title and similarity < _TITLE_MATCH_THRESHOLD):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=self.artifact.source_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason=reason or "ocr_title_mismatch",
                    response=response,
                    observation=AcquisitionObservationKind.OCR_TEXT,
                    evidence=EvidenceClass.OCR_DERIVED,
                ),
            )
        return (
            _probe(
                name=self.name,
                version=self.version,
                method=self.method,
                url=self.artifact.source_url,
                outcome=AcquisitionOutcome.SUCCEEDED,
                response=response,
                equivalence=classify_equivalence(
                    request.requested_url,
                    self.artifact.source_url,
                    same_publisher=_same_publisher_candidate(request, self.artifact.source_url),
                    title_similarity=similarity,
                ),
                evidence=EvidenceClass.OCR_DERIVED,
                observation=AcquisitionObservationKind.OCR_TEXT,
                quality=_quality(decoded, request.title),
            ),
        )


@dataclass(frozen=True, slots=True)
class OfficialNewsletterArchiveStrategy:
    archive_endpoint: str | None = None
    official_domains: frozenset[str] = frozenset()
    name: str = "official_newsletter_archive"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.OFFICIAL_NEWSLETTER_ARCHIVE
    timeout_seconds: float = 15.0

    @property
    def enabled(self) -> bool:
        return bool(self.archive_endpoint)

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.archive_endpoint
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        archive_endpoint = self.archive_endpoint
        if not archive_endpoint:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.NOT_CONFIGURED,
                    reason="official_archive_not_configured",
                    capability=AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION,
                ),
            )
        if not _safe_http_url(archive_endpoint):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=archive_endpoint,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="invalid_official_archive_endpoint",
                ),
            )
        host = _host(archive_endpoint) or ""
        configured = any(
            host == domain or host.endswith(f".{domain}") for domain in self.official_domains
        )
        if not configured and not _same_publisher(request.requested_url, archive_endpoint):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=archive_endpoint,
                    outcome=AcquisitionOutcome.POLICY_SKIPPED,
                    reason="archive_endpoint_not_official",
                    capability=AcquisitionCapabilityState.BLOCKED_BY_POLICY,
                    policy_decision=AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY,
                ),
            )
        source_decision = request.policy.check_url(request.requested_url)
        if source_decision is not AcquisitionPolicyDecision.ALLOWED:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(request.requested_url),
                    outcome=AcquisitionOutcome.POLICY_SKIPPED,
                    reason="original_url_policy_denied",
                    capability=AcquisitionCapabilityState.BLOCKED_BY_POLICY,
                    policy_decision=source_decision,
                ),
            )
        if "{url}" in archive_endpoint or "{title}" in archive_endpoint:
            target = archive_endpoint.format(
                url=quote(request.requested_url, safe=""),
                title=quote(request.title or "", safe=""),
            )
        else:
            split = urlsplit(archive_endpoint)
            query = parse_qsl(split.query, keep_blank_values=True)
            query.append(("url", request.requested_url))
            target = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), ""))
        response, error = await _fetch(
            request,
            name=self.name,
            version=self.version,
            method=self.method,
            url=target,
            timeout_seconds=self.timeout_seconds,
        )
        if error is not None:
            return (error,)
        assert response is not None
        reason, similarity = _validate_page(response, request)
        if reason is not None:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=target,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason=reason,
                    response=response,
                ),
            )
        equivalence = classify_equivalence(
            request.requested_url,
            target,
            same_publisher=_same_publisher(request.requested_url, target),
            title_similarity=similarity,
        )
        return (
            _probe(
                name=self.name,
                version=self.version,
                method=self.method,
                url=target,
                outcome=AcquisitionOutcome.SUCCEEDED,
                response=response,
                equivalence=equivalence,
                evidence=EvidenceClass.VERIFIED_EQUIVALENT
                if equivalence is SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT
                else EvidenceClass.UNKNOWN,
                quality=_quality(response.decoded_body, request.title),
            ),
        )


@dataclass(frozen=True, slots=True)
class ThirdPartyExtractionStrategy:
    provider_url: str | None = None
    provider_token: str | None = None
    name: str = "third_party_extraction"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.THIRD_PARTY_EXTRACTION
    timeout_seconds: float = 20.0

    @property
    def enabled(self) -> bool:
        return bool(self.provider_url and self.provider_token)

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return (
            AcquisitionCapabilityState.IMPLEMENTED
            if self.enabled
            else AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
        )

    def _policy_probe(
        self, request: AcquisitionRequest, url: str, decision: AcquisitionPolicyDecision
    ) -> AcquisitionProbe:
        return _probe(
            name=self.name,
            version=self.version,
            method=self.method,
            url=safe_external_url(url),
            outcome=(
                AcquisitionOutcome.NOT_CONFIGURED
                if decision is AcquisitionPolicyDecision.NOT_CONFIGURED
                else AcquisitionOutcome.POLICY_SKIPPED
            ),
            reason=(
                "provider_not_configured"
                if decision is AcquisitionPolicyDecision.NOT_CONFIGURED
                else "provider_policy_denied"
            ),
            capability=(
                AcquisitionCapabilityState.DISABLED_BY_CONFIGURATION
                if decision is AcquisitionPolicyDecision.NOT_CONFIGURED
                else AcquisitionCapabilityState.BLOCKED_BY_POLICY
            ),
            policy_decision=decision,
        )

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        if not self.enabled:
            return (
                self._policy_probe(
                    request, request.requested_url, AcquisitionPolicyDecision.NOT_CONFIGURED
                ),
            )
        assert self.provider_url is not None and self.provider_token is not None
        if not _safe_http_url(self.provider_url):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(self.provider_url),
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="invalid_provider_endpoint",
                ),
            )
        title = request.title
        if not title or not _safe_http_url(request.requested_url):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(request.requested_url),
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="canonical_public_url_and_title_required",
                ),
            )
        source_decision = request.policy.check_destination(
            request.requested_url, third_party_provider=True
        )
        if source_decision is not AcquisitionPolicyDecision.ALLOWED:
            return (self._policy_probe(request, request.requested_url, source_decision),)
        provider_decision = request.policy.check_destination(
            self.provider_url, third_party_provider=True
        )
        if provider_decision is not AcquisitionPolicyDecision.ALLOWED:
            return (self._policy_probe(request, self.provider_url, provider_decision),)
        canonical = canonicalize_http_url(request.requested_url)
        split = urlsplit(self.provider_url)
        query = parse_qsl(split.query, keep_blank_values=True)
        query.extend((("url", canonical), ("title", title)))
        target = urlunsplit((split.scheme, split.netloc, split.path, urlencode(query), ""))
        if _attempt_key(self.name, target) in request.attempted_strategy_urls:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.SKIPPED,
                    reason="already_attempted_for_strategy",
                ),
            )
        try:
            response = await request.fetch(
                target,
                {"accept": "application/json", "authorization": f"Bearer {self.provider_token}"},
                self.timeout_seconds,
                False,
            )
        except TimeoutError:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.FAILED,
                    reason="provider_timeout",
                ),
            )
        except Exception:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.FAILED,
                    reason="provider_ambiguous_failure",
                ),
            )
        response = replace(
            response,
            requested_url=safe_external_url(target),
            final_url=safe_external_url(response.final_url),
            redirect_chain=(),
        )
        if response.status != 200:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.FAILED,
                    reason="provider_ambiguous_failure",
                    response=response,
                ),
            )
        try:
            payload = json.loads(response.decoded_body)
        except (UnicodeError, json.JSONDecodeError):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.FAILED,
                    reason="provider_ambiguous_failure",
                    response=response,
                ),
            )
        if not isinstance(payload, dict):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.FAILED,
                    reason="provider_ambiguous_failure",
                    response=response,
                ),
            )
        result_url = payload.get("url")
        title = payload.get("title")
        text = payload.get("text") or payload.get("articleBody") or payload.get("content")
        if (
            not isinstance(result_url, str)
            or not _safe_http_url(result_url)
            or not isinstance(title, str)
            or not isinstance(text, str)
        ):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="provider_result_missing_required_fields",
                    response=response,
                ),
            )
        if not (
            canonicalize_http_url(result_url) == canonical or _same_publisher(result_url, canonical)
        ) or not _strong_title_match(request.title, title):
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="provider_result_url_or_title_mismatch",
                    response=response,
                ),
            )
        supplied_hash = payload.get("sha256")
        text_bytes = text.encode("utf-8")
        text_hash = hashlib.sha256(text_bytes).hexdigest()
        if supplied_hash is not None and str(supplied_hash).casefold() != text_hash:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="provider_result_hash_mismatch",
                    response=response,
                ),
            )
        reason = article_rejection_reason(
            body=text_bytes,
            content_type="text/plain",
            requested_url=request.requested_url,
            expected_title=request.title,
            min_text_chars=request.min_text_chars,
            expected_languages=request.expected_languages,
        )
        if reason is not None:
            return (
                _probe(
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=safe_external_url(target),
                    outcome=AcquisitionOutcome.REJECTED,
                    reason=reason,
                    response=response,
                ),
            )
        derived = replace(
            response,
            requested_url=safe_external_url(target),
            final_url=safe_external_url(target),
            declared_content_type="application/json",
            detected_content_type=DetectedMimeType.TEXT,
            decoded_body=text_bytes,
            decoded_size=len(text_bytes),
            decoded_sha256=text_hash,
        )
        try:
            pub_date = (
                date.fromisoformat(str(payload["published_at"])[:10])
                if payload.get("published_at")
                else None
            )
        except ValueError:
            pub_date = None
        publisher = payload.get("publisher")
        return (
            _probe(
                name=self.name,
                version=self.version,
                method=self.method,
                url=safe_external_url(target),
                outcome=AcquisitionOutcome.SUCCEEDED,
                response=derived,
                equivalence=classify_equivalence(
                    request.requested_url,
                    result_url,
                    same_publisher=_same_publisher(request.requested_url, result_url),
                    title_similarity=_title_similarity(request.title, title),
                ),
                evidence=EvidenceClass.UNKNOWN,
                quality=_quality(text_bytes, request.title, reasons=("third_party_derived",)),
                publication_date=pub_date,
                publisher=str(publisher) if publisher else None,
            ),
        )
