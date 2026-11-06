"""Typed, deterministic policy and provenance contracts for acquisition."""

from __future__ import annotations

import ipaddress
import math
import re
from dataclasses import dataclass
from enum import StrEnum
from urllib.parse import unquote, urlsplit


class AcquisitionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_CONFIGURED = "not_configured"
    POLICY_SKIPPED = "policy_skipped"
    NEEDS_RECONCILIATION = "needs_reconciliation"
    LOOKUP_SUCCEEDED = "lookup_succeeded"
    METADATA_SUCCEEDED = "metadata_succeeded"
    ARCHIVED = "archived"


class AcquisitionPolicyDecision(StrEnum):
    ALLOWED = "allowed"
    DENIED_PUBLIC_ACCESS = "denied_public_access"
    DENIED_DOMAIN_POLICY = "denied_domain_policy"
    DENIED_PRIVATE_DESTINATION = "denied_private_destination"
    DENIED_CREDENTIALS = "denied_credentials"
    DENIED_EXTERNAL_LLM = "denied_external_llm"
    DENIED_TLP = "denied_tlp"
    DENIED_PROVIDER_DISCLOSURE = "denied_provider_disclosure"
    NOT_CONFIGURED = "not_configured"


class SourceEquivalenceClass(StrEnum):
    EXACT_ORIGINAL = "exact_original"
    HISTORICAL_ORIGINAL = "historical_original"
    VERIFIED_PUBLISHER_EQUIVALENT = "verified_publisher_equivalent"
    AUTHORIZED_REPUBLICATION = "authorized_republication"
    INDEPENDENT_CORROBORATION = "independent_corroboration"
    UNKNOWN_MATCH = "unknown_match"


class EvidenceClass(StrEnum):
    PRIMARY_OBSERVED = "primary_observed"
    HISTORICAL_PRIMARY = "historical_primary"
    VERIFIED_EQUIVALENT = "verified_equivalent"
    AUTHORIZED_REPUBLICATION = "authorized_republication"
    INDEPENDENT_CORROBORATION = "independent_corroboration"
    OCR_DERIVED = "ocr_derived"
    MODEL_CONTEXTUAL_UNVERIFIED = "model_contextual_unverified"
    UNKNOWN = "unknown"


class AcquisitionCapabilityState(StrEnum):
    IMPLEMENTED = "implemented"
    DISABLED_BY_CONFIGURATION = "disabled_by_configuration"
    DEPENDENCY_UNAVAILABLE = "dependency_unavailable"
    BLOCKED_BY_POLICY = "blocked_by_policy"
    BLOCKED_BY_SECURITY = "blocked_by_security"
    NOT_IMPLEMENTED = "not_implemented"


class AcquisitionObservationKind(StrEnum):
    HTTP_RESPONSE = "http_response"
    BROWSER_DOM = "browser_dom"
    BROWSER_NETWORK_JSON = "browser_network_json"
    EMBEDDED_STRUCTURED_DATA = "embedded_structured_data"
    PUBLISHER_PDF = "publisher_pdf"
    IMAGE = "image"
    MODEL_TRANSCRIPTION = "model_transcription"
    OCR_TEXT = "ocr_text"


_MAX_LANGUAGE_LENGTH = 48
_MAX_REASON_LENGTH = 240
_MAX_QUALITY_REASONS = 16
_MAX_POLICY_ITEMS = 128
_MAX_DOMAIN_LENGTH = 253
_SENSITIVE_QUERY_KEY = re.compile(
    r"(?:^|[_-])(?:token|secret|signature|sig|auth|apikey|api_key|password|credential)"
    r"(?:$|[_-])",
    re.IGNORECASE,
)
_SENSITIVE_VALUE = re.compile(
    r"(?:token|secret|signature|sig|auth|api[_-]?key|password|credential)"
    r"(?:[=/:-])[^/?&\s]{8,}",
    re.IGNORECASE,
)


def _clean_text(value: str | None, limit: int) -> str | None:
    if value is None:
        return None
    return value.strip()[:limit] or None


def _clean_domain(domain: str) -> str:
    value = domain.strip().rstrip(".").casefold()
    if len(value) > _MAX_DOMAIN_LENGTH:
        value = value[:_MAX_DOMAIN_LENGTH]
    try:
        return value.encode("idna").decode("ascii")
    except UnicodeError:
        return value


def _clean_domain_set(domains: frozenset[str]) -> frozenset[str]:
    return frozenset(
        normalized
        for raw_domain in tuple(domains)[:_MAX_POLICY_ITEMS]
        if (normalized := _clean_domain(raw_domain))
    )


def _host_matches(hostname: str, domain: str) -> bool:
    return domain == "*" or hostname == domain or hostname.endswith(f".{domain}")


def _looks_private_hostname(hostname: str) -> bool:
    return hostname == "localhost" or hostname.endswith(
        (".localhost", ".local", ".internal", ".home", ".lan")
    ) or "." not in hostname


def is_credential_free_url(url: str) -> bool:
    """Return whether a URL has no user-info or credential-like fields."""
    return _is_credential_free_url(url, depth=0)


def _is_credential_free_url(url: str, *, depth: int) -> bool:
    if depth > 3:
        return False
    try:
        parsed = urlsplit(url)
        if parsed.username or parsed.password:
            return False
        if any(
            _SENSITIVE_QUERY_KEY.search(unquote(part.split("=", 1)[0]))
            for part in parsed.query.split("&")
            if part
        ):
            return False
        if _SENSITIVE_VALUE.search(unquote(parsed.path)):
            return False
        nested_urls = re.findall(r"https?%3A%2F%2F[^&]+", parsed.query, flags=re.IGNORECASE)
        return all(
            _is_credential_free_url(unquote(nested_url), depth=depth + 1)
            for nested_url in nested_urls
        )
    except (TypeError, ValueError):
        return False


def credential_free_url(url: str) -> str:
    """Return only a URL origin, omitting paths, query data, and fragments."""
    try:
        parsed = urlsplit(url)
        if parsed.scheme.casefold() not in {"http", "https"} or not parsed.hostname:
            return "invalid-url"
        hostname = parsed.hostname.encode("idna").decode("ascii").casefold()
        if ":" in hostname:
            hostname = f"[{hostname}]"
        port = parsed.port
        default_port = 443 if parsed.scheme.casefold() == "https" else 80
        port_suffix = f":{port}" if port is not None and port != default_port else ""
        return f"{parsed.scheme.casefold()}://{hostname}{port_suffix}/"
    except (AttributeError, UnicodeError, ValueError):
        return "invalid-url"


@dataclass(frozen=True, slots=True)
class AcquisitionQuality:
    text_chars: int
    word_count: int
    title_similarity: float
    language: str | None
    boilerplate_ratio: float
    duplicate_ratio: float
    truncated: bool
    challenge_detected: bool
    paywall_detected: bool
    score: float
    reasons: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if isinstance(self.text_chars, bool) or self.text_chars < 0:
            raise ValueError("text_chars must be a non-negative count")
        if isinstance(self.word_count, bool) or self.word_count < 0:
            raise ValueError("word_count must be a non-negative count")
        for name in ("title_similarity", "boilerplate_ratio", "duplicate_ratio", "score"):
            value = getattr(self, name)
            if not math.isfinite(value) or not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be between 0 and 1")
        object.__setattr__(self, "language", _clean_text(self.language, _MAX_LANGUAGE_LENGTH))
        cleaned_reasons = tuple(
            cleaned
            for reason in self.reasons[:_MAX_QUALITY_REASONS]
            if (cleaned := _clean_text(reason, _MAX_REASON_LENGTH)) is not None
        )
        object.__setattr__(self, "reasons", cleaned_reasons)


@dataclass(frozen=True, slots=True)
class AcquisitionPolicyContext:
    allowed_domains: frozenset[str]
    blocked_domains: frozenset[str]
    external_llm_allowed: bool
    tlp: str
    do_not_submit: bool
    expected_languages: tuple[str, ...]
    allowed_syndication_domains: frozenset[str]
    third_party_provider_allowed: bool
    provider_configured: bool

    def __post_init__(self) -> None:
        object.__setattr__(self, "allowed_domains", _clean_domain_set(self.allowed_domains))
        object.__setattr__(self, "blocked_domains", _clean_domain_set(self.blocked_domains))
        object.__setattr__(
            self,
            "allowed_syndication_domains",
            _clean_domain_set(self.allowed_syndication_domains),
        )
        languages = tuple(
            cleaned
            for language in self.expected_languages[:_MAX_POLICY_ITEMS]
            if (cleaned := _clean_text(language, _MAX_LANGUAGE_LENGTH)) is not None
        )
        object.__setattr__(self, "expected_languages", languages)
        object.__setattr__(self, "tlp", self.tlp.strip().upper()[:16])

    def check_url(self, url: str) -> AcquisitionPolicyDecision:
        """Check syntax, embedded credentials, public destination, and domain policy."""
        try:
            parsed = urlsplit(url)
            scheme = parsed.scheme.casefold()
            hostname = parsed.hostname
            # Accessing .port also validates malformed port syntax.
            _ = parsed.port
        except (AttributeError, TypeError, ValueError):
            return AcquisitionPolicyDecision.DENIED_PUBLIC_ACCESS
        if scheme not in {"http", "https"} or not hostname:
            return AcquisitionPolicyDecision.DENIED_PUBLIC_ACCESS
        if not is_credential_free_url(url):
            return AcquisitionPolicyDecision.DENIED_CREDENTIALS
        try:
            normalized_host = hostname.encode("idna").decode("ascii").casefold().rstrip(".")
        except UnicodeError:
            return AcquisitionPolicyDecision.DENIED_PUBLIC_ACCESS
        try:
            address = ipaddress.ip_address(normalized_host)
        except ValueError:
            if _looks_private_hostname(normalized_host):
                return AcquisitionPolicyDecision.DENIED_PRIVATE_DESTINATION
        else:
            if not address.is_global:
                return AcquisitionPolicyDecision.DENIED_PRIVATE_DESTINATION
        if any(_host_matches(normalized_host, domain) for domain in self.blocked_domains):
            return AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
        if self.allowed_domains and not any(
            _host_matches(normalized_host, domain) for domain in self.allowed_domains
        ):
            return AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
        return AcquisitionPolicyDecision.ALLOWED

    def check_destination(
        self,
        url: str,
        *,
        external_llm: bool = False,
        third_party_provider: bool = False,
    ) -> AcquisitionPolicyDecision:
        """Apply destination safety and the requested external disclosure gates."""
        url_decision = self.check_url(url)
        if url_decision is not AcquisitionPolicyDecision.ALLOWED:
            return url_decision
        if not (external_llm or third_party_provider):
            return AcquisitionPolicyDecision.ALLOWED
        if self.tlp == "RED":
            return AcquisitionPolicyDecision.DENIED_TLP
        if external_llm and not self.external_llm_allowed:
            return AcquisitionPolicyDecision.DENIED_EXTERNAL_LLM
        if third_party_provider:
            if self.do_not_submit or not self.third_party_provider_allowed:
                return AcquisitionPolicyDecision.DENIED_PROVIDER_DISCLOSURE
            if not self.provider_configured:
                return AcquisitionPolicyDecision.NOT_CONFIGURED
        return AcquisitionPolicyDecision.ALLOWED


def evidence_class_for_equivalence(
    equivalence: SourceEquivalenceClass | None,
) -> EvidenceClass:
    """Map source equivalence to the strongest evidence class it supports."""
    mapping = {
        SourceEquivalenceClass.EXACT_ORIGINAL: EvidenceClass.PRIMARY_OBSERVED,
        SourceEquivalenceClass.HISTORICAL_ORIGINAL: EvidenceClass.HISTORICAL_PRIMARY,
        SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT: EvidenceClass.VERIFIED_EQUIVALENT,
        SourceEquivalenceClass.AUTHORIZED_REPUBLICATION: EvidenceClass.AUTHORIZED_REPUBLICATION,
        SourceEquivalenceClass.INDEPENDENT_CORROBORATION: EvidenceClass.INDEPENDENT_CORROBORATION,
        SourceEquivalenceClass.UNKNOWN_MATCH: EvidenceClass.UNKNOWN,
    }
    if equivalence is None:
        return EvidenceClass.UNKNOWN
    return mapping[equivalence]
