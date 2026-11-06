# M16 — Resilient Article Acquisition Engine: implementation contracts

This note is the integration contract for the M16 work packages. It refines the
mission's concepts to repository types. It does not replace accepted ADRs,
`backend/AGENTS.md`, or the production contracts.

## Core invariants

1. PostgreSQL, versioned artifacts and MinIO blobs remain canonical. A model
   output is never primary evidence.
2. `encoded_body` is the **observed artifact** (HTTP payload, raw JSON, PDF,
   image, original 403 HTML). `decoded_body` is the **article text derived from
   that artifact** (rendered DOM, extracted JSON/PDF/OCR text). Both blobs and
   both SHA-256 values are archived.
3. `acquisition_method`, `source_equivalence_class` and `evidence_class` state
   how the bytes were obtained and what authority they carry. `ARCHIVED` alone
   never means "verified primary publication".
4. A fallback never overwrites an existing direct/manual archive. A manual
   archive may explicitly supersede a fallback under the existing CAS/row-lock
   rules while the old document and blobs remain immutable.
5. All network access uses `SafeHttpCollector` (DNS pinning, SSRF policy,
   domain policy, size/time limits), including secondary artifact requests.
   Chromium may only reach the network through the enforcing egress proxy.
6. No CAPTCHA solving, stealth, fingerprint spoofing, proxy rotation,
   authentication bypass, or credential use. An explicit denial is terminal for
   that destination. `do_not_submit` is not a VT/Bridge veto; it keeps its
   existing contract-specific meaning.
7. Tests are offline: fake transports, fake browser, fake provider, fake OCR.

## Domain types — `cti_app/domain/acquisition.py` (new)

Additive enum members on the existing `SourceAcquisitionMethod` are allowed;
existing values keep their meaning for old rows.

```python
class AcquisitionOutcome(StrEnum):
    SUCCEEDED = "succeeded"
    REJECTED = "rejected"                 # content fetched but not the article
    FAILED = "failed"
    SKIPPED = "skipped"
    NOT_CONFIGURED = "not_configured"     # capability/dependency absent
    POLICY_SKIPPED = "policy_skipped"     # policy/authorization denied
    NEEDS_RECONCILIATION = "needs_reconciliation"

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

@dataclass(frozen=True, slots=True)
class AcquisitionQuality:
    text_chars: int
    word_count: int
    title_similarity: float            # 0..1, 1 when no title was expected
    language: str | None
    boilerplate_ratio: float           # 0..1
    duplicate_ratio: float             # 0..1
    truncated: bool
    challenge_detected: bool
    paywall_detected: bool
    score: float                        # 0..1 deterministic aggregate
    reasons: tuple[str, ...] = ()

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
```

Method mapping (extend `SourceAcquisitionMethod`; keep old members):

| Mission method | Persisted method value |
|---|---|
| existing_archive | `EXISTING_ARCHIVE` |
| direct_http | `DIRECT_HTTP` (existing) |
| canonical/amp/print/mobile variant | `CANONICAL_VARIANT`, `AMP_VARIANT`, `PRINT_VARIANT`, `MOBILE_VARIANT` |
| rss_atom_jsonfeed | `RSS_ATOM_JSONFEED` |
| publisher_public_api | `PUBLISHER_PUBLIC_API` |
| embedded_structured_data | `EMBEDDED_STRUCTURED_DATA` |
| playwright_chromium_dom | `PLAYWRIGHT_CHROMIUM_DOM` |
| playwright_network_response | `PLAYWRIGHT_NETWORK_RESPONSE` |
| playwright_embedded_state | `PLAYWRIGHT_EMBEDDED_STATE` |
| playwright_firefox / webkit | `PLAYWRIGHT_FIREFOX`, `PLAYWRIGHT_WEBKIT` |
| multi_extractor | `MULTI_EXTRACTOR` |
| wayback_snapshot | `WAYBACK_SNAPSHOT` (keep `WAYBACK` for old rows) |
| commoncrawl_warc | `COMMONCRAWL_WARC` (keep `COMMON_CRAWL`) |
| memento_archive | `MEMENTO_ARCHIVE` (keep `MEMENTO`) |
| exact_title_search | `EXACT_TITLE_SEARCH` |
| publisher_alternative_url | `PUBLISHER_ALTERNATIVE_URL` |
| authorized_syndication | `AUTHORIZED_SYNDICATION` |
| publisher_pdf | `PUBLISHER_PDF` |
| visual_ocr | `VISUAL_OCR` |
| official_newsletter_archive | `OFFICIAL_NEWSLETTER_ARCHIVE` |
| third_party_extraction | `THIRD_PARTY_EXTRACTION` |
| bridge_source_discovery | `BRIDGE_SOURCE_DISCOVERY` |
| bridge_transcription | `BRIDGE_TRANSCRIPTION` (existing) |
| manual_human_archive | `MANUAL_UPLOAD` (existing; API stays manual) |

## Probe and request — `application/acquisition_fallbacks.py`

`AcquisitionProbe` keeps `outcome: AcquisitionOutcome` (StrEnum compares equal to
its string value, so existing `== "succeeded"` checks remain valid) and gains:

```python
policy_decision: AcquisitionPolicyDecision = ALLOWED
capability_state: AcquisitionCapabilityState = IMPLEMENTED
equivalence_class: SourceEquivalenceClass | None = None
evidence_class: EvidenceClass | None = None
quality: AcquisitionQuality | None = None
archive_snapshot_time: datetime | None = None
publication_date: date | None = None
publisher: str | None = None
model_generated: bool = False
primary_source_verified: bool = False
ioc_primary_verified: bool = False
```

`AcquisitionRequest` gains `policy: AcquisitionPolicyContext` and
`expected_languages` (already present).

`AcquisitionStrategy` protocol gains optional `capability_state` and
`description`; `acquire` is unchanged.

## Orchestrator — `application/acquisition_orchestrator.py` (new)

```python
@dataclass(frozen=True, slots=True)
class AcquisitionRunResult:
    outcome: AcquisitionOutcome
    probes: tuple[AcquisitionProbe, ...]
    winner: AcquisitionProbe | None
    reason_code: str | None
    attempts: int
    deadline_exceeded: bool
    cancelled: bool

class ArticleAcquisitionOrchestrator:
    def __init__(self, strategies, *, deadline_seconds, max_attempts,
                 per_domain_concurrency, clock=...): ...
    async def run(self, request, *, cancellation_check=None) -> AcquisitionRunResult: ...
```

Behavior: ordered strategies; skip disabled with `DISABLED_BY_CONFIGURATION`;
per-strategy timeout; global `asyncio.timeout`; at most `max_attempts` probes;
one semaphore per hostname around `request.fetch`; cancellation propagates
`JobCancelledError`; the first probe with `SUCCEEDED` and an accepted response
wins. A strategy that returns several probes stops the run on the first success.
All probes are retained for diagnostics.

## Strategy adapters

- `application/article_extraction.py` (new): deterministic extraction from HTML,
  JSON-LD, embedded state (`__NEXT_DATA__`, `__NUXT__`, `__INITIAL_STATE__`),
  RSS/Atom/JSON Feed, CMS JSON, PDF text, OCR text. Returns
  `ExtractionCandidate(text, title, publisher, published_at, canonical_url,
  method, quality)`. Quality selects by score, not length alone. IOC literals
  are copied byte-for-byte from the extracted text; normalization never runs
  here.
- `application/acquisition_sources.py` (new): archive adapters (Wayback CDX,
  Common Crawl index/WARC, Memento), rediscovery (exact-title search adapter
  that is `NOT_CONFIGURED` unless a public endpoint is configured; sitemap /
  publisher alternative URL; authorized syndication classification), PDF and
  OCR adapters, third-party provider port (disabled by default, policy-gated).
- `application/browser_acquisition.py` (new): Playwright adapter calling the
  isolated renderer; DOM, captured public JSON responses, embedded state; typed
  failure when the secure egress proxy is not configured.
- `sidecars/browser_egress_proxy/` (new): enforcing forward proxy. It resolves
  and validates every destination (public IP only; configured domain policy),
  handles CONNECT and absolute-form HTTP, blocks dangerous schemes, caps
  connection bytes and idle time, and never forwards to loopback/private/
  link-local/metadata addresses. The renderer has no other network.
- `sidecars/playwright_acquirer/` (modified): `/render` accepts either
  `html` (offline render, all secondary requests aborted) or `url` (navigate
  through the configured proxy), returns `rendered_html` plus bounded metadata
  and captured JSON bodies (no response headers with credentials).

## Provenance payload on `source.archived_fallback`

Add (only applicable fields required):

```
acquisition_method, strategy_name, strategy_version, duration_ms,
policy_snapshot_id, acquisition_job_id, actor,
original_url, requested_variant_url, final_url,
publisher, article_title, publication_date_if_known,
archive_snapshot_time, content_type, raw_blob_reference, decoded_blob_reference,
sha256 (decoded), encoded_sha256, attempt_id,
source_equivalence_class, evidence_class, quality_assessment,
model_run_id, consultation_id, prompt_sha256, response_sha256,
model_generated, primary_source_verified, ioc_primary_verified
```

`quality_assessment` is a compact JSON object, never the article body. Logs never
contain source bodies, URLs with credentials, prompts or provider keys.

## Operator fallback

`POST /api/subjects/{subject_id}/sources/{collection_id}/fallback-acquire`
returns `202 {job_id, duplicate}`. Job kind `source.fallback_acquire`,
idempotency key
`source.fallback_acquire:{subject}:{collection}:{policy_snapshot}:{attempt_round}`.
It is authorized by the existing actor identity, refuses non-fallbackable states,
runs only the orchestrator (no direct re-fetch), and is a no-op duplicate when
the same job key already exists. `EXTRACTION` never contacts the network.

## Routing rule (Lot D)

A CORE source whose `acquisition_method` is `BRIDGE_TRANSCRIPTION` (or whose
evidence class is `MODEL_CONTEXTUAL_UNVERIFIED`) is a critical source loss for
verified-publication purposes. The routing decision is `NEEDS_REVIEW` with
`CRITICAL_SOURCE_MISSING`, even if the transcription yielded contextual IOCs.
Independent corroboration is never promoted to CORE. Other CORE archives obtained
from the publisher or a verifiable historical archive keep their current routing.

## Work-package file ownership

| WP | Owns (write) | Must not touch |
|---|---|---|
| A-core | `domain/acquisition.py`, `domain/collection.py`, `application/acquisition_orchestrator.py`, `application/acquisition_fallbacks.py`, `application/collection.py`, `api/collection.py`, `config.py`, `workers/tasks.py`, `tests/test_acquisition_fallbacks.py`, `tests/test_acquisition_orchestrator.py` | sidecars, compose, extraction modules, routing |
| B-extract | `application/article_extraction.py`, `tests/test_article_extraction.py` | everything above |
| B-browser | `sidecars/**`, `infra/acquisition-playwright.Dockerfile`, `infra/acquisition-browser-proxy.Dockerfile`, `compose.yaml`, `application/browser_acquisition.py`, `tests/test_browser_acquisition.py`, `tests/test_browser_egress_proxy.py` | backend collection/acquisition files |
| C-sources | `application/acquisition_sources.py`, `tests/test_acquisition_sources.py` | everything above |
| Integration | `application/acquisition_fallbacks.py`, `application/collection.py`, `application/production_*.py`, `domain/production_*.py`, tests | sidecars except agreed compose wiring |
| Docs | `docs/**` except this note | code |

## Acceptance oracles

Each work package must provide deterministic offline tests and report
`command`, exit code, pass/fail/skip counts. The final matrix covers the
mission's cases 01–24 in `backend/tests/test_acquisition_matrix.py`.
