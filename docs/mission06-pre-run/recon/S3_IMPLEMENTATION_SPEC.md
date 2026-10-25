# M06 S3 implementation specification

Base reviewed: `7059ebbdd891b98d64f17c026fd3f6169c174063`.
Scope: read-only design. This file is the only requested deliverable; no code or tests were changed/run.

## Evidence and unresolved source gaps

- **VERIFIED —** D6 defines `ExternalEffectCoordinator.send_one` as the common seam, a PostgreSQL consume commit as the authorization cut, and one permit per physical request. [D6 §1–3](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:14)
- **VERIFIED —** Current operator path persists `RUNNING` and reservation before dispatch; current callback checks are not transactional with invocation state. [investigation_service.py:1249](../../backend/src/cti_app/application/investigation_service.py:1249), [investigation_service.py:1361](../../backend/src/cti_app/application/investigation_service.py:1361), [investigation_service.py:1399](../../backend/src/cti_app/application/investigation_service.py:1399)
- **VERIFIED —** Current ORM has envelope, invocation and budget reservation/amount rows; the reservation is already separate persisted rows. [investigation.py:59](../../backend/src/cti_app/infrastructure/database/models/investigation.py:59), [investigation.py:108](../../backend/src/cti_app/infrastructure/database/models/investigation.py:108), [investigation.py:182](../../backend/src/cti_app/infrastructure/database/models/investigation.py:182)
- **UNKNOWN —** Required `docs/mission06-pre-run/recon/S3_SEAM.md` is absent from this worktree, so its named anchors could not be followed.
- **UNKNOWN —** `backend/migrations/versions/` contains `0001_baseline.py` and `0002_c2_experiments.py` only; `0003_osint_open_policy` and its exact revision identifier are absent here. The requested 0004 parent must be checked against that migration before implementation. [ADR-0007](../../docs/adr/ADR-0007-frozen-baseline-and-c2-migration.md:21)
- **UNKNOWN —** SDK/proxy retries, provider receipt, and every deployed composition cannot be established by source review or fake transports. [D6 §3, §13](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:248)

## 1. Domain contract and identity

**INFERENCE —** These are normative proposed types; names are new. They implement D6’s prepared-request and authority interface. [D6 §2](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:59)

```python
@dataclass(frozen=True)
class PreparedExternalRequest:
    family: str                         # server-owned operation, e.g. vt.file_report
    provider: str                       # stable configured provider name
    provider_scope: str                  # non-secret account/route identity
    normalizer_version: str              # operation-specific immutable version
    identity_version: str                 # exactly "effect-key.v1"
    canonical_request: Mapping[str, JSON] # safe normalized semantics only
    request_ref: str | None               # immutable safe artifact ref for large inputs
    canonical_request_sha256: str          # lowercase 64-hex
    application_request_sha256: str        # exact prepared method/target/headers/body image
    occurrence_id: UUID | None             # explicit refresh/poll/retry intent
    evidence_refs: tuple[EvidenceRef, ...]  # IDs, versions and hashes; no evidence bytes
    application_request: FrozenWireRequest # ephemeral; never persisted/logged

@dataclass(frozen=True)
class ExternalEffectAuthority:
    investigation_id: UUID
    invocation_id: UUID | None
    operation_key: str                     # persisted reservation key
    reservation_amounts: Mapping[str, int] # server-owned exact expected amounts
    expected_envelope_version: int
    approval_decision_id: UUID
    invocation_arguments_sha256: str | None
    bridge_turn_id: UUID | None
    model_run_id: UUID | None
    acquisition_attempt_id: UUID | None
    retrohunt_reservation_id: UUID | None

class EffectClaimState(StrEnum):
    CONSUMED = "CONSUMED"
    NEEDS_REVIEW = "NEEDS_REVIEW"          # ambiguous outcome
    DEFINITIVELY_NOT_SUBMITTED = "DEFINITIVELY_NOT_SUBMITTED"
    COMPLETED = "COMPLETED"

class ExternalEffectOutcome: ... # closed union below
```

- **INFERENCE —** `FrozenWireRequest` is immutable bytes: HTTP method, exact target bytes, ordered header name/value byte pairs and body bytes. It includes any signed URL/header only in memory. `provider_scope`, family, provider, normalizer and identity versions come from server registration/composition, never tool JSON. [D6 §2–3](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:80)
- **INFERENCE —** Canonical semantic JSON uses RFC 8785 JCS, UTF-8 without BOM, no insignificant whitespace; reject NaN/Infinity, invalid Unicode and non-JSON values. Restrict numbers to safe integers; encode decimal quantities and larger integers as normalized strings. Do no implicit Unicode normalization: operation normalizers own semantic casing, whitespace, IP/URL/hash and cursor rules.
- **INFERENCE —** `canonical_request_sha256 = lowercase_hex(SHA256(UTF8(JCS(canonical_request))))`. Evidence refs are sorted by `(kind,id,version,sha256)` unless their order changes operation meaning; semantically ordered lists remain ordered.
- **INFERENCE —** Encode the exact application wire image as JCS object `{format:"autowork.application-request.v1",method_b64,target_b64,headers_b64,body_b64}`. Each `_b64` is unpadded base64url of the exact bytes; `headers_b64` preserves header order and repeated headers. `application_request_sha256` is SHA-256 of that JCS UTF-8. Persist only the hash. The transport recomputes it immediately before I/O.
- **INFERENCE —** Occurrence digest input is JCS `{"kind":"default","version":"effect-occurrence.v1"}` when `occurrence_id is None`; its digest is `1fa53c303e03ce108082fc0b48f59f997ba2e3a303da05e763d09d6ce145fab7`. Otherwise hash JCS `{"kind":"explicit","occurrence_id":"<lowercase-hyphenated UUID>","version":"effect-occurrence.v1"}`. A nullable UUID is never concatenated ambiguously.
- **INFERENCE —** `effect_key = lowercase_hex(SHA256(b"autowork.effect-key.v1\\0" + UTF8(JCS({identity_version,investigation_id,provider,provider_scope,family,normalizer_version,canonical_request_sha256,occurrence_sha256}))))`; UUIDs are lowercase hyphenated strings. The database unique key is `(investigation_id,effect_key)`. Normalizer meaning/serialization changes require a version bump and explicit reuse compatibility decision.
- **INFERENCE —** `application_request_sha256` mismatch for an otherwise equal effect key is a hard `effect_binding_conflict`; no dispatch. Existing client `Idempotency-Key` and `ToolInvocation.operation_key` remain independent and unchanged. Operator `effect_occurrence_id` is optional UUID and participates in the operator request payload so exact-key replay still reuses the same invocation.
- **INFERENCE —** Closed outcomes: `Completed(claim_id,result_ref,evidence_refs)`, `Reused(claim_id,use_id,source_invocation_id,observation_refs,budget_delta=0)`, `Refused(reason,framework_no_send_proof)`, `DefinitelyNotSubmitted(claim_id,framework_proof)`, `Ambiguous(claim_id,NEEDS_REVIEW)`. `REUSED` is a use-lineage kind, never a claim state. Do not add public invocation statuses. [D6 §3](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:229)
- **INFERENCE —** Never persist/log/promote AutoWork credentials, signed URLs, raw secret headers, raw prepared wire requests, provider keys, passwords or secret-bearing headers. Only safe canonical semantics/references, hashes, status and typed error codes persist. Authorized CTI evidence remains complete in existing versioned artifacts with SHA-256/provenance; no truncation or new prompt ceiling. [backend/AGENTS.md](../../backend/AGENTS.md:46), [D6 §3](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:229)

## 2. `send_one`, permit and consume protocol

**INFERENCE —** Required order, implementing D6’s authorization cut. [D6 §2–3, §8](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:89)

1. Purely normalize and serialize one physical request. Validate family/provider registration, versions, safe canonical request, request/evidence refs, and both digests. No transport/client call is allowed in preparation.
2. Begin one PostgreSQL transaction at READ COMMITTED. Run the consume locks/checks below. Any failed check returns typed refusal with no permit and zero I/O.
3. Insert the claim directly as `CONSUMED` inside that transaction; do not commit `PREPARED`. Atomically move applicable family attempt and Bridge submission state to in-progress. Commit.
4. Only after the commit returns success, mint an opaque, process-local `ConsumedEffectPermit`; bind claim/use IDs, investigation, invocation/attempt IDs, effect key, family, provider/scope, envelope version/approval, request digest, and one-use nonce. No lock spans transport.
5. Call the permit-aware transport exactly once with the same frozen request. It checks issuer, unused nonce, claim/request/provider/family bindings and recomputed request digest immediately before handoff; burn the permit before calling the client.
6. Persist known result/evidence and CAS `CONSUMED -> COMPLETED`; framework-proven zero handoff may CAS to `DEFINITIVELY_NOT_SUBMITTED`. Timeout, cancellation after handoff, lost response, crash, unknown exception or outcome-write failure seals `NEEDS_REVIEW`, retains reservation and never resends.

**INFERENCE —** Lock order is universal and ordered; all rows are freshly reloaded under the same transaction:

1. `investigation_envelopes` row for investigation `FOR UPDATE` (authorization/revocation serialization).
2. Persisted stop/cancel state row `FOR UPDATE` if applicable; every stop/cancel writer must acquire envelope first, then state, or the consume check cannot be serialized.
3. `tool_invocations` row `FOR UPDATE` for registered invocations; omit for main Bridge.
4. `investigation_budget_reservations` row `(investigation_id,operation_key)` `FOR UPDATE`, then amount rows in sorted dimension order `FOR UPDATE`.
5. Insert `external_effect_claim` with unique `(investigation_id,effect_key)`; on conflict select the existing row `FOR UPDATE`, never mint another permit from it.
6. Family attempt `FOR UPDATE` (acquisition dispatch attempt or Retrohunt slot), then any source-association/sample rows needed to recheck restrictions in stable UUID order.
7. `model_conversation_turns` `FOR UPDATE`, then linked `model_runs` `FOR UPDATE` for Bridge/advisor.

- **INFERENCE —** Under those locks verify: investigation exists and is active; exact envelope version and approval ID; current authority kind and G2/G3/provider scope; TLP not RED; `external_llm_allowed`; `do_not_submit`; sample/source/owner restrictions; expiry and stop/cancel; no revocation; exact reserved operation key/status/amounts and remaining budget; invocation belongs to investigation, is `RUNNING`, has expected operation/schema/arguments digest; known normalizer/identity versions; canonical and application hashes; family attempt is the expected unconsumed state; Bridge turn/run IDs link to each other and are eligible. The locked envelope is the source of current policy. [D6 §3](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:189)
- **INFERENCE —** Envelope revoke-first means deny/no claim consumption/no send. Consume-first commit authorizes precisely that one request even if later revoked; all later requests reconsume and see revocation. A uniqueness conflict on a `CONSUMED`/`NEEDS_REVIEW` claim returns `NEEDS_REVIEW` without a second permit. A completed claim is reusable only for an explicitly declared family policy; otherwise return `FAILED/effect_occurrence_required`, release only the new still-RESERVED reservation, and send zero. A new explicit occurrence creates a different key and must pass all checks again.
- **UNKNOWN —** Current state mutators’ lock behavior has not been audited because the required seam report is absent; implementers must enforce the envelope-before-state order across stop/cancel and revocation writers before claiming race safety.

## 3. Permit-aware transports and request fan-out

- **VERIFIED —** VT tool adapters call `file_report`, `object_report`, paged `object_relationship`, paged `intelligence_search`, `behaviours`, and `behaviour_pcap`. [investigation_vt_tools.py:686](../../backend/src/cti_app/application/investigation_vt_tools.py:686), [investigation_vt_tools.py:890](../../backend/src/cti_app/application/investigation_vt_tools.py:890), [investigation_vt_tools.py:1035](../../backend/src/cti_app/application/investigation_vt_tools.py:1035), [investigation_vt_tools.py:1204](../../backend/src/cti_app/application/investigation_vt_tools.py:1204), [investigation_vt_tools.py:1400](../../backend/src/cti_app/application/investigation_vt_tools.py:1400)
- **VERIFIED —** VT HTTP routes use direct and streaming HTTPX entry points; download code loops over redirects. [virustotal.py:1253](../../backend/src/cti_app/infrastructure/virustotal.py:1253), [virustotal.py:2069](../../backend/src/cti_app/infrastructure/virustotal.py:2069)
- **VERIFIED —** Bridge makes first and retry submissions and has corrective/rate-limit loops; `before_submission` is optional. [bridge_investigator.py:1197](../../backend/src/cti_app/application/bridge_investigator.py:1197), [bridge_investigator.py:1207](../../backend/src/cti_app/application/bridge_investigator.py:1207), [bridge_investigator.py:1248](../../backend/src/cti_app/application/bridge_investigator.py:1248), [bridge_investigator.py:1312](../../backend/src/cti_app/application/bridge_investigator.py:1312), [bridge_investigator.py:1343](../../backend/src/cti_app/application/bridge_investigator.py:1343)
- **INFERENCE —** Public raw transport APIs require non-optional `ConsumedEffectPermit`; no direct client is exposed. Missing, copied, stale, expired, consumed or misbound permits raise typed refusal before socket/client send. Never treat a boolean or caller callback as authority. [D6 §3, §11](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:174)
- **INFERENCE —** Place checks at the lowest HTTP/model send primitive, not only at tool adapter entry: VT fallback routes, API reads, signed-CDN stream, Bridge submit, and advisor Bridge submit all call `send_one` for each physical request. Disable SDK retries and automatic redirects. Pagination cursor/page, fallback route, retry, redirect hop, correction and Retrohunt poll each prepare/consume a separate permit; unchanged retries get a fresh persisted occurrence. A failed family is deny-all/unavailable, never legacy fallback. [D6 §3, §10](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:174)
- **VERIFIED —** Acquisition calls `file_download`; existing redirects are explicit and the current completed attempt ledger is appended after request/result processing. [sample_acquisition.py:427](../../backend/src/cti_app/application/sample_acquisition.py:427), [sample_acquisition.py:533](../../backend/src/cti_app/application/sample_acquisition.py:533), [sample_acquisition.py:605](../../backend/src/cti_app/application/sample_acquisition.py:605)
- **INFERENCE —** Do not grant a download permit from the post-response append-only ledger. Create and commit a pre-send `sample_acquisition_dispatch_attempts` row in `PREPARED`, then lock/advance it at consume. Link the existing terminal `sample_acquisition_attempts` row to it afterward; keep local verified sample reuse outside `send_one`. This closes the current “attempt ID exists only after response” gap without rewriting existing attempt rows.

## 4. Migration `0004_external_effect_claims`

**INFERENCE —** Once the actual parent is present, header is `revision="0004_external_effect_claims"`, `down_revision="0003_osint_open_policy"`; do not edit 0001/0002. No backfill or dual-write. Use UUID, VARCHAR, BIGINT, TIMESTAMPTZ, JSONB; lowercase SHA-256 hex checks; VARCHAR + CHECK state enums. This is additive under ADR-0007. [D6 §10](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:465), [backend/AGENTS.md](../../backend/AGENTS.md:18)

- **INFERENCE — `sample_acquisition_dispatch_attempts`:** `id UUID PK`; `investigation_id UUID NOT NULL FK analyst_investigations.id RESTRICT`; `invocation_id UUID NULL FK tool_invocations.id RESTRICT`; `association_id UUID NOT NULL FK sample_associations.id RESTRICT`; `requested_hash VARCHAR(64)`, `hash_family VARCHAR(8)`, `reason VARCHAR(16)`; `state VARCHAR(32) CHECK PREPARED/SUBMISSION_IN_PROGRESS/COMPLETED/DEFINITIVELY_NOT_SUBMITTED/NEEDS_REVIEW`; `version BIGINT CHECK >=1`; `created_at,updated_at TIMESTAMPTZ`. Check hash length/family and state. Index `(investigation_id,state,created_at)` and `(invocation_id,created_at)`.
- **INFERENCE — `external_effect_claim`:** `id UUID PK`; `investigation_id UUID NOT NULL FK analyst_investigations.id RESTRICT`; nullable `invocation_id UUID FK tool_invocations.id RESTRICT`, `bridge_turn_id UUID FK model_conversation_turns.id RESTRICT`, `model_run_id UUID FK model_runs.id RESTRICT`, `acquisition_attempt_id UUID FK sample_acquisition_dispatch_attempts.id RESTRICT`, `retrohunt_reservation_id UUID FK retrohunt_reservations.id RESTRICT`; `provider VARCHAR(64)`, `provider_scope VARCHAR(255)`, `family VARCHAR(128)`, `identity_version VARCHAR(32)`, `normalizer_version VARCHAR(64)`, all NOT NULL; `effect_key,canonical_request_sha256,application_request_sha256,occurrence_sha256 CHAR(64) NOT NULL`; `occurrence_id UUID NULL`; `request_ref TEXT NULL`; `canonical_request JSONB NULL`; `evidence_refs JSONB NOT NULL DEFAULT []`; `operation_key VARCHAR(255) NOT NULL`; `reservation_amounts JSONB NOT NULL`; `envelope_version BIGINT NOT NULL`; `approval_decision_id UUID NOT NULL`; `invocation_arguments_sha256 CHAR(64) NULL`; `state VARCHAR(32) CHECK CONSUMED/NEEDS_REVIEW/DEFINITIVELY_NOT_SUBMITTED/COMPLETED`; `version BIGINT CHECK >=1`; `created_at,consumed_at,updated_at TIMESTAMPTZ NOT NULL`; `completed_at TIMESTAMPTZ NULL`; `last_error_code VARCHAR(64) NULL`.
- **INFERENCE — Claim checks/keys:** all four hashes lowercase hex; JSON object/array types; `occurrence_sha256` always non-null; `canonical_request` or `request_ref` present; `(bridge_turn_id IS NULL) = (model_run_id IS NULL)`; at least one source ID present; `UNIQUE(investigation_id,effect_key)`. FKs are restrictive. Index `(invocation_id,created_at)`, `(bridge_turn_id)`, `(acquisition_attempt_id)`, `(state,updated_at)`, `(family,provider,created_at)`.
- **INFERENCE — `external_effect_use`:** `id UUID PK`; `claim_id UUID NOT NULL FK external_effect_claim.id RESTRICT`; `invocation_id UUID NOT NULL FK tool_invocations.id RESTRICT`; `use_kind VARCHAR(16) CHECK SENT/REUSED`; `observation_refs JSONB NOT NULL DEFAULT [] CHECK array`; `budget_delta JSONB NOT NULL DEFAULT {} CHECK object`; `created_at TIMESTAMPTZ NOT NULL`; `UNIQUE(claim_id,invocation_id)`; index `(invocation_id,created_at)`. Original send records `SENT`; reuse records `REUSED`, source observation refs and exact zero-valued budget dimensions. Claims without ToolInvocation (main Bridge) retain their lineage in claim row.
- **INFERENCE — Acquisition correlation:** add nullable `dispatch_attempt_id UUID` to existing `sample_acquisition_attempts`, FK to `sample_acquisition_dispatch_attempts.id RESTRICT`, plus a partial unique index where non-null. Existing rows stay NULL; terminal outcome rows remain append-only.
- **INFERENCE — State transitions:** insert claim as `CONSUMED`; CAS to `COMPLETED`, `DEFINITIVELY_NOT_SUBMITTED` or `NEEDS_REVIEW`. Only framework-owned proof of no handoff permits the definitive state. Reconciliation is explicit CAS; `NEEDS_REVIEW` is never auto-resubmitted. `REUSED` exists only in use rows.
- **INFERENCE — Preflight/rollback:** fail before DDL on partial/conflicting objects; accept only absent or exhaustively verified canonical 0004 shape. Downgrade refuses if any claim/use/dispatch-attempt row exists or any legacy acquisition row links a dispatch attempt. On an empty disposable DB only, drop indexes/FKs/column and tables in reverse dependency order. Never downgrade live or drop evidence history. [ADR-0007](../../docs/adr/ADR-0007-frozen-baseline-and-c2-migration.md:21)

## 5. Wiring and family ownership

- **VERIFIED —** `ToolExecutionContext` currently has optional `pre_transport_check`; dispatcher has optional `pre_dispatch_check`; `RegistryToolDispatcher` calls adapters after that check. [investigation_tools.py:224](../../backend/src/cti_app/application/investigation_tools.py:224), [investigation_tools.py:415](../../backend/src/cti_app/application/investigation_tools.py:415), [investigation_tools.py:795](../../backend/src/cti_app/application/investigation_tools.py:795)
- **INFERENCE —** Add `external_effect_authority: ExternalEffectAuthority | None` to context. `InvestigationService.execute_tool_call` builds it only after persisting/committing the invocation and reservation; bind current invocation ID/version, exact operation key/amounts, expected envelope version/approval, raw-argument digest and typed family-attempt IDs. `RegistryToolDispatcher` passes the authority unchanged; it must not mint permits. [investigation_service.py:1249](../../backend/src/cti_app/application/investigation_service.py:1249), [investigation_service.py:1361](../../backend/src/cti_app/application/investigation_service.py:1361)
- **INFERENCE —** Main `BridgeInvestigator` and advisor do not use the operator-only authority path: their persisted `ModelConversationTurn`/`ModelRun` and bridge reservation identify each attempt. Advisor binds both operator invocation and its new Bridge attempt. Acquisition binds its prepared dispatch attempt and any operator invocation. [D6 §12](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:596)
- **INFERENCE —** Remove `pre_dispatch_check`, `ToolExecutionContext.pre_transport_check`, `check_current_admission`, and optional Bridge `before_submission`. Keep typed framework proof/result values for confirmed no-send outcomes; they report outcome and do not authorize. Remove `_completed` as an external idempotency authority; PostgreSQL claim/invocation state owns replay. [D6 §11](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:571)

| Slice | Exact ownership proposal (**INFERENCE**) |
|---|---|
| S3-CORE | New `domain/external_effects.py`, `application/external_effects.py`, `infrastructure/database/models/external_effects.py`, `infrastructure/database/repositories/external_effects.py`, `migrations/versions/0004_external_effect_claims.py`; update `infrastructure/database/models/production.py`, `repositories/production.py`, `database/uow.py`, `application/investigation_tools.py`, `application/investigation_service.py`, `application/investigation_composition.py`; add focused unit/PG tests. |
| S3-VT-READ | Own `application/investigation_vt_tools.py`, `infrastructure/virustotal.py`, composition wiring and VT test files. Every public operation and internal route/fallback/page request calls `send_one`; no other family edits. |
| S3-VT-DOWNLOAD | Own `application/investigation_acquisition.py`, `application/sample_acquisition.py`, acquisition attempt persistence in `models/production.py`/`repositories/production.py`, and download portion of `infrastructure/virustotal.py`; preserve quarantine/hash verification/local reuse. |
| S3-BRIDGE | Own `application/bridge_investigator.py`, `application/investigation_advisor_tools.py`, `models/model_execution.py`, its repository and model/bridge composition; each submit, retry and correction has a separate consume. |
| Explicitly unmigrated | **INFERENCE —** Shodan and Retrohunt remain `UNMIGRATED_DENY` until their complete slice lands; their registrations/transport constructors expose a typed unavailable reason and zero send. No “try old adapter” fallback. Do not enable any old direct path after the one-use guarantee is claimed. [D6 §10](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:494) |

**Top 10 source files to open first:** `investigation_tools.py`, `investigation_service.py`, `investigation_composition.py`, `operator_tool_invocation.py`, `models/investigation.py`, `models/model_execution.py`, `models/production.py`, `investigation_vt_tools.py`, `infrastructure/virustotal.py`, `application/bridge_investigator.py`. **VERIFIED —** their current caller/model/transport roles are evidenced above and in D6 §1. [D6 §1](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:14)

## 6. Test matrix (proposed; D6 §9; none run)

Every transport fixture is local/fake and asserts physical fake-send count. No row proves provider receipt. PostgreSQL locking claims require disposable PostgreSQL and independent sessions; in-memory fakes are not a lock oracle. [D6 §7, §9](../../docs/mission03-architecture/_work/D6_S3_C_MIGRATION.md:336)

| Oracle | Fixture / expected status or error | PG 2 sessions? | Does not prove |
|---|---|---:|---|
| Revocation-first then consume-first serial order | Session A pauses before consume; B revokes/commits; A gets `Refused(ENVELOPE_REVOKED)`, zero sends. Reverse order allows exactly one consumed request; next page after revoke is denied. | Yes | Remote receipt or provider revocation behavior. |
| Concurrent workers, same semantic effect, different operation keys | Barrier before consume; unique claim winner only. Loser sees `NEEDS_REVIEW` if in-flight, or explicit allowed `Reused`; fake sends total 1. | Yes | Provider-side idempotency. |
| Same operation replay | Persisted operation key returns same invocation/result; claim/use unchanged; fake sends remain 1 after dispatcher restart. | Yes | Provider receipt. |
| `RUNNING -> NEEDS_REVIEW` race | A commits RUNNING/reservation; B replay seals NEEDS_REVIEW; A consume refuses, zero sends. Consume-first allows only that one request. | Yes | Worker scheduling outside tested transaction. |
| Intentional new occurrence | Same canonical request + distinct UUID yields different effect key/claim and exactly one new permit if policy/budget still allow. | No | That the provider returns fresh data. |
| Crash before consume | Crash after reservation commit, before claim transaction: no claim/permit/send; recovery seals stale invocation NEEDS_REVIEW. | No | Provider availability. |
| Crash after consume before transport | Durable CONSUMED claim, no fake call; recovery NEEDS_REVIEW; replay cannot mint another permit. | No | Whether a real socket was opened in production. |
| Timeout/unknown response | Fake records one accepted request then drops response; claim/invocation NEEDS_REVIEW, reservation retained, replay adds no call. | No | Real provider accepted or processed it. |
| Prepared-byte mutation | Change method/target/header/body after prepare; digest refusal, claim sealed definitive-no-send only if wrapper proves no handoff; fake count 0. | No | Lower-level proxy mutation after wrapper. |
| Direct call without permit | Parameterized VT, download, Bridge, advisor transport call with absent/forged/copy/reused permit; typed refusal, fake count 0. | No | All production compositions if not inventoried. |
| Pagination/fallback/redirect/retry | Script second page/route/redirect/retry; assert each crosses coordinator and gets separate claim; revoke between requests => only first send. Disable hidden client retries. | Yes for revoke race | Provider’s internal retries/redirects outside wrapped client. |
| Budget/evidence preservation | Exhausted or mismatched RESERVED amount denies with zero send; success stores unchanged full authorized refs/SHA/provenance; no truncation. | No | Evidence truth or provider receipt. |
| Reuse lineage | Completed A then B: permitted policy inserts REUSED use with original refs and zero budget delta/no send; no policy returns `FAILED/effect_occurrence_required`, releases only B reservation. | No | Freshness beyond family policy. |
| Acquisition attempt atomicity | Prepared dispatch attempt + claim commit atomically enter SUBMISSION_IN_PROGRESS; ambiguous result links terminal ledger and NEEDS_REVIEW; no resend. | Yes for concurrent attempts | That quarantined bytes came from provider absent local hash verification. |

## 7. Dependency-ordered RED sequence

**INFERENCE —** Commands below are proposed future commands from repository root; tests are not present/run in this design session. Use fakes only; PG cases require configured disposable test DB, never live services. [backend/AGENTS.md](../../backend/AGENTS.md:33)

1. Define JCS subset, types and outcomes. RED: `make test-backend PYTEST_ARGS="tests/test_external_effects.py::test_effect_key_v1_golden_vectors -q --tb=short"`
2. Add additive migration/models and rollback guard. RED: `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_migrations.py INTEGRATION_PYTEST_ARGS="-q --tb=short -k external_effect_claims"`
3. Implement PostgreSQL consume/replay/revocation lock protocol. RED: `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_external_effects_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short -k revocation_first"`
4. Add opaque one-use permit and mandatory raw transport wrapper. RED: `make test-backend PYTEST_ARGS="tests/test_external_effect_transport.py::test_direct_send_requires_one_use_permit -q --tb=short"`
5. Wire operator authority through context/dispatcher; race/replay tests. RED: `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_external_effects_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short -k running_to_needs_review_race"`
6. Cut over S3-VT-READ pages/fallbacks. RED: `make test-backend PYTEST_ARGS="tests/test_investigation_vt_tools.py::test_each_page_consumes_a_separate_permit -q --tb=short"`
7. Cut over S3-VT-DOWNLOAD with prepared attempt and retained quarantine checks. RED: `make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_acquisition_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short -k acquisition_attempt_consumes_permit"`
8. Cut over Bridge/advisor, recovery, reuse lineage; then verify direct entry denial for every family. RED: `make test-backend PYTEST_ARGS="tests/test_bridge_investigator.py::test_retry_and_correction_each_consume_a_permit -q --tb=short"`

**UNKNOWN —** Before implementation, restore the missing seam report and make the actual `0003` revision available; confirm lock order for stop/cancel writers, all composition roots, and every direct HTTP/model request entry. No live provider/database/container operation is authorized or required by this spec.
