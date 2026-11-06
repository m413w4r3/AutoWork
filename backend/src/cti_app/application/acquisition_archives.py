"""Safe, deterministic archive acquisition strategies.

Every remote operation in this module uses ``AcquisitionRequest.fetch`` so the
configured SafeHttpCollector remains the only network boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import zlib
from dataclasses import dataclass
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from html.parser import HTMLParser
from typing import Any
from urllib.parse import quote, urlencode, urlsplit

from cti_app.application.acquisition_fallbacks import (
    AcquisitionProbe,
    AcquisitionRequest,
    archive_url_variants,
    article_rejection_reason,
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
    AcquisitionPolicyDecision,
    AcquisitionQuality,
    EvidenceClass,
    SourceEquivalenceClass,
    credential_free_url,
    is_credential_free_url,
)
from cti_app.domain.collection import SourceAcquisitionMethod
from cti_app.domain.discovery import canonicalize_http_url

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)
_MAX_CDX_ROWS = 100
_MAX_WARC_RANGE_BYTES = 10 * 1024 * 1024
_MAX_WARC_EXPANDED_BYTES = 25 * 1024 * 1024
_ARCHIVE_WRAPPER_MARKERS = frozenset(
    {"wm-ipp", "wm-ipp-base", "wayback-toolbar", "wayback-banner", "archive-banner"}
)


class _HtmlAudit(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.title: list[str] = []
        self.text: list[str] = []
        self._in_title = False
        self.archive_wrapper = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        values = {key.casefold(): value or "" for key, value in attrs}
        self._in_title = self._in_title or tag.casefold() == "title"
        classes = set(values.get("class", "").casefold().split())
        identity = values.get("id", "").casefold()
        self.archive_wrapper = self.archive_wrapper or bool(
            classes.intersection(_ARCHIVE_WRAPPER_MARKERS) or identity in _ARCHIVE_WRAPPER_MARKERS
        )

    def handle_endtag(self, tag: str) -> None:
        if tag.casefold() == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        self.text.append(data)
        if self._in_title:
            self.title.append(data)


def _clean_visible_text(body: bytes) -> str:
    text = body.decode("utf-8-sig", errors="replace")
    text = re.sub(r"(?is)<(script|style|noscript)\b.*?</\1\s*>", " ", text)
    return " ".join(re.sub(r"(?s)<[^>]+>", " ", text).split())


def _title_similarity(expected: str | None, actual: str) -> float:
    expected_words = {word.casefold() for word in _WORD_RE.findall(expected or "") if len(word) > 2}
    if not expected_words:
        return 1.0
    actual_words = {word.casefold() for word in _WORD_RE.findall(actual) if len(word) > 2}
    if not actual_words:
        return 0.0
    return len(expected_words & actual_words) / len(expected_words | actual_words)


def _quality(
    body: bytes, title: str | None, *, reasons: tuple[str, ...] = ()
) -> AcquisitionQuality:
    visible = _clean_visible_text(body)
    words = _WORD_RE.findall(visible)
    audit = _HtmlAudit()
    try:
        audit.feed(body.decode("utf-8-sig", errors="replace"))
    except Exception:
        pass
    similarity = _title_similarity(title, " ".join(audit.title))
    score = min(1.0, len(visible) / 1000.0)
    if title and similarity < 0.5:
        score *= max(0.25, similarity)
    return AcquisitionQuality(
        text_chars=len(visible),
        word_count=len(words),
        title_similarity=similarity,
        language=None,
        boilerplate_ratio=0.0,
        duplicate_ratio=0.0,
        truncated=False,
        challenge_detected=False,
        paywall_detected=False,
        score=score,
        reasons=reasons,
    )


def _timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if re.fullmatch(r"\d{14}", text):
        try:
            return datetime.strptime(text, "%Y%m%d%H%M%S").replace(tzinfo=UTC)
        except ValueError:
            return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
    except ValueError:
        try:
            parsed = parsedate_to_datetime(text)
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        except (TypeError, ValueError, OverflowError):
            return None


def _canonical(url: str) -> str | None:
    try:
        return canonicalize_http_url(url)
    except (TypeError, ValueError):
        return None


def _eligible_original(
    request: AcquisitionRequest,
    *,
    name: str,
    version: str,
    method: SourceAcquisitionMethod,
) -> AcquisitionProbe | None:
    """Do not submit an origin URL that source policy marks unsafe or forbidden."""
    decision = request.policy.check_url(request.requested_url)
    if decision is AcquisitionPolicyDecision.ALLOWED and is_credential_free_url(
        request.requested_url
    ):
        return None
    if decision is AcquisitionPolicyDecision.ALLOWED:
        decision = AcquisitionPolicyDecision.DENIED_CREDENTIALS
    return _make_probe(
        strategy_name=name,
        strategy_version=version,
        method=method,
        url=credential_free_url(request.requested_url),
        outcome=AcquisitionOutcome.POLICY_SKIPPED,
        reason="original_url_policy_denied",
        capability=AcquisitionCapabilityState.BLOCKED_BY_POLICY,
        policy_decision=decision,
    )


def _make_probe(
    *,
    strategy_name: str,
    strategy_version: str,
    method: SourceAcquisitionMethod,
    url: str,
    outcome: AcquisitionOutcome,
    reason: str | None = None,
    response: CollectedResponse | None = None,
    duration_ms: int = 0,
    equivalence: SourceEquivalenceClass | None = None,
    evidence: EvidenceClass | None = None,
    snapshot_time: datetime | None = None,
    quality: AcquisitionQuality | None = None,
    capability: AcquisitionCapabilityState = AcquisitionCapabilityState.IMPLEMENTED,
    policy_decision: AcquisitionPolicyDecision = AcquisitionPolicyDecision.ALLOWED,
) -> AcquisitionProbe:
    return AcquisitionProbe(
        strategy_name=strategy_name,
        strategy_version=strategy_version,
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
        observation_kind=AcquisitionObservationKind.HTTP_RESPONSE,
        equivalence_class=equivalence,
        evidence_class=evidence,
        quality=quality,
        archive_snapshot_time=snapshot_time,
    )


def _attempt_key(strategy_name: str, url: str) -> str:
    return hashlib.sha256(f"{strategy_name}\0{url}".encode()).hexdigest()


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
        return None, _make_probe(
            strategy_name=name,
            strategy_version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.SKIPPED,
            reason="already_attempted_for_strategy",
        )
    started = time.monotonic()
    try:
        response = await request.fetch(url, headers or {}, timeout_seconds, allow_binary)
    except CollectionError as error:
        probe = _make_probe(
            strategy_name=name,
            strategy_version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.FAILED,
            reason=getattr(error.reason_code, "value", "collection_failed"),
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        return None, probe
    except TimeoutError:
        return None, _make_probe(
            strategy_name=name,
            strategy_version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.FAILED,
            reason="timeout",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    except Exception:
        return None, _make_probe(
            strategy_name=name,
            strategy_version=version,
            method=method,
            url=url,
            outcome=AcquisitionOutcome.FAILED,
            reason="fetch_failed",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
    return response, None


def _validated_article(
    response: CollectedResponse,
    request: AcquisitionRequest,
) -> str | None:
    content_type = (response.declared_content_type or "").casefold().split(";", 1)[0]
    if response.status != 200:
        return "archive_http_status_not_200"
    if content_type and content_type not in {
        "text/html",
        "application/xhtml+xml",
        "text/plain",
        "application/json",
    }:
        return "unsupported_article_content_type"
    return article_rejection_reason(
        body=response.decoded_body,
        content_type=response.declared_content_type,
        requested_url=request.requested_url,
        expected_title=request.title,
        min_text_chars=request.min_text_chars,
        headers=response.headers,
        expected_languages=request.expected_languages,
    )


def _wayback_wrapper_reason(body: bytes, min_text_chars: int) -> str | None:
    audit = _HtmlAudit()
    try:
        audit.feed(body.decode("utf-8-sig", errors="replace"))
    except Exception:
        return None
    if audit.archive_wrapper:
        return "archive_wrapper_detected"
    title = " ".join(" ".join(audit.title).split()).casefold()
    text = " ".join(" ".join(audit.text).split()).casefold()
    wrapper_words = ("wayback machine", "internet archive", "save page now", "about this capture")
    if any(term in title for term in wrapper_words) and len(_WORD_RE.findall(text)) < max(
        20, min_text_chars // 8
    ):
        return "archive_wrapper_only"
    return None


def _wayback_replay_matches(url: str, *, timestamp: str, original_url: str) -> bool:
    try:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() != "https" or parsed.hostname != "web.archive.org":
            return False
        prefix = f"/web/{timestamp}id_/"
        if not parsed.path.startswith(prefix):
            return False
        target = parsed.path[len(prefix) :]
        if parsed.query:
            target = f"{target}?{parsed.query}"
        return _canonical(target) == _canonical(original_url)
    except (TypeError, ValueError):
        return False


def _wayback_rows(
    body: bytes, original_url: str, published_at: date | None
) -> list[tuple[str, str]]:
    try:
        decoded = body.decode("utf-8-sig")
    except (UnicodeError, json.JSONDecodeError):
        return []
    try:
        rows = json.loads(decoded)
        if not isinstance(rows, list):
            rows = []
    except json.JSONDecodeError:
        rows = []
    if not rows:
        rows = []
        for line in decoded.splitlines():
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(row, list):
                rows.append(row)
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    expected = _canonical(original_url)
    if expected is None:
        return []
    candidates: list[tuple[str, str]] = []
    for row in rows[1:]:
        if not isinstance(row, list) or len(row) < 3:
            continue
        stamp, original, status = str(row[0]), str(row[1]), str(row[2])
        if not re.fullmatch(r"\d{14}", stamp) or status != "200":
            continue
        if _canonical(original) != expected:
            continue
        if _timestamp(stamp) is None:
            continue
        candidates.append((stamp, original))
    if not candidates:
        return []
    if published_at is None:
        candidates.sort(key=lambda item: item[0])
    else:
        target = datetime.combine(published_at, datetime.min.time(), tzinfo=UTC)

        def selection_key(item: tuple[str, str]) -> tuple[float, int, str]:
            snapshot_time = _timestamp(item[0])
            assert snapshot_time is not None
            return (
                abs((snapshot_time - target).total_seconds()),
                0 if snapshot_time <= target else 1,
                item[0],
            )

        candidates.sort(key=selection_key)
    return candidates


@dataclass(frozen=True, slots=True)
class WaybackSnapshotStrategy:
    name: str = "wayback_snapshot"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.WAYBACK_SNAPSHOT
    enabled: bool = True
    timeout_seconds: float = 20.0
    max_snapshots: int = 20

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return AcquisitionCapabilityState.IMPLEMENTED

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        denied = _eligible_original(
            request, name=self.name, version=self.version, method=self.method
        )
        if denied is not None:
            return (denied,)
        original = _canonical(request.requested_url)
        if original is None:
            return (
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="invalid_original_url",
                ),
            )
        limit = max(1, min(int(self.max_snapshots), _MAX_CDX_ROWS))
        lookup_url = "https://web.archive.org/cdx/search/cdx?" + urlencode(
            {
                "url": original,
                "output": "json",
                "filter": "statuscode:200",
                "fl": "timestamp,original,statuscode",
                "limit": str(limit),
            }
        )
        lookup, error = await _fetch(
            request,
            name=self.name,
            version=self.version,
            method=self.method,
            url=lookup_url,
            timeout_seconds=self.timeout_seconds,
        )
        probes: list[AcquisitionProbe] = []
        if error is not None:
            return (error,)
        assert lookup is not None
        if lookup.status != 200:
            return (
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=lookup_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason="cdx_http_status_not_200",
                    response=lookup,
                ),
            )
        candidates = _wayback_rows(lookup.decoded_body, original, request.published_at)
        probes.append(
            _make_probe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=lookup_url,
                outcome=AcquisitionOutcome.LOOKUP_SUCCEEDED,
                response=lookup,
            )
        )
        if not candidates:
            probes.append(
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=lookup_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="no_exact_matching_snapshot",
                )
            )
            return tuple(probes)
        for stamp, row_original in candidates[:limit]:
            replay_url = f"https://web.archive.org/web/{stamp}id_/{_canonical(row_original)}"
            snapshot, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=replay_url,
                timeout_seconds=self.timeout_seconds,
            )
            if error is not None:
                probes.append(error)
                continue
            assert snapshot is not None
            reason = (
                None
                if _wayback_replay_matches(
                    snapshot.final_url, timestamp=stamp, original_url=original
                )
                else "replay_url_mismatch"
            )
            if reason is None:
                reason = _wayback_wrapper_reason(snapshot.decoded_body, request.min_text_chars)
            if reason is None:
                reason = _validated_article(snapshot, request)
            parsed_stamp = _timestamp(stamp)
            if reason is not None:
                probes.append(
                    _make_probe(
                        strategy_name=self.name,
                        strategy_version=self.version,
                        method=self.method,
                        url=replay_url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=reason,
                        response=snapshot,
                        snapshot_time=parsed_stamp,
                    )
                )
                continue
            probes.append(
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=replay_url,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=snapshot,
                    equivalence=SourceEquivalenceClass.HISTORICAL_ORIGINAL,
                    evidence=EvidenceClass.HISTORICAL_PRIMARY,
                    snapshot_time=parsed_stamp,
                    quality=_quality(snapshot.decoded_body, request.title),
                )
            )
            break
        return tuple(probes)


def _latest_index(body: bytes) -> str | None:
    try:
        rows = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(rows, list):
        return None
    ids = [
        str(row["id"])
        for row in rows
        if isinstance(row, dict) and re.fullmatch(r"CC-MAIN-\d{4}-\d{2}", str(row.get("id", "")))
    ]
    return max(ids) if ids else None


def _cdx_rows(body: bytes, expected_urls: tuple[str, ...]) -> list[dict[str, Any]]:
    expected = {_canonical(url) for url in expected_urls}
    expected.discard(None)
    rows: list[dict[str, Any]] = []
    for line in body.splitlines():
        try:
            row = json.loads(line)
        except (UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(row, dict) or str(row.get("status", row.get("statuscode", ""))) != "200":
            continue
        if _canonical(str(row.get("url", ""))) not in expected:
            continue
        try:
            offset, length = int(row["offset"]), int(row["length"])
        except (KeyError, TypeError, ValueError):
            continue
        filename = str(row.get("filename", ""))
        parts = filename.split("/")
        if (
            offset < 0
            or length < 1
            or length > _MAX_WARC_RANGE_BYTES
            or not filename.startswith("crawl-data/")
            or re.fullmatch(r"crawl-data/[A-Za-z0-9._/-]+\.warc\.gz", filename) is None
            or "\\" in filename
            or any(part in {"", ".", ".."} for part in parts)
            or not filename.endswith(".warc.gz")
        ):
            continue
        stamp = str(row.get("timestamp", ""))
        if _timestamp(stamp) is None:
            continue
        rows.append({"offset": offset, "length": length, "filename": filename, "timestamp": stamp})
    return rows


def _parse_warc(
    response: CollectedResponse,
    expected_urls: tuple[str, ...],
    expected_range_length: int,
) -> tuple[CollectedResponse, datetime | None] | None:
    if response.status != 206 or response.encoded_size != expected_range_length:
        return None
    body = response.decoded_body
    try:
        if body.startswith(b"\x1f\x8b"):
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            record = inflater.decompress(body, _MAX_WARC_EXPANDED_BYTES + 1)
            if len(record) > _MAX_WARC_EXPANDED_BYTES or not inflater.eof:
                return None
            record += inflater.flush()
            if len(record) > _MAX_WARC_EXPANDED_BYTES:
                return None
        else:
            record = body
    except (OSError, EOFError, zlib.error):
        return None
    header_end = record.find(b"\r\n\r\n")
    if header_end < 0 or not record.startswith(b"WARC/1."):
        return None
    warc_header = record[:header_end].decode("latin-1", errors="replace")
    fields: dict[str, str] = {}
    for line in warc_header.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep:
            fields[key.strip().casefold()] = value.strip()
    if fields.get("warc-type", "").casefold() != "response":
        return None
    target = _canonical(fields.get("warc-target-uri", ""))
    if target is None or target not in {_canonical(url) for url in expected_urls}:
        return None
    try:
        warc_length = int(fields["content-length"])
    except (KeyError, TypeError, ValueError):
        return None
    content = record[header_end + 4 :]
    if warc_length < 1 or len(content) < warc_length:
        return None
    if len(content) > warc_length and content[warc_length:].strip(b"\r\n"):
        return None
    http_record = content[:warc_length]
    http_header_end = http_record.find(b"\r\n\r\n")
    if http_header_end < 0:
        return None
    http_head = http_record[:http_header_end].decode("latin-1", errors="replace")
    status_match = re.match(r"^HTTP/\d(?:\.\d)?\s+(\d{3})(?:\s|$)", http_head)
    if status_match is None or status_match.group(1) != "200":
        return None
    http_headers: dict[str, str] = {}
    for line in http_head.split("\r\n")[1:]:
        key, sep, value = line.partition(":")
        if sep:
            http_headers[key.strip().casefold()] = value.strip()
    if http_headers.get("transfer-encoding", "").casefold() not in {"", "identity"}:
        return None
    encoded_article = http_record[http_header_end + 4 :]
    if "content-length" in http_headers:
        try:
            if int(http_headers["content-length"]) != len(encoded_article):
                return None
        except ValueError:
            return None
    content_type = http_headers.get("content-type", "")
    base_type = content_type.split(";", 1)[0].strip().casefold()
    if base_type not in {"text/html", "application/xhtml+xml", "text/plain", "application/json"}:
        return None
    article = encoded_article
    content_encoding = http_headers.get("content-encoding", "identity").casefold()
    if content_encoding == "gzip":
        try:
            inflater = zlib.decompressobj(16 + zlib.MAX_WBITS)
            article = inflater.decompress(article, _MAX_WARC_EXPANDED_BYTES + 1)
            if len(article) > _MAX_WARC_EXPANDED_BYTES or not inflater.eof:
                return None
            article += inflater.flush()
        except zlib.error:
            return None
    encoded_digest = hashlib.sha256(encoded_article).hexdigest()
    decoded_digest = hashlib.sha256(article).hexdigest()
    warc_date = _timestamp(fields.get("warc-date", ""))
    acquired = response.acquired_at
    return (
        CollectedResponse(
            requested_url=response.requested_url,
            final_url=fields.get("warc-target-uri", response.final_url),
            redirect_chain=(),
            status=200,
            headers={"content-type": content_type, "status": "200"},
            declared_content_type=base_type,
            detected_content_type=_detect_mime(article, allow_images=False, allow_binary=False),
            encoded_body=encoded_article,
            decoded_body=article,
            encoded_size=len(encoded_article),
            encoded_sha256=encoded_digest,
            decoded_size=len(article),
            decoded_sha256=decoded_digest,
            content_encoding=content_encoding,
            acquired_at=acquired,
        ),
        warc_date,
    )


@dataclass(frozen=True, slots=True)
class CommonCrawlWarcStrategy:
    name: str = "commoncrawl_warc"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.COMMONCRAWL_WARC
    enabled: bool = True
    timeout_seconds: float = 20.0
    max_range_bytes: int = _MAX_WARC_RANGE_BYTES

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return AcquisitionCapabilityState.IMPLEMENTED

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        denied = _eligible_original(
            request, name=self.name, version=self.version, method=self.method
        )
        if denied is not None:
            return (denied,)
        probes: list[AcquisitionProbe] = []
        info_url = "https://index.commoncrawl.org/collinfo.json"
        info, error = await _fetch(
            request,
            name=self.name,
            version=self.version,
            method=self.method,
            url=info_url,
            timeout_seconds=self.timeout_seconds,
        )
        if error is not None:
            return (error,)
        assert info is not None
        if info.status != 200:
            return (
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=info_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason="collinfo_http_status_not_200",
                    response=info,
                ),
            )
        index_id = _latest_index(info.decoded_body)
        probes.append(
            _make_probe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=info_url,
                outcome=AcquisitionOutcome.LOOKUP_SUCCEEDED,
                response=info,
            )
        )
        if index_id is None:
            probes.append(
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=info_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="index_unavailable",
                )
            )
            return tuple(probes)
        variants = archive_url_variants(request.requested_url)
        max_range = max(1, min(int(self.max_range_bytes), _MAX_WARC_RANGE_BYTES))
        for variant in variants:
            index_url = f"https://index.commoncrawl.org/{index_id}-index?" + urlencode(
                {"url": variant, "output": "json"}
            )
            index, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=index_url,
                timeout_seconds=self.timeout_seconds,
            )
            if error is not None:
                probes.append(error)
                continue
            assert index is not None
            if index.status != 200:
                probes.append(
                    _make_probe(
                        strategy_name=self.name,
                        strategy_version=self.version,
                        method=self.method,
                        url=index_url,
                        outcome=AcquisitionOutcome.FAILED,
                        reason="commoncrawl_index_http_status_not_200",
                        response=index,
                    )
                )
                continue
            expected = (variant,)
            records = _cdx_rows(index.decoded_body, expected)
            probes.append(
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=index_url,
                    outcome=AcquisitionOutcome.LOOKUP_SUCCEEDED,
                    response=index,
                )
            )
            if not records:
                continue
            for record in records:
                if record["length"] > max_range:
                    probes.append(
                        _make_probe(
                            strategy_name=self.name,
                            strategy_version=self.version,
                            method=self.method,
                            url=index_url,
                            outcome=AcquisitionOutcome.REJECTED,
                            reason="warc_range_exceeds_limit",
                        )
                    )
                    continue
                end = record["offset"] + record["length"] - 1
                warc_url = f"https://data.commoncrawl.org/{record['filename']}"
                ranged, error = await _fetch(
                    request,
                    name=self.name,
                    version=self.version,
                    method=self.method,
                    url=warc_url,
                    timeout_seconds=self.timeout_seconds,
                    headers={"range": f"bytes={record['offset']}-{end}"},
                    allow_binary=True,
                )
                if error is not None:
                    probes.append(error)
                    continue
                assert ranged is not None
                parsed = _parse_warc(ranged, expected, record["length"])
                if parsed is None:
                    probes.append(
                        _make_probe(
                            strategy_name=self.name,
                            strategy_version=self.version,
                            method=self.method,
                            url=warc_url,
                            outcome=AcquisitionOutcome.REJECTED,
                            reason="invalid_or_mismatched_warc_record",
                            response=ranged,
                        )
                    )
                    continue
                article, warc_date = parsed
                reason = _validated_article(article, request)
                if reason is not None:
                    probes.append(
                        _make_probe(
                            strategy_name=self.name,
                            strategy_version=self.version,
                            method=self.method,
                            url=warc_url,
                            outcome=AcquisitionOutcome.REJECTED,
                            reason=reason,
                            response=article,
                            snapshot_time=warc_date or _timestamp(record["timestamp"]),
                        )
                    )
                    continue
                probes.append(
                    _make_probe(
                        strategy_name=self.name,
                        strategy_version=self.version,
                        method=self.method,
                        url=warc_url,
                        outcome=AcquisitionOutcome.SUCCEEDED,
                        response=article,
                        equivalence=SourceEquivalenceClass.HISTORICAL_ORIGINAL,
                        evidence=EvidenceClass.HISTORICAL_PRIMARY,
                        snapshot_time=warc_date or _timestamp(record["timestamp"]),
                        quality=_quality(article.decoded_body, request.title),
                    )
                )
                return tuple(probes)
        if not any(probe.outcome is AcquisitionOutcome.REJECTED for probe in probes):
            probes.append(
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="no_matching_commoncrawl_record",
                )
            )
        return tuple(probes)


def _memento_entries(body: bytes) -> list[tuple[str, datetime | None]]:
    try:
        document = json.loads(body)
    except (UnicodeError, json.JSONDecodeError):
        return []
    found: list[tuple[str, datetime | None]] = []

    def visit(value: object) -> None:
        if isinstance(value, dict):
            rel = value.get("rel", "")
            rel_values = rel if isinstance(rel, list) else str(rel).split()
            if any(str(item).casefold() == "memento" for item in rel_values):
                url = value.get("uri") or value.get("url") or value.get("href")
                if isinstance(url, str):
                    stamp = (
                        value.get("datetime")
                        or value.get("memento_datetime")
                        or value.get("timestamp")
                    )
                    found.append((url, _timestamp(stamp)))
            for nested in value.values():
                visit(nested)
        elif isinstance(value, list):
            for nested in value:
                visit(nested)

    visit(document)
    return found


@dataclass(frozen=True, slots=True)
class MementoArchiveStrategy:
    name: str = "memento_archive"
    version: str = "1"
    method: SourceAcquisitionMethod = SourceAcquisitionMethod.MEMENTO_ARCHIVE
    enabled: bool = True
    timeout_seconds: float = 15.0
    endpoint: str = "https://timetravel.mementoweb.org/api/json"

    @property
    def capability_state(self) -> AcquisitionCapabilityState:
        return AcquisitionCapabilityState.IMPLEMENTED

    async def acquire(self, request: AcquisitionRequest) -> tuple[AcquisitionProbe, ...]:
        denied = _eligible_original(
            request, name=self.name, version=self.version, method=self.method
        )
        if denied is not None:
            return (denied,)
        original = _canonical(request.requested_url)
        if original is None:
            return (
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=request.requested_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="invalid_original_url",
                ),
            )
        target_date = (request.published_at or datetime.now(UTC).date()).isoformat()
        lookup_url = f"{self.endpoint.rstrip('/')}/{target_date}/{quote(original, safe=':/?=&%')}"
        lookup, error = await _fetch(
            request,
            name=self.name,
            version=self.version,
            method=self.method,
            url=lookup_url,
            timeout_seconds=self.timeout_seconds,
        )
        if error is not None:
            return (error,)
        assert lookup is not None
        if lookup.status != 200:
            return (
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=lookup_url,
                    outcome=AcquisitionOutcome.FAILED,
                    reason="memento_lookup_http_status_not_200",
                    response=lookup,
                ),
            )
        probes = [
            _make_probe(
                strategy_name=self.name,
                strategy_version=self.version,
                method=self.method,
                url=lookup_url,
                outcome=AcquisitionOutcome.LOOKUP_SUCCEEDED,
                response=lookup,
            )
        ]
        entries = [
            (url, stamp)
            for url, stamp in _memento_entries(lookup.decoded_body)
            if _canonical(url) is not None and is_credential_free_url(url)
        ]
        if not entries:
            probes.append(
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=lookup_url,
                    outcome=AcquisitionOutcome.REJECTED,
                    reason="no_valid_memento_relation",
                )
            )
            return tuple(probes)
        for snapshot_url, snapshot_time in entries:
            snapshot, error = await _fetch(
                request,
                name=self.name,
                version=self.version,
                method=self.method,
                url=snapshot_url,
                timeout_seconds=self.timeout_seconds,
            )
            if error is not None:
                probes.append(error)
                continue
            assert snapshot is not None
            reason = _validated_article(snapshot, request)
            if reason is not None:
                probes.append(
                    _make_probe(
                        strategy_name=self.name,
                        strategy_version=self.version,
                        method=self.method,
                        url=snapshot_url,
                        outcome=AcquisitionOutcome.REJECTED,
                        reason=reason,
                        response=snapshot,
                        snapshot_time=snapshot_time,
                    )
                )
                continue
            probes.append(
                _make_probe(
                    strategy_name=self.name,
                    strategy_version=self.version,
                    method=self.method,
                    url=snapshot_url,
                    outcome=AcquisitionOutcome.SUCCEEDED,
                    response=snapshot,
                    equivalence=SourceEquivalenceClass.HISTORICAL_ORIGINAL,
                    evidence=EvidenceClass.HISTORICAL_PRIMARY,
                    snapshot_time=snapshot_time,
                    quality=_quality(snapshot.decoded_body, request.title),
                )
            )
            break
        return tuple(probes)


def existing_archive_probe(collection: object) -> AcquisitionProbe:
    """Describe an archive already attached to a collection, without fetching it."""
    source_document_id = getattr(collection, "source_document_id", None)
    requested_url = str(getattr(collection, "requested_url", ""))
    exists = source_document_id is not None
    outcome = AcquisitionOutcome.SUCCEEDED if exists else AcquisitionOutcome.SKIPPED
    quality = AcquisitionQuality(
        text_chars=0,
        word_count=0,
        title_similarity=1.0,
        language=None,
        boilerplate_ratio=0.0,
        duplicate_ratio=0.0,
        truncated=False,
        challenge_detected=False,
        paywall_detected=False,
        score=0.0,
        reasons=("existing_archive_attached",) if exists else (),
    )
    return _make_probe(
        strategy_name="existing_archive",
        strategy_version="1",
        method=SourceAcquisitionMethod.EXISTING_ARCHIVE,
        url=requested_url,
        outcome=outcome,
        reason=None if exists else "no_existing_archive",
        quality=quality if exists else None,
    )
