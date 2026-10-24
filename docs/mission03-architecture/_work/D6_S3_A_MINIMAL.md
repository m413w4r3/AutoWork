# D6 S3-A — Minimal interface for external-effect decisions

**Study point:** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde` on `mission03/architecture-design`  
**Scope:** interface design only. No production code, tests, migrations, configuration, provider, database, MinIO, Docker, or network activity was performed.

Labels distinguish **VERIFIED** current-source facts, **INFERENCE** about the design needed to satisfy the brief, and **UNKNOWN** runtime/provider behavior. Proposed choices are design recommendations, not implemented behavior.

## 1. Current seam and proposed seam

**VERIFIED.** The operator route currently persists a `RUNNING` `ToolInvocation` and envelope reservation, commits, then dispatches through a mutable registry. The optional admission callback reads the current envelope and returns before adapter entry; it does not check the invocation is still `RUNNING` (`investigation_service.py:1007-1153,1190-1237`; `investigation_tools.py:363-427,619-798`). Main Bridge is a separate route through `BridgeInvestigator.turn` and `ModelConversationTurnTransport`; it has optional pre-submission checks, retry attempts, and corrective submissions (`investigation_service.py:595-672`; `bridge_investigator.py:1172-1445`). Advisor consultation and lower acquisition/transport paths are additional callable seams (`M1A_EXTERNAL_EFFECTS.md`, sections (a) and (d)).

**INFERENCE — proposed seam.** Put one deep application module, `InvestigationExternalEffectGateway`, immediately before every investigation-related provider/model transport. It owns preparation, effect identity, durable claim, final consume, one physical send, result classification, and review linkage. The registry, Bridge loop, advisor and acquisition code become callers/adapters of this module. In particular, `advisor.consult` is routed as a model submission even though its current `ToolDefinition` says `EXTERNAL_READ`. Local computation remains outside it.

The module has two principal operations: `execute_one(intent)` and `reconcile(attempt_id, decision)`. `execute_one` covers one physical request. It is mandatory in the production composition; there is no optional callback or direct “authorized” boolean that substitutes for it. The gateway’s internal route table is immutable after composition. An absent route, stale route version, or direct adapter entry without a one-use dispatch grant fails closed.

## 2. Public interface and pseudo-code

**INFERENCE — proposed types.** `EffectIntent` is an internal application value, never client JSON. It carries a trusted registered route, raw input, and one of two necessary origins:

```python
@dataclass(frozen=True)
class ToolOrigin:
    invocation_id: UUID
    operation_key: str

@dataclass(frozen=True)
class BridgeOrigin:
    turn_id: UUID
    model_run_id: UUID

@dataclass(frozen=True)
class EffectIntent:
    investigation_id: UUID
    origin: ToolOrigin | BridgeOrigin
    route: RegisteredEffectRoute  # supplied by trusted composition, not request JSON
    raw_input: Mapping[str, object]
    occurrence_id: str | None = None  # application-created only for intentional repetition

async def execute_one(intent: EffectIntent) -> EffectOutcome: ...

async def reconcile(
    attempt_id: UUID,
    decision: ReconciliationDecision,  # actor, reason, correlation, evidence refs
) -> EffectOutcome: ...
```

The origin union is the smallest necessary exception to a uniform caller shape: registry effects have a `ToolInvocation`; main Bridge does not, so its persisted `ModelConversationTurn` and linked `ModelRun` are the equivalent durable attempt. Advisor can bind both a `ToolOrigin` and its model attempt internally.

**INFERENCE — internal implementation sketch.** Preparation and consume are hidden, not exposed as caller obligations:

```python
async def execute_one(intent):
    route = immutable_routes.require_exact(intent.route)
    prepared = route.prepare(intent.raw_input)  # canonical values + versioned serializer
    attempt = await persist_prepared_if_absent(
        intent, prepared, effect_identity(prepared, intent.occurrence_id)
    )
    if attempt.is_resolved:
        return await reuse_or_return_prior(attempt, intent)

    grant = await postgres.consume_one(attempt.id, intent.origin)
    # consume_one commits before returning; grant is opaque, bound to this attempt/digest.
    request_bytes = route.render_from_persisted(attempt.prepared_payload_ref)
    assert sha256(request_bytes) == attempt.prepared_payload_sha256
    try:
        response = await route.transport.send(request_bytes, grant=grant)
    except UnknownOutcome:
        return await mark_needs_review(attempt.id)
    return await persist_outcome_and_provenance(attempt.id, response)
```

The code sketch assumes no provider-side idempotency and does not permit an adapter to normalize again after consume. A route may not silently add pagination, retry, or correction requests inside `send`.

## 3. Invariants, timing, versions, errors and confidentiality

**INFERENCE — identity.** Each route owns a pure, versioned preparation function. It produces an immutable normalized request and a serializer version. Persist the credential-free canonical request bytes (or a content-addressed immutable artifact reference plus SHA-256), `normalizer_version`, `route_version`, `serializer_version`, and payload digest before consume. The effect identity is separate from the caller’s `operation_key`:

```text
effect_identity = SHA256(
  "autowork-effect-v1" || family || route_version || schema_version ||
  normalizer_version || serializer_version || provider/endpoint ||
  canonical_nonsecret_request_bytes
)
claim_key = (investigation_id, effect_identity, occurrence_id)
```

It includes all semantically material parameters, page/cursor, method, endpoint and body. It excludes `operation_key`, API credentials, tokens, passwords, signed URLs and other transport secrets. The adapter sends the persisted canonical request bytes, not a re-normalized object; it checks the bytes’ digest immediately before handoff. Authentication is injected separately by the transport adapter. A provider SDK with hidden retries is not acceptable until retries are disabled or surfaced as separate attempts.

The default occurrence is a non-null stable sentinel for an investigation/effect identity, so a second client key cannot silently create a duplicate. A route that intentionally repeats an identical read/poll/refresh must receive an application-generated, persisted `occurrence_id` identifying that business occurrence. Bridge attempt UUIDs and changed page cursors are distinct occurrences. Client input cannot mint arbitrary occurrence IDs. Keeping occurrence outside `effect_identity` makes an identical request recognizable while still permitting a separately authorized occurrence.

**INFERENCE — admission.** Preparation stores the approval decision ID and policy fingerprint that justified the request. Consume loads and locks the current envelope, verifies that same approval decision remains effective, and checks exact provider scope, `external_llm_allowed`, TLP, `do_not_submit`, sample restrictions, acquisition permission, and current stop/cancel state. It also verifies the reservation’s operation key, `RESERVED` status, and exact amounts. It does not widen the prepared authorization if policy has changed; a mismatch requires a fresh invocation.

The short consume transaction commits before transport. No SQL lock crosses network I/O. A revocation committed before consume wins and produces zero egress. If consume commits first, that one request may proceed even if revocation commits before the HTTP write; the grant authorizes only that physical request. It does not authorize a later retry, page, or correction.

**INFERENCE — errors.** Missing, stale, reused, misbound, expired, or unavailable decisions return a typed framework denial and zero transport calls. A refusal before consume may release the claim/reservation only with framework-attested proof. A failure after consume is ambiguous unless the gateway itself can prove no transport was entered or the transport adapter returns a narrowly defined typed `NOT_SENT` proof before any request bytes were handed off. Timeout, cancellation, process loss, or unknown response after transport entry becomes `NEEDS_REVIEW`; no replay may submit again. Provider/model text remains untrusted data.

**CONFIDENTIALITY.** No AutoWork credential or signed URL enters the identity, prepared artifact, trace, or log. Transport secrets are injected only inside the route adapter. Evidence references, source/version links, hashes, limitations, permitted raw bytes and every policy-based omission remain observable through canonical evidence records; do not truncate or suppress authorized evidence to fit the interface.

## 4. Example from real consumers

**INFERENCE — operator tool.** `InvestigationService.execute_tool_call` still creates the durable invocation and reservation. When a registered VT/Shodan/Retrohunt/acquisition adapter reaches its external action, it submits the invocation reference, trusted route and raw arguments to the gateway. The route normalizes once, persists the prepared request, consumes the permit and sends it. The dispatcher no longer calls provider-facing `adapter.execute` directly for external effects.

```python
outcome = await effect_gateway.execute_one(EffectIntent(
    investigation_id=ctx.investigation_id,
    origin=ToolOrigin(invocation_id=ctx.invocation_id,
                      operation_key=ctx.idempotency_key),
    route=registered_route,
    raw_input=arguments,
))
```

**INFERENCE — Bridge.** `ModelConversationTurnTransport.submit_with_attempt` calls the same gateway with the exact persisted turn and linked run. `BridgeInvestigator.turn` may prepare another physical attempt only after a known terminal response or proven pre-submission failure; that attempt has a new persisted turn/run and a new consume. A changed corrective prompt is always a new request and digest. The existing main Bridge and advisor routes cannot rely on a Boolean or optional `before_submission` callback as authorization.

## 5. Responsibilities hidden in the implementation

The gateway and its route adapters own: effect-family classification; immutable route lookup; normalization and versioning; effect identity and occurrence rules; prepared payload persistence; authorization fingerprinting; atomic Postgres consume; one-shot grant creation; byte/digest equality; direct-entry denial; response/ambiguity classification; replay behavior; budget disposition; observation/invocation lineage; and durable review/reconciliation linkage.

The adapter retains domain-specific checks that cannot be generalized away: exact prevalidated Retrohunt version and human decision, acquisition hash/quarantine verification, provider capability/schema checks, advisor evidence preparation, and Bridge parsing. These checks supplement the gateway and cannot grant egress by themselves.

## 6. Responsibilities removed from callers

Callers no longer re-read the envelope immediately before dispatch, infer whether an invocation is still `RUNNING`, coordinate reservation status with revocation, construct an ad hoc effect fingerprint, decide whether a retry is covered by a prior approval, or rely on an optional callback. They do not pass an authorization Boolean to a transport or invoke a provider port directly.

The interface does not remove domain-specific validation, evidence archiving, response interpretation, observation creation, or human review. It centralizes the outbound decision and leaves those responsibilities local to their domain adapters.

## 7. Dependencies and adapters

**VERIFIED.** PostgreSQL is the only accepted oracle for row locks, unique constraints, and races across sessions. Existing provider/model transports are true external dependencies with fake/scripted adapters; normalization is in-process (`D6_BRIEF_S3.md`, sections 3 and 4; `M2A_TEST_SURFACE_EXTERNAL.md`).

**INFERENCE — proposed adapters.** The gateway uses the existing SQLAlchemy unit of work and new PostgreSQL repositories in production, with a disposable-PostgreSQL adapter for integration tests. Route adapters wrap the existing VT, Shodan, Retrohunt, acquisition and model/Bridge transports; tests inject their existing fake/scripted implementations. Normalizers remain pure in-process implementation seams. Do not add a hypothetical transport port where no second real or test adapter exists. Build the route table from trusted registrations and freeze it after composition; old/missing route versions fail closed rather than dispatching through a changed registration.

**UNKNOWN.** Whether any underlying HTTP/model SDK retries transparently, and every production-reachable direct transport call outside the scoped paths, require an implementation-time audit. No provider behavior is assumed.

## 8. Persistent source of truth and consume transaction

**VERIFIED.** `investigation_envelopes.budget_snapshot` stores the budget and reservations in JSON; `InvestigationEnvelopeRow` is the same row locked by revocation (`repositories/investigation.py:1426-1467`; `models/investigation.py:547-582`). Existing `ToolInvocation` has a versioned status and CAS. Existing Bridge turns and model runs persist before gateway execution but are not coordinated with envelope consume (`M3_S3_DIAGNOSIS.md`, questions 6–8).

**INFERENCE — proposed persistent record.** Add one `external_effect_attempts` row per physical request. It is both the identity claim and the one-use decision record, avoiding a second claim/decision state machine. It stores `attempt_id`, investigation, optional tool invocation, optional Bridge turn/run, route/family/schema/normalizer/serializer versions, effect identity, occurrence ID, prepared-payload ref/digest, policy/approval fingerprint, consumed envelope version, timestamps, response/evidence refs and state. The unique identity/occurrence row prevents distinct client keys from claiming the same default effect twice.

**Exact consume state set and lock order — INFERENCE.** `consume_one` runs one short transaction in this fixed order:

1. `investigation_envelopes` row `FOR UPDATE`: require the expected investigation, current active approval decision and policy fingerprint; read the exact reservation from the locked `budget_snapshot` and require matching operation key, `RESERVED`, and exact amounts. Reservation is JSON inside this row, so there is no separate reservation-row lock in the current operator path.
2. If a tool origin exists, `tool_invocations` row `FOR UPDATE`: require same investigation, tool/schema/prepared-input binding, and status exactly `RUNNING`.
3. `external_effect_attempts` row `FOR UPDATE`: require exact route/identity/payload digest binding and state exactly `PREPARED`; reject any existing consumed or terminal claim.
4. For Bridge origins, `model_conversation_turns` row `FOR UPDATE`, then linked `model_runs` row `FOR UPDATE`: require the exact turn ID/idempotency key/input SHA and turn status `RUNNING`; require run status `RUNNING` and `submission_state=NOT_SUBMITTED`.
5. In that transaction, transition the effect attempt `PREPARED → CONSUMED`, stamp the locked current envelope version and consume ID, and for Bridge transition the linked run’s submission state `NOT_SUBMITTED → SUBMISSION_IN_PROGRESS`. Leave a tool invocation `RUNNING` and reservation `RESERVED`; they settle only when the outcome is known.

Commit, release every lock, then create the in-memory one-use grant and enter exactly one transport request. For a main Bridge request the persisted turn/run pair is the durable invocation equivalent; for advisor the gateway checks both its `ToolInvocation` and model attempt. The Bridge `ModelGateway` must accept this consumed `SUBMISSION_IN_PROGRESS` state as the grant for this exact run and must never independently retry or submit it again. All writers that touch these records in one transaction must use this lock order. A conflicting status/version, absent row, malformed prepared payload, stale route, or DB failure aborts consume and makes no egress.

**Proposed attempt states:** `PREPARED → CONSUMED → SUCCEEDED | NEEDS_REVIEW`; `PREPARED → NOT_SUBMITTED` when consume never committed; `CONSUMED → NOT_SUBMITTED` only with the typed framework proof above; `NEEDS_REVIEW → RECONCILED_SUCCEEDED | RECONCILED_NOT_SUBMITTED | ABANDONED` through explicit human reconciliation. Terminal rows are never re-consumed. `ABANDONED` keeps the reservation unresolved and the effect identity blocked; it does not assert that the provider did nothing.

## 9. Positive, negative, crash, replay and PostgreSQL oracles

**INFERENCE — interface-level fake transport oracles.** Through `execute_one`, assert: an authorized registered effect consumes once and sends the exact persisted bytes; the payload digest matches; observation refs retain source and invocation links; a local-compute route does not consume; and family-specific successful result reuse creates a lineage link without another provider call or a second budget charge.

Negative cases: no envelope, changed/revoked approval, provider outside scope, `external_llm_allowed=false`, TLP RED, `do_not_submit`, denied sample, missing acquisition permission, missing/changed reservation, invocation not `RUNNING`, Bridge run/turn not eligible, changed normalizer/route version, altered payload digest, duplicate/copy/misbound grant, second grant use, and absent direct-entry grant. Each asserts zero calls. Parameterize the direct raw entries listed in M2-A #10 (VT, Shodan, acquisition, Retrohunt, advisor and main Bridge).

Crash/replay cases: crash after durable reservation/preparation but before consume yields zero calls and a non-consumable stale invocation until explicit safe reconciliation; crash after consume but before send, after request handoff, after provider acceptance with lost response, cancellation, timeout, and late response all reuse the same attempt and never auto-submit again. A late response may resolve only that exact attempt by CAS. Replay of `RUNNING` changed to `NEEDS_REVIEW` by another worker yields zero calls. A known, fully recorded response followed by a domain-authorized retry creates a new occurrence and a new consume.

**Required two-session PostgreSQL races.** Use independent sessions, a disposable database, deterministic barriers and fake egress:

1. Worker A commits reservation, `RUNNING` invocation, prepared version/digest and Bridge attempt, then pauses before consume. Worker B replays and commits `NEEDS_REVIEW`. Resume A: consume locks/reloads the invocation, refuses, and fake send count is zero.
2. Race revocation and consume on the envelope row. Revocation-first must deny and send zero. Consume-first commits one winner; its single request may proceed after revocation commits. Block the fake transport and prove a second session can commit revocation while it is blocked, demonstrating no SQL lock is held across transport.
3. Concurrent distinct client keys with identical effect identity and default occurrence must produce one claim/send. A distinct application occurrence may produce a second claim only when the route explicitly permits it.

These oracles prove the database ordering and application call count, not provider receipt or provider-side idempotency. Existing PostgreSQL tests cover adjacent persistence and provider fakes but do not prove this consume seam (`M2A_TEST_SURFACE_EXTERNAL.md`, “Missing oracle” list).

## 10. Compatibility, additive migration and rollback

**INFERENCE — proposed migration.** Add a new forward revision after current head (for example `0003_external_effect_attempts`). Do not alter frozen `0001_baseline` or rewrite `0002`. Create the attempt table with restrictive FKs to investigation/invocation/turn/run, a status `CHECK`, SHA-256/version checks, `UNIQUE(investigation_id, effect_identity, occurrence_id)`, `UNIQUE(attempt_id)`, and unique nullable links for one physical attempt per Bridge turn/run. Add a separate reuse-link table if a prior observation is reused across invocations; it records source attempt, source invocation, destination invocation and observation IDs. Use indexes for investigation/state recovery and unresolved attempts. Preserve current ToolInvocation/ModelRun statuses where possible; any added status value requires expanding its existing database `CHECK` in this additive revision.

Migration preflight must recognize supported schema states and fail before DDL on partial or drifted forms, following ADR-0007. Existing `RUNNING` rows without a prepared/consumed effect record are never inferred to be authorized; recovery seals them for review or proves safe pre-consume state. Roll back application code while retaining the additive schema. A downgrade may drop the new tables only on an isolated empty database after proving there are no attempt, reuse, or evidence links; it must refuse if history exists. Never delete canonical effect history to make rollback pass.

## 11. Deletion test

Delete `InvestigationExternalEffectGateway` from the proposed design. The operator service, dispatcher, each VT/Shodan/Retrohunt/acquisition/advisor adapter, Bridge transport and raw provider wrappers would again need to implement preparation, identity, revocation serialization, reservation checks, invocation/Bridge state locks, one-use behavior, direct-entry denial, timeout ambiguity, reuse lineage and zero-call proofs. The same invariants would be copied across at least six routes, while cross-process behavior would remain hard to test consistently. That complexity reappearing in callers means this module earns its seam and has depth; its leverage is one consume contract for every external investigation effect and locality for policy fixes.

## 12. Before/after comparison

| Concern | Before — VERIFIED | After — INFERENCE |
|---|---|---|
| Dispatch seam | Optional registry callback then adapter; Bridge separate | One required `execute_one` seam for every investigation egress |
| Durable decision | Reservation/invocation commit, then read/check callback | One short Postgres consume transaction serializes revocation and request eligibility |
| Invocation replay race | Callback does not reread `RUNNING` state | Consume locks and requires `RUNNING`; `NEEDS_REVIEW` wins before consume |
| Identity | Raw arguments in `normalized_parameters`; separate operator key; no shared canonical identity | Versioned normalized payload digest plus occurrence claim, distinct from `operation_key` |
| Bridge scope | One logical turn can contain multiple sends under one reservation | One request per consume; retries, pages and changed corrections need new attempts |
| Direct entry | Adapters/transports are callable below service; callbacks can be absent | No grant means typed denial and zero send; production composition exposes no raw route |
| Recovery | Separate service-specific ambiguity state machines | Common attempt state plus existing domain reconciliation/evidence logic |

Evidence for the “Before” column: `M1A_EXTERNAL_EFFECTS.md` sections (a), (c)–(e); `M3_S3_DIAGNOSIS.md` questions 5–10; and current source ranges listed in sections 1 and 8 above. The “After” column is a proposal, not verified implementation behavior.

## 13. Risks, UNKNOWNs and the seven BLOCKED_DESIGN decisions

**Decision 1 — prepared-effect interface.** **INFERENCE:** each route has a versioned pure normalizer and serializer; persist the canonical non-secret bytes/ref and SHA before consume; route sends those same bytes without re-normalizing. Credential material is injected separately. **UNKNOWN:** any route whose endpoint itself is an ephemeral signed URL needs a transport-private resolver and exact implementation review; the URL must not enter identity, artifacts, trace or logs.

**Decision 2 — cross-adapter identity/claim.** **INFERENCE:** use semantic `effect_identity` plus application-controlled `occurrence_id` in a separate claim key, independent of client `operation_key`. Default occurrence blocks duplicate semantic requests across client keys. An explicit occurrence authorizes intentional poll/refresh only for a route that declares it. Reuse is route-specific, terminal-only, links source invocation/observation and charges measured usage once; unresolved claims never reuse or resend.

**Decision 3 — mandatory seam and direct entry.** **INFERENCE:** `InvestigationExternalEffectGateway.execute_one` is the only production path to investigation transports, including unregistered main Bridge, advisor and lower acquisition. Raw adapters require an opaque one-use grant bound to attempt and payload digest; no grant, missing route or stale route fails before transport. Composition must not expose raw send methods as caller dependencies.

**Decision 4 — consume record, states, keys and locks.** **INFERENCE:** one `external_effect_attempts` row is both claim and decision. The consume transaction checks the exact state set and follows the lock order in §8. It atomically binds the locked current envelope/version and JSON reservation, optional `RUNNING` ToolInvocation, prepared identity/version, decision state, and Bridge turn/run. Reservation remains `RESERVED` and invocation remains `RUNNING` until outcome persistence.

**Decision 5 — physical request versus session; post-consume revocation.** **INFERENCE:** consume authorizes one physical request only. Every HTTP retry, provider page/cursor, Bridge retry, advisor submission or corrective prompt has its own prepared request and consume; changed Bridge context is never covered implicitly. The existing bounded retry/correction rules remain the domain policy and no new arbitrary cap is added. Revocation before consume prevents that request; after consume, only that consumed physical request may proceed. Later sends must consume again and will observe the revocation.

**Decision 6 — crash, ambiguity, reconciliation and reuse.** **INFERENCE:** no consume means no egress and can be released only from durable `PREPARED` state; after consume, unknown outcome, timeout, cancellation or crash becomes `NEEDS_REVIEW` and cannot auto-resubmit. Only gateway/transport-attested exact no-send proof or explicit human reconciliation backed by attempt-specific evidence can resolve as not submitted. Otherwise keep reservation and claim blocked. A reconciled retry uses a new application-generated occurrence; it never reopens the old consumed row. A resolved reuse links the new invocation to the original attempt and observation and charges measured budget once. A late response resolves the same attempt by CAS. Provider idempotency is not assumed.

**Decision 7 — migration/status constraints.** **INFERENCE:** add an additive revision after current head for attempt/reuse storage and constraints. Reuse existing invocation and model-run states unless a new state is necessary; if one is added, extend its `CHECK` constraint in the same revision. Preserve `0001` and `0002`; rollback retains history and refuses destructive downgrade when rows exist.

**Hardest unresolved risk — UNKNOWN.** The full set of production-reachable raw transport entry points, hidden SDK retries, and Bridge/model transaction ordering must be audited before implementation. Also unresolved is the safe representation of secret-bearing ephemeral download targets without persisting them. These are implementation gates, not grounds to assume the current code is safe.

**Limits.** This is a design proposal, not proof of PostgreSQL behavior or provider receipt. No provider-side canonicalization/idempotency, transport retries, Bridge pagination, or runtime lock ordering is assumed; each remains an explicit audit/test question.
