# D6-S3-C — Common caller and migration

**Design point:** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde` on
`mission03/architecture-design`  
**Scope:** interface and migration design only. No production code, tests,
migrations, or configuration were changed.  
**Status:** proposal; the seven `BLOCKED_DESIGN` decisions are answered in §3.

Labels mean **VERIFIED** at the stated source/line, **INFERENCE** from those
paths, and **UNKNOWN** where source review cannot establish runtime behavior.

## 1. Current seam and proposed seam

**VERIFIED — operator route.** The common real caller is
`OperatorToolInvocationService.invoke` → `InvestigationService.execute_tool_call`
→ `RegistryToolDispatcher.dispatch` → registered adapter → provider port. The
operator service canonicalizes the API schema alias and client idempotency
payload; `execute_tool_call` persists `RUNNING` plus the reservation and commits
before dispatch; the dispatcher checks registration, schema, capability,
policy and budget before entering the adapter. References:
`operator_tool_invocation.py:700-761`, `investigation_service.py:1007-1153`,
`investigation_tools.py:619-784`.

**VERIFIED — seam gap.** The dispatcher’s `pre_dispatch_check` is optional
(`investigation_tools.py:412-427,779-784`). The callback in
`InvestigationService` reads the envelope and reservation, returns, and only
then the adapter begins (`investigation_service.py:1190-1237`). It does not
lock/check the persisted invocation status in a transaction with the decision.
The Bridge path has a separate optional `before_submission` callback and can
make multiple physical submissions in one logical turn
(`bridge_investigator.py:1172-1297,1321-1340`). Therefore the registry dispatcher
is too high to be the sole seam.

**PROPOSED seam — `ExternalEffectCoordinator.send_one`.** Put one mandatory
application module immediately before each physical outbound request. A
provider adapter prepares the exact request; the coordinator commits the
durable consume decision; then it passes an opaque, bound, one-use permit and
the same immutable request to a permit-aware transport adapter. An adapter or
transport called without a permit refuses before I/O. No caller-supplied
boolean, effect enum, or callback grants permission.

`RegistryToolDispatcher` remains the main orchestration seam for registry
tools. For the ordinary operator request it prepares a server-owned
`ExternalEffectAuthority` from the durable invocation and reservation and
passes it in `ToolExecutionContext`; local tools have no authority and skip the
external module. External adapters call the same `send_one` for every actual
request. `BridgeInvestigator`, advisor, Retrohunt and acquisition use the same
coordinator with their own persisted attempt identifiers. This gives the
operator caller a short default path while placing the decision low enough to
cover every egress route.

The new module has depth only if each path’s current provider adapter and a
scripted/fake transport can satisfy its interface. PostgreSQL is its durable
decision adapter; in-process identity preparation is pure. The shared seam
provides leverage across the callers and locality for admission, identity,
recovery and no-permit denial.

## 2. Public interface and pseudocode

The names below describe the proposed interface, not existing symbols.

```python
@dataclass(frozen=True)
class PreparedExternalRequest:
    family: str                    # e.g. vt.file_report, bridge.turn
    provider: str                  # registered/configured provider identity
    normalizer_version: str        # immutable, operation-specific version
    identity_version: str          # common effect-key format version
    canonical_request: Mapping     # safe normalized semantics, no credentials
    request_ref: str | None        # immutable versioned artifact/reference if large
    canonical_request_sha256: str
    application_request_sha256: str # exact serialized method/target/body/headers
    occurrence_id: UUID | None    # explicit refresh/poll/attempt, not operation_key
    evidence_refs: tuple[EvidenceRef, ...]

@dataclass(frozen=True)
class ExternalEffectAuthority:
    investigation_id: UUID
    invocation_id: UUID | None
    reservation_key: str
    expected_envelope_version: int
    approval_decision_id: UUID
    bridge_turn_id: UUID | None
    model_run_id: UUID | None
    family_attempt_id: UUID | None

async def send_one(
    authority: ExternalEffectAuthority,
    prepared: PreparedExternalRequest,
    transport: PermitAwareTransport,
) -> ExternalEffectOutcome: ...

class PermitAwareTransport(Protocol):
    async def send(
        self,
        prepared: PreparedExternalRequest,
        *,
        permit: ConsumedEffectPermit,
    ) -> TransportOutcome: ...

# ExternalEffectOutcome is a closed result type:
# Completed(claim_id, response/evidence refs)
# Reused(claim_id, source invocation, observation refs, budget_delta=0)
# Refused(reason, typed framework proof that this request was not sent)
# Ambiguous(claim_id, NEEDS_REVIEW; reservation retained)
```

The coordinator obtains `family`, provider and normalizer version from
server-owned registration/composition, not from JSON arguments. Preparation is
operation-specific and returns the exact normalized request that will be
serialized. It is not a caller-supplied authorization claim.

The effect identity is distinct from `ToolInvocation.operation_key` and from
the client `Idempotency-Key`:

```text
request_sha = SHA256(canonical_json(safe normalized request))
effect_key = SHA256(canonical_json({
    identity_version, investigation_id, provider_scope, family,
    normalizer_version, request_sha, occurrence_id_or_default
}))
```

`provider_scope` is a non-secret configured route/account identifier. It must
not contain provider credentials, access tokens or signed URLs. The row also
stores `application_request_sha256`, computed over the exact application
request bytes after serialization and before handing them to the HTTP/model
client. The adapter passes that immutable byte representation through; the
transport recomputes and compares the digest immediately before I/O. The
digest is stored, never the credential-bearing request bytes. Large authorized
evidence remains complete: use the existing immutable versioned artifact and
SHA-256/evidence references, not truncation or a new prompt-size ceiling.

`send_one` performs, in order:

1. Validate the prepared request and authority binding, including the raw
   invocation arguments hash, operation/schema, normalizer version, evidence
   references, expected reservation and request digest.
2. Run the short PostgreSQL consume transaction described in §3. If denied,
   return a typed pre-send result without constructing a permit or calling the
   transport.
3. Commit. Only after commit mint the in-process opaque permit bound to the
   claim, invocation/attempt, effect key, envelope version, request digest and
   provider. No SQL lock remains held.
4. Call the injected permit-aware transport once with the same prepared bytes.
5. Persist the known outcome, or seal the claim and existing invocation/attempt
   as `NEEDS_REVIEW` when outcome is ambiguous.

Each permit covers **one physical request**. A page, cursor fetch, retry,
corrective Bridge request, Retrohunt poll or other second physical request
must be prepared and consumed separately. A stable logical budget reservation
may still cover the existing bounded Bridge turn as it does today; permit
count is not a new budget charge. A changed Bridge prompt/context always has a
new effect key. Any SDK retry that can emit a second request must either be
disabled or call `send_one` again; hidden retries are outside this interface
and are not authorized by one permit.

## 3. Invariants, timing, versions, errors and confidentiality

### Seven `BLOCKED_DESIGN` decisions from M3

1. **Prepared effect.** Each provider operation owns a pure, versioned
   normalizer and serializer. It returns normalized semantic values, a
   canonical request digest, the immutable request/artifact reference and the
   digest of the exact application request bytes. The egress adapter sends
   those same bytes. VT, Shodan, Retrohunt, acquisition, advisor and Bridge
   each have operation-specific versions; an absent or unknown version fails
   closed. Changing normalized meaning or serialization requires a new
   version and an explicit compatibility decision. Source evidence:
   `investigation_vt_tools.py:686-697`,
   `investigation_shodan_tools.py:665-683,767-792,929-945`,
   `retrohunt.py:129-150`, `investigation_acquisition.py:114-141`,
   `investigation_advisor_tools.py:174-199,889-907`.
2. **Durable identity and occurrence.** The PostgreSQL `effect_key` is common
   across adapters and is not the client operation key. A missing occurrence
   means the normalized effect is the same application occurrence. A distinct
   caller intent to refresh an identical read or poll supplies a stable
   application `occurrence_id` (for example, a poll tick or Bridge attempt).
   The operator route may add an optional `effect_occurrence_id` for an
   intentional refresh; it does not replace or alter the required
   `Idempotency-Key`. Existing operation-key replay remains unchanged.
3. **Mandatory outbound seam.** Every external physical request goes through
   `send_one`. Every raw transport method requires `ConsumedEffectPermit` and
   checks issuer, claim, operation, provider and request digest immediately
   before sending. Direct adapter/transport entry without a permit is a typed
   refusal and makes zero provider calls. Local compute/read paths do not
   obtain a permit. This includes main Bridge and acquisition, which do not
   use the operator registry.
4. **Consume row set and lock order.** The transaction locks/reloads, in one
   universal order: (1) envelope row `FOR UPDATE`; (2) `ToolInvocation` row
   `FOR UPDATE` when this is a registered tool; (3) the exact reservation and
   amount (currently part of the locked envelope budget; if later split into
   a row, lock it here); (4) insert/lock the unique effect claim; (5) any
   family-specific durable attempt (Retrohunt slot or acquisition attempt);
   (6) `ModelConversationTurn`, then its linked `ModelRun`, when used. For a
   main Bridge call with no `ToolInvocation`, the invocation slot is omitted;
   its persisted turn/run and Bridge reservation are mandatory. Advisor calls
   bind both the operator invocation and Bridge attempt. Lock only rows that
   apply, always in this order. A consume requires exact investigation,
   envelope version and approval identity; effective G2 provider scope,
   TLP, `external_llm_allowed`, `do_not_submit`, sample restrictions, expiry,
   stop/cancel state, and remaining budget; exact `RESERVED` operation key and
   amounts; invocation still `RUNNING`; and exact request/normalizer digests.
   The Bridge turn/run must be the expected persisted attempt and in a state
   that has not already been consumed. It atomically records claim state
   `CONSUMED` and the linked Bridge submission-in-progress state. A unique
   constraint makes concurrent equivalent claims serialize. The consume
   commit is the authorization cut, not the earlier reservation commit.
5. **Scope and revocation.** One consume authorizes one physical request only.
   If revocation commits first, consume observes it and denies with zero
   egress. If consume commits first, that one request may proceed even if a
   revocation commits before the socket call; the commit is the explicit
   authorization cut. Every later page/retry/correction/poll reconsumes and
   sees the revocation. No permit authorizes an open-ended adapter session.
6. **Crash, ambiguity, late result and reuse.** A crash before consume leaves
   no claim and cannot yield a permit. The existing recovery path may seal the
   stale `RUNNING` invocation as `NEEDS_REVIEW`; it cannot dispatch it. A crash
   after consume, whether before transport entry or after remote acceptance,
   is conservatively ambiguous: mark the claim and existing invocation/attempt
   `NEEDS_REVIEW`, retain the reservation, and never automatically resend.
   Timeout, cancellation after handoff, lost response and unknown transport
   error have the same disposition. Only framework-owned proof that no bytes
   were handed to transport may mark a consumed claim
   `DEFINITIVELY_NOT_SUBMITTED`; an adapter/model-provided assertion is not
   proof. An exact late response is attached to the existing claim as a
   reconciliation candidate; it does not trigger a second request or silently
   change an already reviewed invocation. Explicit reconciliation remains
   CAS-guarded. A successful effect can be reused only under a per-family
   `reuse_policy`; the new invocation links to the original effect and
   observation references, the source observation keeps its original
   invocation ID, the new use is auditable, and its budget delta is zero. No
   reuse policy means an explicit occurrence is required for another send.
7. **Statuses and additive schema.** Reuse existing `ToolInvocation` statuses,
   including `RUNNING` and `NEEDS_REVIEW`; do not add or reinterpret a
   persisted invocation status. Add a claim table/state check and a use/lineage
   table by a migration after the actual current Alembic head. No edit to
   `0001_baseline`, `0002`, existing operation keys, or existing invocation
   rows. Details and rollback are in §10.

### Error and privacy contract

Absent, expired, replayed, copied, stale or misbound permits fail closed before
transport. A same-digest concurrent/in-flight claim cannot yield another
permit. A confirmed pre-send denial maps through current `ToolResult` and API
status/error-code behavior; the coordinator must not invent a more permissive
status. A consumed unknown result maps to the existing `NEEDS_REVIEW` flow and
keeps its reservation. `EffectClaim` state is new internal state, not a new
public status. For an already successful same-effect claim, use its result
only when that operation's `reuse_policy` permits it. Otherwise return the
existing `FAILED` invocation status with the explicit new error code
`effect_occurrence_required`, a framework proof of no new send, and release
only this invocation's still-`RESERVED` budget. A consumed/ambiguous matching
claim returns existing `NEEDS_REVIEW` and retains budget. This error and the
optional `effect_occurrence_id` are the deliberate API contract additions;
existing response shape, status enum, same-key replay and other error mappings
remain unchanged.

Keep the current G2/G3, provider-scope, TLP, disclosure, revocation and human
approval checks. Never put AutoWork credentials, passwords, signed URLs or
secret-bearing headers in effect identity, canonical request material, logs
or prompts. Compute the application request digest in memory before the send;
persist only its hash and safe prepared representation/reference. Preserve
all authorized CTI evidence, SHA-256, provenance, source versions,
limitations and observable omission reasons. Treat tool/provider/model output
as untrusted data. Do not add a size cap, field-name allowlist or silent
truncation.

**UNKNOWN:** whether HTTP/model SDKs transparently retry or mutate request
bytes after the injected transport call. The implementation must disable
hidden retries or instrument every physical attempt through `send_one`; a
fake transport cannot establish provider receipt.

## 4. Example from the frequent operator caller

The API handler and `OperatorToolInvocationService.invoke` retain their
current request validation, operation-key construction, operator-mode and
disclosure checks. `execute_tool_call` continues to persist the same
`ToolInvocation`, budget reservation and `RUNNING` status before dispatch.
The default external case becomes:

```python
invocation, result, refs, external = await execution.execute_tool_call(
    investigation=investigation,
    envelope=envelope,
    budget=budget,
    state=state,
    tool_name="vt.file_report",
    schema_version=registered_schema,
    arguments=arguments,
    purpose=purpose,
    operation_key=existing_operator_operation_key,
)
```

Inside the dispatcher, the registered VT adapter validates the schema and
prepares its normalized hash/report request. Its transport request calls
`send_one` with the server-built authority for this invocation and the exact
serialized request. Consume either returns a bound permit and one transport
call proceeds, or returns a refusal and the existing result/status finalizer
handles it. There is no `check_current_admission` closure to understand or
write at the call site. Equivalent operator requests with a different client
operation key compete on `effect_key`; safe successful reuse links the new
invocation to the source claim and incurs no second transport/budget charge.
An intentional identical refresh uses a distinct application
`effect_occurrence_id`.

## 5. Responsibilities hidden in the implementation

`ExternalEffectCoordinator` hides the versioned identity calculation, safe
request-reference handling, exact digest comparison, claim uniqueness,
transaction and lock ordering, current envelope/policy recheck, reservation
matching, invocation/Bridge state check, one-use permit issuance, transport
handoff, outcome journaling, and recovery disposition. Provider adapters hide
their operation-specific normalization and exact request serialization. The
PostgreSQL adapter hides `SELECT … FOR UPDATE`, CAS, unique-conflict handling,
and commit/rollback. A family adapter retains its domain workflow (for example,
Retrohunt's reviewed-rule/corpus checks or acquisition's quarantined-byte
verification); those do not become permission to bypass the common seam.

The implementation stores a durable claim only when consume commits; there
is no committed `PREPARED` state that recovery might mistake for authorization.
The claim row includes effect key, identity/normalizer versions, safe request
reference and digests, occurrence, source invocation/attempt, reservation key,
envelope/approval versions, timestamps and claim state. A separate effect-use
row links reuse to each consuming invocation and stores the source claim,
observation references and budget delta. Unique keys are scoped to the
investigation and explicit occurrence; they do not imply provider-side
idempotency.

## 6. Responsibilities removed from callers

Callers no longer read the latest envelope and then hope policy remains valid
until the adapter runs. They no longer supply an optional “still allowed”
boolean/callback, manually bind budget and invocation state to a send, or
decide how an ambiguous timeout may be retried. Registry adapters do not trust
request JSON to choose effect category. Bridge does not treat one logical turn
as authorization for all retries/corrections. Provider transports do not
accept an unguarded direct call. Callers still supply their persisted
invocation/attempt/reservation identifiers and operation-specific prepared
request; those are business facts, not distributed authorization logic.

## 7. Dependencies and adapters

| Dependency | Classification | Adapter / test surface |
|---|---|---|
| PostgreSQL envelope, invocation, reservation, claim, Bridge and family-attempt rows | Locally substitutable for ordinary logic, but PostgreSQL is the only valid oracle for row-lock order, uniqueness and independent-session races | Production PostgreSQL repository plus disposable-PostgreSQL integration tests using two independent sessions; in-memory fakes are not proof |
| Identity preparation and canonical hashing | In-process | Direct deterministic tests with versioned golden request and byte-digest cases |
| VT, Shodan, Retrohunt, VT download and Bridge/model providers | True external systems | Existing production transport adapters and injected fake/scripted transports; no live-provider tests and no idempotency assumptions |
| Artifact/observation storage | Canonical application stores | Existing versioned-artifact and SHA-256 repositories; keep full authorized evidence references and provenance |
| Local tools (rule/sample analysis, reads, extraction) | In-process/local | Remain outside `send_one`; local work has no external permit |

The provider transport port is justified by production and fake/scripted
adapters. The effect identity calculator does not need a port. Do not add an
in-memory PostgreSQL adapter and claim it proves `FOR UPDATE` ordering.

## 8. Persistent source of truth and decision point

PostgreSQL claim state is authoritative for whether AutoWork may initiate a
physical egress. Versioned artifacts, observations and MinIO SHA-256
references remain canonical for evidence and results. The dispatcher cache,
Python permit object and transport result are not durable authorities.

**Decision point:** the successful PostgreSQL consume commit after policy and
exact-row checks. Before that commit there is no permit and zero egress. After
it, the one bound physical request is authorized; no database lock is held
through HTTP/model transport. Envelope revocation uses the same envelope row
lock. Thus revocation-first denies; consume-first permits precisely that one
request. All later requests in a loop need another commit.

Current operator source commits `RUNNING` plus reservation before dispatch
(`investigation_service.py:1110-1153`) and its callback reads but does not
serialize that decision (`1190-1237`). The proposed transaction closes both
the revocation gap and the `RUNNING` → `NEEDS_REVIEW` race by checking the
current invocation row under lock at consume.

## 9. Proposed oracles

These are implementation gates, not tests run for this design. Use disposable
PostgreSQL and fake provider/Bridge transports. Preserve existing useful tests,
but replace shallow callback assertions with behavior through the new module.

1. **Denied consume:** absent/expired/revoked envelope, disallowed provider,
   `external_llm_allowed=false`, TLP RED, `do_not_submit`, sample restriction
   and exhausted budget each yield no permit, no claim consumption and zero
   fake calls. Assert the existing public invocation status/error behavior.
2. **Permit binding:** mutate invocation, investigation, provider, request
   digest, normalizer version, envelope version, reservation or Bridge attempt;
   copy a permit to another invocation; consume twice; call raw transport with
   no permit. Every case refuses before fake I/O. Parameterize direct VT,
   Shodan, acquisition, Retrohunt, advisor and Bridge entries.
3. **Revocation race — PostgreSQL, two sessions:** A pauses before consume;
   B commits revoke; A resumes. Assert deny and zero fake calls. Reverse the
   serial order; assert the already-consumed one request may proceed and a
   second page/retry after revocation is denied.
4. **`RUNNING` → `NEEDS_REVIEW` race — PostgreSQL, two sessions:** A commits
   `RUNNING` and reservation then pauses; B replays and commits `NEEDS_REVIEW`;
   A resumes. Assert consume refuses and fake call count is zero. Also test
   consume-first and confirm replay cannot cancel the already-authorized one
   request.
5. **Unique semantic effect — PostgreSQL, two sessions:** distinct client
   operation keys prepare the same normalized request with no occurrence.
   Exactly one claim can consume. A second call never sends. A distinct
   explicit occurrence can intentionally consume a fresh request.
6. **Normalize and bind:** equivalent raw requests (hash casing/whitespace,
   IP/query normalization, Retrohunt clean parameters, acquisition SHA,
   advisor references and Bridge rendered prompt) produce the documented same
   identity; materially different prepared request produces another. Change
   serialized bytes after preparation and assert the transport refuses.
7. **Crash before consume — PostgreSQL:** crash after the existing reservation
   commit but before consume. Restart/replay cannot dispatch the stale
   invocation and sees no consumed claim; recovery is safe and no provider
   call occurs.
8. **Crash after consume and ambiguous response:** crash after consume but
   before transport; assert zero fake calls, consumed claim cannot be consumed
   again, and recovery becomes `NEEDS_REVIEW`. Then fake one accepted request
   with dropped response; replay asserts one accepted request total, retained
   reservation and `NEEDS_REVIEW`.
9. **Bridge physical request scope:** after a first Bridge submission, revoke
   or exhaust scope before a pre-submission retry and before a corrective
   request. Each later physical submit needs a new claim; assert its denial,
   actual submit count, and unchanged one-logical-turn budget charge.
10. **Reuse lineage/budget:** complete claim A and reuse it in invocation B
    under an allowed reuse policy. Assert A's invocation and observation
    provenance stay intact; B has an effect-use link, source observation refs,
    digest and version; no transport call and zero additional measured budget.
11. **Specialized state machines:** keep PostgreSQL Retrohunt slot, acquisition
    verification/reuse, and Bridge ModelRun recovery cases. Assert the common
    claim and family-specific attempt transition atomically at consume; an
    ambiguous outcome is not resubmitted by either state machine.
12. **No hidden send:** verify each HTTP/model adapter emits the prepared
    application request whose digest was committed. Inject serializer changes,
    retry hooks and response pagination; each additional physical request
    crosses consume. A fake proves AutoWork's call count only, not provider
    receipt.

Current PostgreSQL cases such as
`test_concurrent_operation_key_replay_creates_one_invocation` prove row
uniqueness but not one provider dispatch; the unapproved-envelope vertical
test proves its tested zero-call path but not consume/revocation ordering.
These new oracles close those specific gaps called out in M2A/M3. No test,
database, container, provider or network command was run for this artifact.

## 10. Compatibility, expand/contract migration and rollback

### Compatibility contract

Keep the `ToolInvocation.operation_key` algorithm, uniqueness, persisted rows,
status values/transitions, API response models, reservation amounts, tool
names, schema versions and current exact-key replay behavior. Do not rewrite
historical `normalized_parameters`; it currently contains raw tool arguments.
The only proposed API addition is optional `effect_occurrence_id` for an
intentional identical refresh/poll. When omitted, a matching durable effect
claim follows that operation's declared reuse policy; an ambiguous claim is
always `NEEDS_REVIEW`, never resent. Status polls and refreshes that require
fresh data must provide an application occurrence. This is an explicit
semantic contract change and must be documented before enabling it.

### Expand/contract sequence

0. **Freeze the decision set.** Record current Alembic head, supported
   PostgreSQL versions, exact provider transport entry points, operation
   normalizer versions and the family-by-family runtime composition. Do not
   infer a live database state or migrate it as part of this design.

1. **Expand schema only.** Add a new migration after the actual current head;
   leave `0001_baseline` and `0002` untouched. Add an `external_effect_claim`
   table with a restrictive FK to investigation and optional restrictive FKs
   to the source `ToolInvocation`, Bridge turn/run or family attempt; safe
   effect/normalizer versions and digests; request/artifact reference; nullable
   explicit occurrence plus a non-null occurrence digest/default sentinel;
   reservation and envelope/approval binding; state check; timestamps and CAS
   version. Add an `external_effect_use` table linking each invocation to its
   source claim, reused observation refs and budget delta. Add unique
   `(investigation_id, effect_key)` and lookup indexes/FKs/checks. Keep
   `ToolInvocation` status constraints unchanged. No data backfill and no
   dual-write in this step. Old code ignores these new tables.

   **Rollback:** roll application back; leave additive tables in place. A
   disposable empty database may exercise a guarded downgrade. Never downgrade
   a live database or drop claim/evidence history. Follow ADR-0007's additive
   revision and preflight discipline; do not treat an installation stamped
   `0001` as empty.

2. **Add the dormant module and contracts.** Add pure normalizers/serializers,
   the PostgreSQL claim adapter, coordinator, typed outcomes and
   permit-required transport interfaces. Keep the old path active until each
   consumer has a complete adapter cutover. Run the new PG and direct-entry
   oracles in implementation work before enabling any external route.

   **Rollback:** remove/disable dormant wiring; no durable claims were written
   and existing code continues. Do not add a fallback that interprets a
   missing permit as permission.

3. **Cut over in complete family slices.** Start with the frequent operator
   registry path for VT/Shodan read operations. Within each family, convert
   preparation and serialization, operator authority construction, every
   physical request, and raw transport enforcement together. Then migrate
   Retrohunt submit/status, acquisition download, advisor consultation and
   main Bridge turns. Each family slice must retain its current domain checks,
   outcomes, evidence archive and API statuses while switching the decision
   point. Drain old workers for that family before enabling permit-aware
   traffic; a process still running pre-cutover code can dispatch without a
   claim. Use a deny-all/unavailable switch for a broken family, never fallback
   to the old path.

   During this stage the durable exact-operation replay reader remains in
   place. The Retrohunt slot, verified acquisition ledger, and Bridge
   turn/ModelRun remain readable and are linked to common claims; they keep
   their domain meaning. Do not dual-write a second “shadow consume” beside the
   old read-then-dispatch callback: that would record a claim without owning
   the send. Do not bulk-reconstruct effect identity from generic raw
   `ToolInvocation.normalized_parameters`. Historical adapter bytes cannot be
   proven from that field. Any selective legacy mapping requires family
   evidence and explicit review; otherwise historical records remain
   operation-key replay/reconciliation data, not proof of a new effect claim.

   **Rollback:** before enabling the family, revert the dormant slice freely.
   After first consume, roll back only to a prior permit-aware build that still
   understands claim rows; reconcile consumed/ambiguous rows and keep schema.
   If no compatible build exists, deny new egress for that family. Never route
   post-consume traffic through the old callback or re-send consumed requests.

4. **Cut over Bridge and repeated requests.** Make each Bridge submit,
   pre-submission retry and correction a separately prepared and consumed
   request. Bind `ModelConversationTurn`/`ModelRun` to the same transaction
   with the existing logical reservation. Integrate the same path with
   `advisor.consult`; correct its internal effect classification as an LLM
   submission while preserving its external request/response schema. Route
   acquisition download egress through the seam, but leave verified local
   sample reuse/analysis local. Route Retrohunt provider submission and each
   status/results HTTP read through the seam; retain its subject slot and
   ambiguity rules.

   **Rollback:** same permit-aware-only rule. A Bridge or download failure
   fails closed and leaves the claim reviewable; no live provider call is used
   to validate rollback.

5. **Contract and delete.** After all production compositions and direct
   adapters use `send_one`, remove `ToolDispatcher.pre_dispatch_check`,
   `InvestigationService.check_current_admission`, the optional Bridge
   `before_submission` boolean callback, and any legacy wrapper that merely
   performs read-then-dispatch. Remove process-local completed-result memory
   as an external idempotency authority; keep local-tool behavior only if it
   still has a measured need. Preserve family-specific state that carries
   domain facts. See the deletion test in §11.

   **Rollback:** code rollback may restore a prior permit-aware version. It
   must not restore direct external adapter entry or the optional read
   callback. Keep the migration and claim history; a destructive downgrade is
   not the rollback plan.

### Dual-read/write and historical rows

The proposed migration needs no dual-write: doing so would make two stores
appear authoritative. Exact `operation_key` replay remains a dual-era read of
existing `ToolInvocation` rows and is not replaced by effect identity. A
matching old terminal claim cannot be inferred generally from raw arguments;
legacy in-flight rows stay sealed/reconciled under existing rules. Do not
backfill an old completed result as reusable unless the family can prove the
same normalized request, source observation and exact request version. New
claim identity is authoritative only for sends made through the new seam.
This leaves a bounded historical compatibility risk for a semantically
identical new operation after cutover; it is listed explicitly in §13 and must
be addressed per family before enabling cross-invocation reuse.

## 11. Deletion test

The seam is earning its keep only if the old coordination logic can disappear.
The acceptance test for the final contract is:

1. Delete the optional `pre_dispatch_check` argument/protocol and the
   `check_current_admission` closure from the operator service. Delete the
   optional `BridgeInvestigator.before_submission` boolean callback.
2. Search all production dispatch/transport compositions and show every
   physical external request requires the coordinator and a non-optional
   `ConsumedEffectPermit`; local-only adapters remain callable without one.
3. Call each raw external transport directly with no permit and assert typed
   denial and a zero fake call count. Call the operator route through the
   regular registry path with a valid persisted envelope and assert one
   consume, one fake request and normal API response.
4. Remove `RegistryToolDispatcher._completed` as the durable-effect replay
   mechanism; replay through PostgreSQL and assert stored claim/invocation
   behavior is unchanged after restarting the dispatcher.
5. Remove any migration compatibility wrapper that only calls the old
   read-check and then invokes the same adapter. No external production route
   may depend on that wrapper.

If deleting those elements causes policy, lock ordering, replay or retry logic
to reappear in the operator, Bridge, acquisition or Retrohunt callers, the
new module is too shallow or the seam is misplaced.

## 12. Before/after caller comparison

| Caller | Before (verified) | After (proposed) |
|---|---|---|
| Operator API → registry tool (most frequent) | `invoke` checks operation key/operator mode/disclosure; `execute_tool_call` persists `RUNNING`/reservation; optional read callback returns; dispatcher enters adapter. Raw arguments are recorded in `normalized_parameters`. | Keep API key, rows, statuses and outer flow. Dispatcher passes trusted invocation authority. Adapter prepares one request; coordinator serializes consume with revocation and `RUNNING`; transport requires a one-use permit. Same-key replay stays unchanged; same-effect reuse is linked and charged once when family policy allows. |
| Main Bridge investigation turn | `InvestigationService.run` checks disclosure/reserves then calls `BridgeInvestigator.turn`; optional callback is read before each request; bounded retry/corrective requests share logical budget reservation. | Each turn/attempt has a persisted prepared request and `ModelConversationTurn`/`ModelRun` binding; each physical send consumes separately. Same turn budget remains once. Revocation blocks subsequent retry/correction. |
| `advisor.consult` | Registry definition says `EXTERNAL_READ`, but the adapter submits a Bridge/model consultation and writes proposal provenance. | Treat the actual action as model submission internally; bind operator invocation plus persisted Bridge attempt; require permit at advisor transport. Keep tool name, input/output and proposal provenance. |
| Retrohunt submit/status/results | Submit uses its own rule/corpus slot and ambiguity state; status/results are separate provider reads. | Keep rule review and slot state. Each submit, status poll and result request consumes one common permit. Poll occurrence identifies the intended refresh; slot and common claim link to each other. |
| Acquisition / `vt.download_sample` | `HitAcquisitionService` checks G2/TLP/DNT, reserves and verifies content; verified sample reuse is no-download/zero VT units; lower attempt ledger is separate. | Only physical download crosses `send_one`; atomic consume binds acquisition attempt and common claim. A verified local reuse retains its provenance and remains zero network/budget. Hash verification/quarantine remain canonical. |
| Local compute/read tools | Registry/local adapters use schema/capability/budget, no external authorization. | No permit and no consume. Dispatcher behavior and statuses remain local. |

## 13. Risks, `UNKNOWN` and limits

| Item | Label | Consequence / gate |
|---|---|---|
| Provider-side idempotency and canonicalization | **UNKNOWN** | Never assume it. The local claim prevents a second AutoWork permit, not duplicate provider behavior after a lost response. Ambiguity remains `NEEDS_REVIEW`. |
| Transparent SDK/proxy retries and exact post-serialization bytes | **UNKNOWN** | Inspect each runtime transport. Disable hidden retry or make each physical request consume a fresh permit; otherwise the one-request guarantee is false. |
| Production callers that bypass the listed compositions | **UNKNOWN** | The source map names scoped paths, not every runtime instantiation. Inventory all direct raw adapter call sites before final cutover. |
| Legacy effect equivalence across cutover | **INFERENCE / risk** | Existing raw `normalized_parameters` and historical model/adapter rows do not generally prove exact prepared bytes. No generic backfill. A family must reconcile or selectively map its historical results before automatic cross-invocation reuse is enabled. |
| Normalizer version changes | **DESIGN constraint** | Bump versions only with an identity migration/alias or explicit occurrence policy. A version bump must never silently authorize a fresh external request for an unchanged semantic effect. |
| Occurrence source and operator UX | **DESIGN decision; runtime impact UNKNOWN** | Define which workflow creates stable occurrence IDs for refresh/poll and document the optional operator field before enabling effect dedupe. Never overload `operation_key`. |
| Result freshness policy | **INFERENCE / explicit per-family contract** | A reused successful read can be stale. Declare `reuse_policy` for each operation; require explicit occurrence for a fresh read. Preserve the source observation and make reuse visible. |
| PostgreSQL race behavior and deployment drain | **UNKNOWN until implementation oracle** | Only disposable PostgreSQL with independent sessions proves the decision order. Drain pre-cutover workers and use deny-all failure behavior; never use an in-memory test as lock evidence. |

**VERIFIED boundary:** M3 confirms no shared consume transaction currently
exists, Bridge retries/corrections can create more than one physical request,
and current `RUNNING` → `NEEDS_REVIEW` replay can race with the original
worker. **INFERENCE:** a low transport-level seam with one-request permits is
the smallest common interface that gives the operator path a trivial default
and still covers the non-registry callers. **UNKNOWN:** runtime SDK behavior
and provider receipt remain outside what this design or offline tests can
prove.
