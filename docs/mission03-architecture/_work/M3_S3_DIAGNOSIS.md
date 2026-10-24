# M3 / Phase 1 / S3-B1A1 — external-effect diagnosis

**Code SHA studied:** `aa0cef3ee07525ca77d6b1358252ebf5735a3dde`  
**Scope:** read-only source and document review of effect identity, admission,
dispatch decision, provider paths, persistence, and revocation. Only this
diagnosis artifact is written. No test, database, provider, Docker, or network
command was run.

Labels below distinguish **VERIFIED**, **INFERENCE**, and **UNKNOWN**. A cited
path and line range points to current source unless the citation names a prior
review/design.

## 12 mandatory questions

### 1. Who qualifies the effect?

**VERIFIED.** The registered `ToolDefinition.effect` is assigned by each tool
registration and consumed by `OperatorToolInvocationService` and
`RegistryToolDispatcher`; request arguments do not choose the effect
(`investigation_tools.py:39-48,151-169,732-740,865-885`; `operator_tool_invocation.py:848-861`).

- VT reports/search/behaviour and Shodan host/search/domain/count are
  `EXTERNAL_READ` (`investigation_vt_tools.py:1684-1705`;
  `investigation_shodan_tools.py:1208-1223`).
- Retrohunt submit is `EXTERNAL_SUBMIT`; status/results are `EXTERNAL_READ`
  (`investigation_retrohunt_tools.py:81-105`). This is the only
  `EXTERNAL_SUBMIT` registration found in the scoped application registrations.
- `vt.download_sample` is `ACQUIRE`; `sample.analyze` and
  `sample.set_qualification` are `LOCAL_COMPUTE`
  (`investigation_acquisition_tools.py:663-715`). Advisor consultation is
  labeled `EXTERNAL_READ`, while advisor reparse is `LOCAL_COMPUTE`
  (`investigation_advisor_tools.py:3395-3409,3789-3823`).
- The enum also has `LOCAL_READ` and `EXTERNAL_ACTIVE`; no current
  `EXTERNAL_ACTIVE` registration was found in the scoped registration source.
- Main Bridge-led investigation turns are outside the registry: `run` checks
  disclosure and calls `BridgeInvestigator.turn` directly
  (`investigation_service.py:595-613,661-672`).

### 2. Who normalizes parameters, and is that form persisted or bound?

**VERIFIED.** There is no shared preparation/normalization module. The
dispatcher strictly validates JSON schema, then calls the adapter with the
original `arguments` mapping (`investigation_tools.py:597-612,741-784`).
Operation-specific adapters/services normalize later: VT trims/lowercases a
file hash before calling its port (`investigation_vt_tools.py:686-697`);
Shodan validates/canonicalizes IP, query, page and domain in its adapters
(`investigation_shodan_tools.py:665-679,767-792,929-945`); Retrohunt trims the
corpus ID and validates/normalizes parameters before making its reservation
(`retrohunt.py:129-149,508-537`); acquisition validates the SHA while
registering the hit (`investigation_acquisition.py:114-141`); advisor trims
the question, parses UUID references, and assembles the transmitted context
inside the adapter (`investigation_advisor_tools.py:174-199,889-907`).

The generic invocation stores `arguments` in the field named
`normalized_parameters` (`investigation_service.py:1031-1040`). The operator
`operation_key` hashes the client idempotency key plus a canonical JSON payload
containing the raw arguments, purpose, schema version and expected state
version (`operator_tool_invocation.py:718-749`). The limited
`_semantic_parameters_fingerprint` is a duplicate guard for selected active
ambiguous operations; it is not a persisted, versioned prepared effect and is
not bound to adapter bytes (`operator_tool_invocation.py:880-924,1330-1398`).

There are scoped exceptions, not a common contract: Retrohunt persists its
clean parameter object with its reservation; acquisition has a deterministic
investigation/hash association and verifies reuse; Bridge persists the rendered
message hash and per-turn idempotency key. None coordinates those identities
with a common dispatch decision.

### 3. Where is the canonical effect identity, distinct from a client `operation_key`?

**VERIFIED.** There is no cross-adapter canonical effect identity/claim.
`operator_tool_invocation.py:744-749` defines a client-operation replay key;
`ToolInvocation.operation_key` is unique in the generic journal
(`models/investigation.py:59-66`). The dispatcher's completed-result cache is
process-local and also keyed by that operation key (`investigation_tools.py:619-625,696-703`).

Specific durable identities exist, with narrower meaning:

- Retrohunt derives a version-tagged SHA-256 from the exact rule source hash,
  corpus identity/hash and clean parameters; the reservation repository uses
  that key to find/reuse the subject-scoped reservation
  (`retrohunt.py:129-149,491-531`; `repositories/retrohunt.py:23-64`). This is
  a Retrohunt-specific submission identity, not the common gateway claim.
- Acquisition derives an association UUID from investigation plus normalized
  sample SHA-256 and reuses only verified quarantine content
  (`investigation_acquisition.py:121-141,201-243`). This identifies sample
  content/resource reuse, not arbitrary provider effects.
- Bridge derives a per-turn-attempt key from investigation, turn index, context
  hash, prompt version and attempt; `ModelConversationService.add_turn` persists
  a `ModelConversationTurn.id`, its key/input hash, and linked `ModelRun` before
  gateway execution (`bridge_investigator.py:1448-1462`;
  `model_conversations.py:1282-1305`). It is attempt identity, not normalized
  cross-invocation effect equivalence.

**UNKNOWN.** Provider-side idempotency/canonicalization is not established by
these application identities.

### 4. Which module owns the right to trigger an external submission?

**VERIFIED.** No shared outbound module owns this right across the scoped
effects. The registry dispatcher enforces registration, schema, capability,
selected policy and budget before calling an adapter; it does not own an
atomic, durable send decision (`investigation_tools.py:619-784`). Tool-specific
modules still initiate their own egress. Retrohunt is the clearest specialized
case: `RetrohuntService.submit` checks the exact reviewed version and
authorization, commits a durable subject reservation, then calls the provider
(`retrohunt.py:108-161,199-212,331-365`). Acquisition has its own service and
policy checks (`investigation_acquisition.py:144-200,290-322`). Advisor and
main Bridge use the model-conversation path rather than a common effect
gateway (`investigation_advisor_tools.py:3395-3410`;
`bridge_investigator.py:555-560,642-690`).

### 5. Can a transport or adapter bypass admission?

**VERIFIED. Yes.** `ToolDispatcher` exposes an optional
`pre_dispatch_check`; it invokes the callback only when supplied, then enters
`adapter.execute` (`investigation_tools.py:412-427,779-784`).
`ToolExecutionContext` has no opaque permit or decision binding
(`investigation_tools.py:224-245`). Direct adapter entry or dispatcher entry
without the callback can therefore skip the service admission callback. Some
adapters repeat local disclosure checks, but those are not a serialized
revocation/dispatch decision.

The Bridge-led `before_submission` callback is also optional, and
`BridgeInvestigator.turn` calls its transport when the callback is absent
(`bridge_investigator.py:1172-1183,1231-1270,1271-1291`). The Bridge
`ModelConversationTurnTransport` and `ModelConversationService` provide durable
turn/run recording, but that is not a shared effect permit.

### 6. Which PostgreSQL transaction is the serialized decision point?

**VERIFIED. None exists today.** `execute_tool_call` writes the `RUNNING`
invocation and envelope reservation and commits them before dispatch
(`investigation_service.py:1031-1040,1110-1153`). Its fresh check performs
ordinary repository reads of the envelope and reservation, then returns; the
dispatcher invokes the adapter after that callback ends
(`investigation_service.py:1190-1237`; `investigation_tools.py:779-784`).

Revocation and envelope CAS do serialize their own envelope writes:
`InvestigationEnvelopeService.revoke` writes a revoked envelope and commits
(`investigation_governance.py:428-487`), and
`save_if_version` takes `SELECT … FOR UPDATE` on the same envelope row
(`repositories/investigation.py:1426-1434`). The read-only admission callback
does not acquire that lock together with an effect-decision write. The
reservation commit is a durable admission checkpoint, not a send decision.

### 7. Which persisted states must be checked atomically?

**VERIFIED current state; INFERENCE required consume contract.** Current state
includes an envelope version/approval decision and its G2/TLP/disclosure
bounds; budget reservations with `RESERVED`/`COMMITTED`/`RELEASED`; a versioned
`ToolInvocation` whose status can be `RUNNING` or `NEEDS_REVIEW`; and, for
Bridge, persisted `ModelConversationTurn` and `ModelRun` status/submission
state (`domain/investigation.py:18-27,126-130,219-230,431-445`;
`models/investigation.py:59-99,621-675`;
`models/model_execution.py:26-36,196-247`).

A consume decision must atomically verify and bind at least: exact current
envelope version and approval/revocation/disclosure bounds; the same
investigation's exact reservation key, amounts and `RESERVED` state; one
unconsumed effect decision/claim; the persisted operator invocation ID still
in `RUNNING`; its tool/effect/schema and normalized parameters plus normalizer
version/digest; and the equivalent Bridge attempt state (`ModelConversationTurn`
and linked `ModelRun`) when that path is used. Any stop/cancel state used to
refuse egress must be included in the same final check. This is the state
coordination missing from the current read callback.

### 8. What happens if `RUNNING` becomes `NEEDS_REVIEW` after reservation?

**VERIFIED.** A replay with the same operator key/payload changes a persisted
`PENDING`/`RUNNING` invocation to `NEEDS_REVIEW`, commits and returns without a
new dispatch (`operator_tool_invocation.py:863-878`). The original worker has
already committed its reservation (`investigation_service.py:1139-1153`). Its
later fresh check verifies the envelope and reservation but never rereads the
invocation status (`investigation_service.py:1190-1226`); if those still match,
the dispatcher can enter the adapter (`investigation_tools.py:779-784`). Thus
the paused original worker may still submit after the durable invocation has
become `NEEDS_REVIEW`. This confirms the M02 review's principal blocker.

### 9. What is the scope of one authorization over retry, corrective requests and pagination?

**VERIFIED current behavior; UNKNOWN as a consumed-permit scope.** There is no
consumed permit. Main Bridge checks the optional current-policy callback before
and after pacing for each physical submission. It can make bounded
pre-submission retries with changed attempt keys, and corrective requests use a
changed context/hash and another attempt key (`bridge_investigator.py:1204-1219,1231-1291,1321-1340,1411-1433`). The same Bridge call can therefore create
multiple egresses. No Bridge pagination loop was found in the scoped
`BridgeInvestigator.turn` implementation; **UNKNOWN** for transport behavior
outside that loop. Registered provider adapters can also issue multi-page
requests under one adapter execution/reservation (for example, VT adapters are
configured with `max_pages`, `investigation_vt_tools.py:1571-1654`).

Design must explicitly say whether a decision authorizes one physical request
or a bounded adapter session. A retry, page/cursor request or correction may
share a consumed decision only if its exact request set and bounds are bound
before consume; a changed Bridge prompt/context must not be implicitly covered.
Revocation behavior for the remaining egresses in an already consumed session
must be stated.

### 10. How is an ambiguous external submission represented?

**VERIFIED.** For registered effects, an unproven external failure is changed
to `NEEDS_REVIEW`; the reservation is retained and ambiguous response
observations are not persisted as successful result evidence
(`investigation_service.py:1238-1260,1269-1293,1299-1319,2797-2810`). Same-operation
replay does not redispatch. Only typed dispatcher proof counts as confirmed
non-execution; adapter-supplied proof is stripped after adapter return
(`investigation_tools.py:799-801`; `investigation_service.py:3323-3325`).

For main Bridge, `ModelConversationTurn` and linked `ModelRun` persist a
pre-egress running record; uncertain outcomes are sealed as review-required
and not automatically resent (`model_conversations.py:1282-1305,1403-1460`;
`domain/model_conversations.py:153-169,220-235`). The Bridge turn key and
attempt record are useful existing state, but are not atomically bound to the
operator effect decision described above.

### 11. How are lineage and budget preserved when a result is reused?

**VERIFIED for exact replay. UNKNOWN for generic semantic reuse.** Replaying
the same persisted operation key returns its `ToolInvocation`, response refs
and budget state without dispatching again (`investigation_service.py:1079-1108,1294-1319`;
`models/investigation.py:59-66`). Observations have an invocation reference
(`domain/investigation.py:528-540`), preserving the original invocation
lineage. Acquisition's verified same-hash reuse records reuse provenance and
reports zero VT/download budget (`investigation_acquisition.py:201-243,245-288`).

There is no common terminal-effect claim that reuses a result across distinct
operator invocation IDs. The semantic fingerprint only blocks selected
`RUNNING`/`NEEDS_REVIEW` cases (`operator_tool_invocation.py:880-924,1330-1398`).
Therefore cross-invocation result lineage, response-ref linkage, and one-time
measured-budget accounting remain unspecified. Provider usage must not be
charged twice or silently detached from the source observation if a future
effect claim reuses a result.

### 12. Which transitions/constraints require a migration, and is it additive under ADR-0007?

**VERIFIED.** Existing `PENDING`, `RUNNING`, and `NEEDS_REVIEW` values and the
`RUNNING → NEEDS_REVIEW` transition already exist in the domain and persisted
status check; using those states alone does not require a new status migration
(`domain/investigation.py:18-27,468-509`;
`models/investigation.py:59-79`). Existing budget reservation states are
already constrained to `reserved`, `committed`, and `released`
(`models/investigation.py:621-675`). Adding a new state to either constrained
column requires a forward migration to update its database CHECK constraint.

Persisting a common effect claim/digest and normalizer version, one-use
decision/consume state, or an explicit link/state for the Bridge attempt
requires additive columns and/or tables plus the needed unique, foreign-key,
status and lookup constraints. If the design adds status values to existing
`tool_invocations`, `model_conversation_turns` or `model_runs`, the current
status/submission-state CHECK constraints must be updated by that revision.

This is compatible with ADR-0007 only as a new additive revision after current
head: preserve frozen `0001_baseline`, do not rewrite `0002`, retain existing
rows/allowed values, and preflight any supported stamped/manual schema state
before DDL (`ADR-0007:19-45`; `backend/AGENTS.md`, migration rules). No migration
was authored or run here.

## Prior findings checked against current source

The M02 independent review's principal finding still holds: the current
admission callback is read-then-adapter rather than a serialized decision
(`S3_DISPATCH_REVIEW.md:9`; current `investigation_service.py:1190-1237` and
`investigation_tools.py:779-784`). Its `RUNNING → NEEDS_REVIEW` omission also
holds exactly (`S3_DISPATCH_REVIEW.md:10`; current
`operator_tool_invocation.py:863-878` and `investigation_service.py:1190-1226`).

The review's effect classification, raw-argument persistence, process-local
dispatcher lock/cache, callable provider paths, and missing cross-invocation
lineage still hold (`S3_DISPATCH_REVIEW.md:11-12,15-17`; current
`investigation_tools.py:619-625,741-784`; `investigation_service.py:1031-1040`;
`operator_tool_invocation.py:718-749,1330-1398`). The permit forgery/replay
protections in its design section remain unimplemented because no permit
exists (`S3_DISPATCH_REVIEW.md:13`; `investigation_tools.py:224-245,412-427`).

The Bridge scope finding remains open, with one correction: the scoped
implementation **does** persist a `ModelConversationTurn` UUID, idempotency
key/input hash and linked `ModelRun` before gateway execution
(`model_conversations.py:1282-1305`). The M02 claim that no durable Bridge
attempt ID exists is stale. The remaining issue is coordination of those
persisted attempt states with consume/revocation, and the authorization scope
across retries/corrections; no Bridge pagination was found in this path
(`S3_DISPATCH_REVIEW.md:14`; `bridge_investigator.py:1204-1219,1321-1340,1411-1433`).

The M02 design's `IMPLEMENTABLE_NOW` conclusion was superseded by the
independent review's `BLOCKED_DESIGN` verdict, which identifies the missing
invocation-status check (`S3_DISPATCH_DESIGN.md:202-217`;
`S3_DISPATCH_REVIEW.md:25-29`). The current-source audit agrees with the
review, not the earlier verdict.

From B1A1, canonical effect identity and revocation/dispatch atomicity remain
open (`B1A1_REVIEW.md:184-211`). Two historical details need qualification:
current `ToolExecutionContext` no longer has the forgeable dispatch-guard
booleans described in the older B1A1 section (`B1A1_REVIEW.md:265-309`;
`investigation_tools.py:224-245`); and registered dispatch now strips an
adapter-returned framework proof before interpreting the result
(`B1A1_REVIEW.md:33`; `investigation_tools.py:799-801`;
`investigation_service.py:3323-3325`). Neither correction supplies atomic
admission or a durable consume decision.

## Durable decision point and revocation semantics

**VERIFIED current state:** no durable dispatch-decision row/state exists in
the scoped path. The durable cut required by the design is the commit of a
one-use consume/decision transaction that holds the same envelope row lock as
revocation and atomically checks/writes the envelope version, reservation,
effect claim, invocation, and equivalent Bridge attempt. Only after that
transaction commits may the outbound session call a provider. This is a
candidate decision point, not current behavior.

**INFERENCE — feasible transaction shape:** a short PostgreSQL transaction
that locks/revalidates the envelope and the required invocation/effect/Bridge
rows, records the consumed decision, and commits before network I/O is feasible
in principle. The current revoke path already locks the envelope row
(`repositories/investigation.py:1426-1434,1453-1465`). Holding that lock through
HTTP is unnecessary and would couple revocation and the connection pool to
provider latency. No such transaction was implemented or executed here.

- Revocation committed before consume: consume observes the revoked/newer
  envelope and denies egress. Release the reservation/effect claim only if the
  framework proves pre-submission.
- Consume committed first: that already-authorized execution may proceed even
  if revocation commits before the physical HTTP write. PostgreSQL cannot
  atomically guarantee an arbitrary HTTP departure or cancel a request already
  authorized/in flight. If the post-consume result is uncertain, keep it
  `NEEDS_REVIEW`; never infer safe resubmission.

## Remaining BLOCKED_DESIGN decisions

S3 is **S3_BLOCKED_DESIGN**. Before implementation, a reviewed design still
needs to specify and accept:

1. The prepared-effect interface for every effect family: canonical normalized
   request, normalizer/schema version, bounds, and how the exact values/bytes
   later sent are proven equal to the persisted prepared form.
2. A cross-adapter durable effect identity/claim distinct from client
   `operation_key`, including when an explicit occurrence ID permits an
   intentional identical refresh or poll.
3. The single mandatory outbound seam and denial behavior for direct adapter
   and transport entry, including unregistered main Bridge and acquisition
   paths.
4. The consume row/state model, transition rules, unique keys, and lock/order
   contract that atomically coordinates envelope+version, exact reservation,
   effect decision, `RUNNING` invocation, normalized parameters/version, and
   the persisted Bridge attempt (`ModelConversationTurn`/`ModelRun`) with
   revocation.
5. Whether each physical retry/page/corrective submission is a separate
   consume or belongs to a bounded session, and what a revocation after consume
   means for subsequent egresses in that session.
6. Crash, timeout, cancellation, late-response, and reconciliation rules,
   including exact proof required to release a claim/reservation and how reused
   results preserve invocation/observation lineage and charge measured budget
   once.
7. Which existing status values are reused and which new persisted rows,
   columns, status checks, unique constraints, and FKs require an additive
   migration.

These are design gates, not a full architecture proposal.

## Constraints for designers

- **Identity:** keep the durable effect identity separate from the caller's
  `operation_key`. Version each operation-specific normalizer and bind the
  normalized values and bounded request set to the exact outbound payload.
  Require an explicit application occurrence for an intentional identical
  refresh/poll.
- **Ordering and invariants:** serialize final consume with revocation, the
  exact envelope version, reservation, effect claim, invocation status, and
  Bridge turn/run status. Commit the decision before entering the provider
  transport; do not rely on an in-process lock as cross-process serialization.
- **Authorization scope:** state whether one decision covers a single physical
  request or a bounded multi-request session. Corrections with changed Bridge
  context and any pagination/retry egress must follow that rule. Local effects
  remain outside external permit checks.
- **States and errors:** fail closed on absent, stale, expired, reused,
  misbound, or unavailable decision state. A refusal before consume needs typed
  framework proof before releasing reservation/claim. A consumed request with
  unknown outcome remains `NEEDS_REVIEW` and must not be automatically resent.
- **Privacy and provenance:** enforce current G2 provider scope, TLP,
  `external_llm_allowed`, `do_not_submit`, sample restrictions and revocation
  at consume. Never put AutoWork credentials or signed URLs in effect identity,
  traces or logs. Preserve authorized CTI evidence references, SHA-256,
  source/version links and limitations; treat provider/model text as untrusted
  data and make any policy omission observable.
- **Crash, replay and budget:** cover pre-consume crash, post-consume/pre-HTTP,
  post-departure/pre-response, cancellation, timeout and late response. Reuse
  must preserve source observation and invocation lineage and account measured
  usage exactly once; unresolved outcomes retain the reservation until
  reconciliation.
- **Oracles:** keep provider/Bridge fakes for payload and zero-call assertions,
  and add the disposable-PostgreSQL concurrency race below. Neither fake
  transport nor offline replay proves provider receipt.

## Required PostgreSQL race oracle

Add a disposable-PostgreSQL integration test using two independent sessions
(and ideally separate worker processes) with fake provider/Bridge egress and
deterministic barriers:

1. Worker A persists reservation plus `RUNNING` invocation and matching
   normalized effect/version and Bridge attempt, then pauses before consume.
2. Worker B replays the operation, commits `NEEDS_REVIEW`, then A resumes.
   Consume must lock/reload the invocation and equivalent Bridge attempt,
   refuse, and observe zero fake transport calls. A memory fake cannot prove
   this transaction ordering.
3. Separately race revocation against consume under the same envelope row lock.
   Revocation-first must deny with zero egress. Consume-first must persist one
   winner; the test may allow its fake egress after revocation commits, matching
   the stated cut. Concurrent same-effect claims under distinct client keys
   must also have one winner.

The existing in-memory barrier test covers same-working-state CAS and one
adapter call (`tests/test_b1_admission_durability.py:707-743`). The existing
PostgreSQL duplicate-operation test proves one invocation row, not serialized
revocation/consume or one external dispatch
(`tests/integration/test_investigation_persistence_postgres.py:542-587`). A
fake cannot prove real `SELECT … FOR UPDATE`, independent-session ordering,
database uniqueness/CAS under contention, or the external provider's receipt
of an HTTP request. No test command was run for this diagnosis.

## Verdict

**S3_BLOCKED_DESIGN.** The required commit-time decision is not implemented,
and the design has not yet accepted its atomic state set, Bridge scope, shared
effect identity, and cross-invocation reuse rules. The required consume check
must include the persisted `RUNNING` invocation and equivalent Bridge-attempt
state, in the same serialized transaction as envelope+version, reservation,
effect decision, normalized parameters/version, and revocation.
