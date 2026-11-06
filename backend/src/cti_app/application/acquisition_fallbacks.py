"""Pure helpers for safe, same-publication acquisition fallbacks."""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
import re
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from html.parser import HTMLParser
from typing import Protocol, cast
from urllib.parse import quote, unquote, urlencode, urljoin, urlsplit, urlunsplit

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

_LINK_RELATIONS = frozenset({"amphtml", "alternate", "canonical", "print"})
_LINK_HEADER_RE = re.compile(r"<([^>]+)>\s*;([^,]*)")
_REFUSAL_RE = re.compile(
    r"(?is)^\s*(?:i\s+(?:cannot|can't|could not|couldn't|am unable to)\b|"
    r"(?:the\s+)?article\s+(?:is\s+)?(?:unavailable|not available)\b)"
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


class _ArticleLinkParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self.title_parts: list[str] = []
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
    title_line = f"Title: {safe_title}\n" if safe_title else ""
    return (
        "Retrieve and transcribe the public article at the URL below. Do not infer or "
        "reconstruct missing text. Include IOC strings only when visible in the article. "
        "If the article cannot be accessed, write ARTICLE: UNAVAILABLE. Return plain text "
        "in this tolerant format:\n"
        "ARTICLE:\n<article text>\n"
        "IOCS:\n- <type>: <value> — <short context>\n\n"
        f"URL: {url}\n{title_line}"
    )


def parse_bridge_transcription(text: str) -> str | None:
    """Accept the requested labels or a plain article transcript; reject refusals."""
    body = text.strip()
    if body.startswith("```"):
        body = re.sub(r"^```[^\n]*\n?", "", body, count=1)
        body = re.sub(r"\n?```\s*$", "", body, count=1).strip()
    if not body:
        return None

    article_match = re.search(r"(?im)^\s*(?:ARTICLE|TEXT|TRANSCRIPTION)\s*:\s*", body)
    ioc_match = re.search(r"(?im)^\s*(?:IOCS|INDICATORS)\s*:\s*", body)
    if article_match:
        article_start = article_match.end()
        article_end = (
            ioc_match.start() if ioc_match and ioc_match.start() > article_start else len(body)
        )
        article = body[article_start:article_end].strip()
    else:
        if _REFUSAL_RE.search(body):
            return None
        article = body

    if re.fullmatch(
        r"(?is)(?:\[?\s*)?(?:unavailable|not available|no access)(?:\s*\]?)\W*",
        article,
    ):
        return None
    if not article:
        return None
    if ioc_match:
        ioc_notes = body[ioc_match.end() :].strip()
        if ioc_notes:
            article += "\n\nBridge IOC notes (unverified by primary source):\n" + ioc_notes
    return article


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
                _EVIDENCE_CLASS_STRENGTH[self.evidence_class]
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


def default_acquisition_strategies(
    *,
    flags: dict[str, bool] | None = None,
    timeouts: dict[str, float] | None = None,
    max_candidates: int = 24,
    playwright_adapter: BrowserRenderAdapter | None = None,
) -> tuple[AcquisitionStrategy, ...]:
    """Ordered default strategy registry. All HTTP calls remain SafeHttpCollector calls."""
    enabled = flags or {}
    timeout = timeouts or {}

    def configured(name: str, default: float) -> float:
        return max(0.1, min(float(timeout.get(name, default)), 300.0))

    def on(name: str) -> bool:
        return enabled.get(name, True)

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
        WaybackStrategy(enabled=on("wayback"), timeout_seconds=configured("wayback", 20)),
        ArchivePhStrategy(enabled=on("archive_ph"), timeout_seconds=configured("archive_ph", 15)),
        CommonCrawlStrategy(
            enabled=on("common_crawl"), timeout_seconds=configured("common_crawl", 25)
        ),
        MementoStrategy(enabled=on("memento"), timeout_seconds=configured("memento", 15)),
        SearchCacheStrategy(
            enabled=on("search_cache"), timeout_seconds=configured("search_cache", 10)
        ),
        PlaywrightStrategy(
            adapter=playwright_adapter,
            enabled=on("playwright") and playwright_adapter is not None,
            timeout_seconds=configured("playwright", 45),
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
