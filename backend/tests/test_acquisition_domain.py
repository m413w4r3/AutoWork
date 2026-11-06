from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from cti_app.domain.acquisition import (
    AcquisitionCapabilityState,
    AcquisitionObservationKind,
    AcquisitionOutcome,
    AcquisitionPolicyContext,
    AcquisitionPolicyDecision,
    AcquisitionQuality,
    EvidenceClass,
    SourceEquivalenceClass,
    credential_free_url,
    evidence_class_for_equivalence,
    is_credential_free_url,
)
from cti_app.domain.classification import TLP


def _policy(
    *,
    allowed_domains: frozenset[str] = frozenset({"example.com"}),
    blocked_domains: frozenset[str] = frozenset(),
    external_llm_allowed: bool = True,
    tlp: TLP | str = TLP.CLEAR,
    do_not_submit: bool = False,
) -> AcquisitionPolicyContext:
    return AcquisitionPolicyContext(
        allowed_domains=allowed_domains,
        blocked_domains=blocked_domains,
        external_llm_allowed=external_llm_allowed,
        tlp=tlp,
        do_not_submit=do_not_submit,
        expected_languages=(" en ", "fr"),
        allowed_syndication_domains=frozenset({"syndication.example.com"}),
        third_party_provider_allowed=True,
        provider_configured=True,
    )


def test_control_enums_are_str_enums_with_string_compatibility() -> None:
    assert isinstance(AcquisitionOutcome.SUCCEEDED, str)
    assert AcquisitionOutcome.SUCCEEDED == "succeeded"
    assert AcquisitionPolicyDecision.DENIED_TLP == "denied_tlp"
    assert SourceEquivalenceClass.EXACT_ORIGINAL == "exact_original"
    assert EvidenceClass.PRIMARY_OBSERVED == "primary_observed"
    assert AcquisitionCapabilityState.IMPLEMENTED == "implemented"
    assert AcquisitionObservationKind.HTTP_RESPONSE == "http_response"


def test_acquisition_quality_validates_ranges_and_sanitizes_text() -> None:
    quality = AcquisitionQuality(
        text_chars=8,
        word_count=2,
        title_similarity=1.0,
        language="  en  ",
        boilerplate_ratio=0.25,
        duplicate_ratio=0.0,
        truncated=False,
        challenge_detected=False,
        paywall_detected=False,
        score=0.8,
        reasons=("  clean  ", " "),
    )
    assert quality.language == "en"
    assert quality.reasons == ("clean",)
    with pytest.raises(FrozenInstanceError):
        quality.score = 0.5  # type: ignore[misc]
    with pytest.raises(ValueError):
        AcquisitionQuality(
            text_chars=-1,
            word_count=0,
            title_similarity=1.0,
            language=None,
            boilerplate_ratio=0.0,
            duplicate_ratio=0.0,
            truncated=False,
            challenge_detected=False,
            paywall_detected=False,
            score=1.0,
        )
    with pytest.raises(ValueError):
        AcquisitionQuality(
            text_chars=0,
            word_count=0,
            title_similarity=1.01,
            language=None,
            boilerplate_ratio=0.0,
            duplicate_ratio=0.0,
            truncated=False,
            challenge_detected=False,
            paywall_detected=False,
            score=1.0,
        )


def test_policy_context_strips_and_limits_untrusted_text() -> None:
    policy = AcquisitionPolicyContext(
        allowed_domains=frozenset({" EXAMPLE.COM. "}),
        blocked_domains=frozenset(),
        external_llm_allowed=True,
        tlp=" clear ",
        do_not_submit=False,
        expected_languages=(" en ", " " * 4),
        allowed_syndication_domains=frozenset(),
        third_party_provider_allowed=True,
        provider_configured=True,
    )
    assert policy.allowed_domains == frozenset({"example.com"})
    assert policy.tlp is TLP.CLEAR
    assert policy.expected_languages == ("en",)
    assert policy.check_url("https://news.example.com/story") is AcquisitionPolicyDecision.ALLOWED


def test_policy_context_allows_only_public_allowed_domains() -> None:
    policy = _policy(blocked_domains=frozenset({"blocked.example.com"}))
    assert policy.check_url("https://example.com/article") is AcquisitionPolicyDecision.ALLOWED
    assert policy.check_url("https://news.example.com/article") is AcquisitionPolicyDecision.ALLOWED
    assert (
        policy.check_url("https://blocked.example.com/article")
        is AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
    )
    assert (
        policy.check_url("https://unlisted.test/article")
        is AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
    )
    assert (
        policy.check_url("http://127.0.0.1/article")
        is AcquisitionPolicyDecision.DENIED_PRIVATE_DESTINATION
    )
    assert (
        policy.check_url("file:///etc/passwd")
        is AcquisitionPolicyDecision.DENIED_PUBLIC_ACCESS
    )


def test_empty_domain_allowlist_allows_public_hosts_but_honors_blocklist() -> None:
    policy = _policy(
        allowed_domains=frozenset(),
        blocked_domains=frozenset({"blocked.example.net"}),
    )
    assert (
        policy.check_url("https://public.example.net/article")
        is AcquisitionPolicyDecision.ALLOWED
    )
    assert (
        policy.check_url("https://blocked.example.net/article")
        is AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
    )
    assert (
        _policy(allowed_domains=frozenset({"example.com"})).check_url(
            "https://public.example.net/article"
        )
        is AcquisitionPolicyDecision.DENIED_DOMAIN_POLICY
    )


@pytest.mark.parametrize(
    "url",
    (
        "https://user:password@example.com/article",
        "https://example.com/article?api_key=privatevalue123",
        "https://example.com/path/token=privatevalue123",
    ),
)
def test_policy_context_denies_credential_bearing_urls(url: str) -> None:
    assert _policy().check_url(url) is AcquisitionPolicyDecision.DENIED_CREDENTIALS
    assert not is_credential_free_url(url)


def test_disclosure_policy_checks_external_llm_do_not_submit_and_tlp() -> None:
    url = "https://example.com/article"
    assert (
        _policy(external_llm_allowed=False).check_destination(url, external_llm=True)
        is AcquisitionPolicyDecision.DENIED_EXTERNAL_LLM
    )
    assert _policy(do_not_submit=True, tlp=TLP.AMBER).check_destination(
        url, external_llm=True
    ) is AcquisitionPolicyDecision.ALLOWED
    assert (
        _policy(tlp=TLP.RED).check_destination(url, external_llm=True)
        is AcquisitionPolicyDecision.DENIED_TLP
    )
    assert (
        _policy(tlp="red", external_llm_allowed=True).check_destination(
            url, external_llm=True
        )
        is AcquisitionPolicyDecision.DENIED_TLP
    )
    assert (
        _policy().check_destination(url, third_party_provider=True)
        is AcquisitionPolicyDecision.ALLOWED
    )
    assert (
        _policy(do_not_submit=True).check_destination(url, third_party_provider=True)
        is AcquisitionPolicyDecision.DENIED_PROVIDER_DISCLOSURE
    )


def test_invalid_tlp_fails_closed_during_policy_construction() -> None:
    with pytest.raises(ValueError, match="recognized TLP"):
        _policy(tlp="UNKNOWN_LABEL")


def test_credential_free_url_returns_only_the_origin() -> None:
    assert (
        credential_free_url("https://user:secret@example.com/private/path?token=abc")
        == "https://example.com/"
    )
    assert credential_free_url("javascript:alert(1)") == "invalid-url"


@pytest.mark.parametrize(
    ("equivalence", "expected"),
    (
        (SourceEquivalenceClass.EXACT_ORIGINAL, EvidenceClass.PRIMARY_OBSERVED),
        (SourceEquivalenceClass.HISTORICAL_ORIGINAL, EvidenceClass.HISTORICAL_PRIMARY),
        (
            SourceEquivalenceClass.VERIFIED_PUBLISHER_EQUIVALENT,
            EvidenceClass.VERIFIED_EQUIVALENT,
        ),
        (
            SourceEquivalenceClass.AUTHORIZED_REPUBLICATION,
            EvidenceClass.AUTHORIZED_REPUBLICATION,
        ),
        (
            SourceEquivalenceClass.INDEPENDENT_CORROBORATION,
            EvidenceClass.INDEPENDENT_CORROBORATION,
        ),
        (SourceEquivalenceClass.UNKNOWN_MATCH, EvidenceClass.UNKNOWN),
        (None, EvidenceClass.UNKNOWN),
    ),
)
def test_equivalence_maps_to_conservative_evidence_class(
    equivalence: SourceEquivalenceClass | None, expected: EvidenceClass
) -> None:
    assert evidence_class_for_equivalence(equivalence) is expected
