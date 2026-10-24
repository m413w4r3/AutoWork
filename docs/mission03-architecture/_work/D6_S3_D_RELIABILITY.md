# D6 S3-D — Reliability-first external effect decision

**Study point:** aa0cef3ee07525ca77d6b1358252ebf5735a3dde  
**Nature:** design only. This artifact proposes a module and interface; it does not describe implemented behavior.  
**Constraint:** RELIABILITY FIRST.

## Evidence labels

- **VERIFIED** means directly supported by current source at the study point or by the named active contract.
- **INFERENCE** means a consequence of those verified paths, not a behavior exercised here.
- **PROPOSED** means the reliability contract recommended below.
- **UNKNOWN** means source inspection cannot establish the runtime or provider fact.

The study point is on branch mission03/architecture-design. No tests, PostgreSQL, MinIO, Docker, providers, or network calls were run. No production or test files were changed.

## 1. Current seam and proposed seam

### Current seam — VERIFIED

The operator path is:

API → OperatorToolInvocationService → InvestigationService.execute_tool_call → RegistryToolDispatcher → registered adapter → provider port/transport.

The service persists a RUNNING ToolInvocation and budget reservation before dispatcher entry. The optional fresh-admission callback checks current envelope/reservation state, then returns before the adapter runs. It does not atomically claim an external send. The dispatcher has an in-process lock/cache; neither is a cross-process decision. The callback does not re-read invocation status, so a paused worker can pass after replay has changed RUNNING to NEEDS_REVIEW. See M1-A §a–d; M3 §1 questions 4–8; current source at investigation_service.py:1007–1321, investigation_tools.py:412–427,619–798, and operator_tool_invocation.py:863–878.

The Bridge-led path is separate:

InvestigationService.run → BridgeInvestigator.turn → ModelConversationTurnTransport/ModelConversationService → ModelGateway → provider.

A ModelConversationTurn and ModelRun are durably created before gateway execution, but not in the transaction that checks the envelope, reservation, and RUNNING invocation. Bridge retries and corrective submissions use distinct request identities. The current before_submission callback is optional. See M1-A §a–d; M3 §1 questions 1, 4–10; bridge_investigator.py:1172–1445 and model_conversations.py:1282–1305.

The persisted budget reservation is normalized into investigation_budget_reservations and investigation_budget_reservation_amounts. Its key is investigation plus operation key. Envelope governance is independently locked/versioned. Retrohunt, acquisition, and Bridge have additional durable attempt records, but no shared decision row coordinates them. See M1-A §e; models/investigation.py:547–675 and repositories/investigation.py:1414–1577.

### Proposed seam — PROPOSED

Place one deep application module, ExternalEffectCoordinator, between effect preparation and every physical provider/model request. It owns the durable prepare/consume/outcome lifecycle. The seam is at a single physical outbound request, below orchestration and above concrete transport adapters.

The operator dispatcher and Bridge loop remain orchestration modules. They may prepare a request and request a dispatch, but they do not grant permission. VT, Shodan, Retrohunt, acquisition, advisor, Bridge, and any lower HTTP/model transport are adapters at this seam. Every raw outbound adapter requires a consumed permit and the exact prepared request. There is no optional admission callback and no alternate direct-send method.

Local reads and local computation remain outside this module. External reads, acquisition, external submissions, and model consultations enter it. Advisor consultation must be catalogued by its actual external model submission effect, even though its present ToolDefinition is EXTERNAL_READ. The effective effect is the stricter of registered policy and the physical action; a registration label cannot weaken the actual send.

This seam has depth because one implementation owns CAS, authorization freshness, uniqueness, budget/reservation linkage, crash semantics, provenance, and recovery. Its leverage is the same contract across the operator and Bridge paths. Its locality keeps those rules out of every provider adapter. PostgreSQL is the real adapter for transaction behavior; a fake outbound adapter is the test adapter. No new transport port is justified unless production and test adapters both use it.

## 2. Public contract and pseudo-code

### Types — PROPOSED

~~~python
@dataclass(frozen=True)
class EffectOwner:
    investigation_id: UUID
    invocation_id: UUID | None       # operator/tool path
    bridge_turn_id: UUID | None      # Bridge path
    model_run_id: UUID | None        # required with bridge_turn_id
    reservation_operation_key: str
    expected_owner_version: int

@dataclass(frozen=True)
class PreparedEffect:
    effect_family: EffectFamily      # EXTERNAL_READ, ACQUIRE, EXTERNAL_SUBMIT, EXTERNAL_LLM
    provider: str
    operation: str
    tool_schema_version: str
    normalizer_id: str
    normalizer_version: str
    occurrence_id: UUID | None
    reuse_policy: ReusePolicy
    route_id: str                    # versioned provider operation, no credentials
    method: bytes
    path_and_query: bytes
    content_type: bytes | None
    body: bytes                      # complete bytes the transport will send
    semantic_identity_sha256: str
    request_sha256: str

@dataclass(frozen=True)
class ConsumedEffectPermit:
    dispatch_id: UUID
    effect_key: str
    request_sha256: str
    envelope_version: int
    one_use_nonce: object            # coordinator-created in-process capability
~~~

EffectOwner is a closed typed sum in implementation: exactly one of invocation or Bridge turn/run is present. Acquisition and Retrohunt retain their existing attempt/slot identifiers as linked child records. A direct call with no durable owner and reservation is denied before transport.

PreparedEffect is immutable and complete. Its identity is not the caller’s operation_key. A versioned, domain-separated hash is computed from investigation ID, effect family, provider, operation, route/version, schema version, normalizer ID/version, exact request SHA-256, and occurrence token. Length-prefix or canonical JSON encoding must make the hash unambiguous. Credentials, service keys, passwords, cookies, and signed URLs are never in the prepared request or identity. Authentication is injected by the transport after permit validation.

The effect identity describes AutoWork’s exact prepared request, not provider-side semantic equivalence. The normalizer version is part of the identity. A new normalizer version cannot silently reuse an old claim. No provider canonicalization or idempotency is assumed. For a reusable read with no occurrence ID, preparation uses a reserved terminal-reuse token and permits lookup/reuse only; it never authorizes a fresh send. A fresh poll, refresh, retry, correction, acquisition, or submit requires a new explicit occurrence ID. The occurrence token is included in effect_key, not semantic_identity_sha256.

### Interface — PROPOSED

~~~python
async def dispatch(
    owner: EffectOwner,
    prepared: PreparedEffect,
    transport: PreparedTransport,
) -> EffectOutcome:
    # Persist immutable request reference and PREPARED decision.
    # In one short PostgreSQL transaction, revalidate and consume.
    # Only a committed consume returns a capability.
    # Invoke exactly one physical request, with no SQL lock held.
    # Persist outcome, observation links, and budget settlement.
~~~

The coordinator loads canonical governance and owner state itself. It does not accept caller-supplied booleans such as external_llm_allowed as proof. It returns a durable outcome or a typed denial. PreparedTransport.send(prepared, permit) is mandatory and is also the only transport entry. It checks that the permit is coordinator-created, unused, bound to the dispatch ID and request digest, and used for the matching route. Its final low-level send consumes the in-process one-use nonce. Reusing the object for a second physical request fails locally; a second worker cannot obtain a second permit because PostgreSQL consume is one-use.

For dynamic pagination, retries, redirects, fan-out, or corrective prompts, the orchestrator calls dispatch again for each physical request with a distinct occurrence ID and exact request bytes. The module does not authorize a session. Adapters must disable transparent retries and redirects, or route each subsequent physical request through another dispatch. If a client library cannot expose its physical requests to this seam, it is not an acceptable adapter.

The coordinator issues no permit if PostgreSQL, the request artifact, or authorization state is unavailable. It reads and verifies the immutable request artifact before opening the consume transaction; while locks are held it compares the persisted SHA/reference to those verified bytes without making a MinIO or other artifact-store call. A commit acknowledgement lost after consume is treated as consumed-but-unknown: lookup may report the durable state, but the caller does not reconstruct a permit or resend. The permit type has a module-private constructor/authority marker; raw callers cannot create a valid capability by instantiating its public fields.

### Typed outcomes — PROPOSED

- **SUCCEEDED:** a correlated response and all canonical observation/artifact references were committed.
- **DENIED:** consume did not commit; includes a framework proof code and no egress occurred.
- **NEEDS_REVIEW:** consume committed but execution/result is unknown, or durable records conflict.
- **CONFIRMED_NOT_SENT:** a trusted transport/framework proof establishes no request bytes left the process. An adapter cannot author this proof.
- **RECONCILED_EXECUTED, RECONCILED_NOT_EXECUTED, ABANDONED:** explicit, versioned human reconciliation states. They never trigger a send.
- Persistence failure before consume is a fail-closed denial. Persistence failure after consume is ambiguous and retains the reservation.

## 3. Invariants, timing, versions, errors, and confidentiality

### Durable decision state machine — PROPOSED

One row represents one physical request. A logical ToolInvocation or Bridge turn may link to several rows, one per page/retry/correction. Every row has a monotonically increasing version and an append-only transition event.

| Current state | Allowed next state | Guard and durable effect |
|---|---|---|
| PREPARED | CONSUMED | Only the consume transaction may do this. It locks and reloads every required row, checks all guards below, writes the one-use decision and Bridge attempt submission state, then commits. This commit is the decision point. |
| PREPARED | DENIED | Guard failed before consume. Persist typed framework proof; no permit exists. Resolve only this dispatch’s reservation amount as unconsumed. |
| PREPARED | ABANDONED | Recovery proves there is no committed consume for this dispatch. No permit could have been issued; no egress. Release its unconsumed reservation. |
| CONSUMED | SUCCEEDED | Exact correlated terminal result received; response/artifact and all observation/provenance refs are durable; settle usage exactly once. A response that does not establish a terminal outcome remains NEEDS_REVIEW. |
| CONSUMED | CONFIRMED_NOT_SENT | Trusted transport proves no bytes were submitted. Preserve proof. Any retry is a new dispatch with a new occurrence ID and its own consume. |
| CONSUMED | NEEDS_REVIEW | Timeout, cancellation, process death, lost acknowledgement, response loss, transport exception without conclusive proof, or inconsistent state. Keep reservation; never automatically resubmit. |
| NEEDS_REVIEW | SUCCEEDED | Late response matches this exact dispatch/request/Bridge attempt; commit its result and provenance by CAS. This does not create another request. |
| NEEDS_REVIEW | RECONCILED_EXECUTED | Human attaches authoritative evidence that this exact effect executed. Preserve evidence and charge conservatively if usage is unknown. No automatic result fabrication. |
| NEEDS_REVIEW | RECONCILED_NOT_EXECUTED | Human records framework/provider evidence that this exact request did not execute. Release only the unconsumed amount proven safe. New work needs a new occurrence and dispatch row. |
| NEEDS_REVIEW | ABANDONED | Human accepts unresolved outcome. Keep effect identity poisoned against implicit reuse and settle the reserved amount conservatively; never retry automatically. |
| Terminal state | none | No reset, delete, or return to PREPARED. A permitted new physical attempt is a new row with a new occurrence ID. |

A PREPARED row means no caller could have obtained a permit. A CONSUMED row is intentionally conservative: it cannot establish whether a request reached the provider. The absence of a response is never proof of non-execution. No SQL lock spans transport.

### Crash matrix around durable writes — PROPOSED

| Durable write / crash point | State after restart | Recovery and egress rule |
|---|---|---|
| Before any write, during pure normalization | No invocation-specific decision exists | No permit; zero egress. Re-run preparation only from the durable owner request. |
| Request artifact write succeeds, then crash before its PostgreSQL reference | An unreferenced immutable blob may exist | It is not canonical and cannot authorize egress. Garbage collection may remove it only after proving no database reference exists. |
| Operator invocation plus reservation transaction commits, then crash before request artifact or PREPARED row | RUNNING plus RESERVED, but no dispatch decision | Absence of a consumed row proves no authorized egress under the mandatory adapter contract. Recovery marks the invocation non-dispatchable, records a pre-consume resolution, and releases only the unconsumed reservation. |
| Bridge turn/ModelRun persistence commits, then crash before reservation/preparation | Durable attempt remains NOT_SUBMITTED with no dispatch decision | No permit and no egress. Mark the orphaned attempt for safe recovery; do not invoke ModelGateway directly. |
| Bridge logical reservation commits after its attempt but before request artifact/PREPARED | Attempt plus RESERVED budget, but no dispatch decision | No permit and no egress. Recovery marks the attempt non-dispatchable and releases only the unconsumed reservation. |
| PREPARED dispatch row commits, then crash before consume starts | PREPARED; owner/reservation remain linked | No permit exists. Recovery may transition to ABANDONED/DENIED and release the unconsumed reservation after the same ordered row checks. |
| Consume transaction starts but rolls back or loses its database connection before commit | PREPARED remains, or the transaction is rolled back | No permit is returned; zero egress. Retry only the transaction after fresh reads. |
| CONSUMED commit succeeds, but acknowledgement is lost or process crashes before receiving the permit | CONSUMED is durable; no permit is reconstructed | Mark NEEDS_REVIEW. Do not send from a replay worker. No automatic retry even though the process may have died before transport. |
| Permit is returned, then crash before transport entry | CONSUMED, outcome unknown | NEEDS_REVIEW; keep reservation; no auto-resubmit. The database cannot prove that the process did not begin sending. |
| Transport begins and crashes before/after bytes leave, before response | CONSUMED, no terminal response | NEEDS_REVIEW; keep reservation; no auto-resubmit. A request may have reached the provider. |
| Provider response arrives, then process crashes before response artifact write | CONSUMED, response lost | NEEDS_REVIEW; no auto-resubmit. Reconcile using evidence tied to this dispatch if available. |
| Response artifact write succeeds, then crash before outcome transaction | CONSUMED plus an unreferenced response blob | The blob is not yet a canonical observation. Recovery may correlate it to the same dispatch and finalize by CAS; otherwise NEEDS_REVIEW. Never send again. |
| Outcome transaction begins and rolls back | CONSUMED remains; response blob may be orphaned | NEEDS_REVIEW unless recovery can retry the same idempotent database finalization from the exact correlated response. No transport retry. |
| Outcome transaction commits, then acknowledgement/client response is lost | SUCCEEDED plus committed refs and settlement | Replay returns the stored outcome and lineage; zero new egress and no second budget charge. |

The operator invocation/reservation creation is one PostgreSQL transaction. For Bridge, its persisted turn/run and the logical reservation must be linked before consume; the consume transaction verifies the exact turn/run state and changes ModelRun submission state together with the dispatch row. The final outcome transaction atomically links canonical observations, invocation/turn result, and reservation settlement. Artifact writes can precede that transaction but are not canonical until referenced by it.

### Consume guards — PROPOSED

The single READ COMMITTED transaction must re-read after any lock wait and require:

1. The current envelope belongs to the same investigation, has the exact prepared version, remains active, allows this provider and effect, and still meets G2, TLP, external_llm_allowed, do_not_submit, sample restrictions, and human approval rules.
2. Any mutable investigation stop/cancel generation used to block egress is unchanged and not stopped. Lock that row at the documented rank below.
3. The ToolInvocation is still RUNNING at the expected version, has the same investigation/tool/schema and links to this prepared request; or, for Bridge, the exact persisted turn and linked ModelRun are still eligible and match the stored input hash/key.
4. The exact budget reservation exists for the same investigation and operation key, remains RESERVED, has the exact amount digest, and has sufficient remaining allowance for this physical request.
5. The exact prepared request artifact still hashes to request_sha256; provider/route, normalizer/schema versions, owner, occurrence ID, and identity hash match the PREPARED row.
6. The unique effect key has not already been consumed or resolved to an incompatible result.
7. Any linked Retrohunt slot or acquisition attempt is in the exact pre-submit state, bound to the same subject/rule/corpus/sample/version, and is transitioned with the decision.

A stale version, status mismatch, duplicate, missing row, stale approval, malformed binding, failed CAS, timeout, deadlock, or unavailable dependency returns no permit. A failed consume transaction has no egress. Retry the database transaction only before commit; never retry a consumed physical request.

If a late response arrives after a human reconciliation already made the dispatch terminal, append a conflicting late-response event and preserve both evidence references for review; do not overwrite the human decision or create another dispatch. A late response may resolve NEEDS_REVIEW only while that state remains current by CAS.

### Lock order — PROPOSED

Use one lock order in consume, denial resolution, success settlement, and reconciliation:

1. Investigation envelope row, the same row locked by revoke and envelope CAS.
2. Mutable investigation stop/cancel row, if separate from the envelope.
3. ToolInvocation row, when the operator path owns the request.
4. Exact InvestigationBudgetReservation row, then its amount rows in sorted dimension order.
5. ExternalEffectDispatch row.
6. Linked operation attempt rows: Bridge conversation turn, then ModelRun; or Retrohunt slot; or acquisition attempt. When more than one same-kind row is needed, sort by primary key.

Bridge owners omit step 3. Do not acquire an earlier-ranked row after a later-ranked one. Revocation locks only the envelope and commits promptly. A READ COMMITTED transaction that waited on the envelope lock must read the latest committed version; serialization errors or stale snapshots fail closed. CAS/version checks remain in addition to row locks.

### Revocation race contract — PROPOSED

- If revoke commits first, consume waits for the envelope lock, reloads the revoked/new version, and denies. No permit is created and the fake transport count remains zero.
- If consume commits first, that one physical request is authorized even if revoke commits before the process reaches the socket. This is the linearization point; PostgreSQL cannot retract the permit or atomically coordinate an HTTP departure.
- Every later page, retry, redirect, correction, or other request needs a separate consume and therefore observes the revocation. No logical Bridge session is grandfathered in.
- If consume commit status is unknown to its caller, it must not send or reconstruct a permit. The durable row is conservatively NEEDS_REVIEW even if no bytes actually left.

### Confidentiality and provenance — PROPOSED

Store complete authorized request bytes or a content-addressed immutable request artifact with SHA-256, byte count, MIME type, and version reference. Do not impose a new prompt/evidence ceiling or silently truncate. The transport sends those exact bytes. Provider credentials are injected separately and excluded from identity, artifacts, logs, traces, and errors. Any evidence omission is persisted with its reason and remains visible.

Store responses as versioned artifacts and observations with their source SHA, provider/transport limits, dispatch ID, invocation ID, and Bridge attempt ID. Model/provider text stays untrusted data. Do not redact allowed CTI evidence merely to make a request fit; if transport rejects size, report that outcome with its reason and preserve the request evidence reference.

## 4. Example from real consumers

### Operator VT report — PROPOSED

~~~python
# The operator service has already created/loaded a durable invocation and reservation.
raw = {"file_hash": client_hash}
prepared = vt_preparer.file_report(raw, occurrence_id=None)
outcome = await external_effects.dispatch(
    owner=EffectOwner.for_invocation(invocation, reservation),
    prepared=prepared,
    transport=virus_total_file_report_transport,
)
# The adapter receives the exact prepared request and a mandatory consumed permit.
~~~

The VT preparer applies the operation-specific normalization before persistence. The same immutable request is used for identity, permit binding, and the HTTP body. A later page or explicit refresh calls dispatch separately. If another invocation asks for the same terminal reusable read with no occurrence ID, it links the existing observations and charges zero additional external usage. A caller that means “fetch fresh now” supplies a new occurrence ID.

### Bridge correction — PROPOSED

~~~python
turn = await model_conversations.persist_attempt(
    prompt=rendered_prompt,
    idempotency_key=attempt_key,
    # creates ModelConversationTurn and ModelRun in NOT_SUBMITTED state
)
prepared = bridge_preparer.model_submission(turn)
outcome = await external_effects.dispatch(
    owner=EffectOwner.for_bridge(turn, model_run, turn_budget_reservation),
    prepared=prepared,
    transport=model_conversation_turn_transport,
)
~~~

A corrective prompt or pre-submission retry creates a new persisted attempt, a new occurrence ID, and a new dispatch decision. It cannot inherit the first request’s permit. The existing BRIDGE_TURNS reservation may cover the logical turn’s bounded requests, but each physical submit gets an independent decision and each measured usage item is attached once to that reservation.

## 5. Responsibilities hidden in the implementation

ExternalEffectCoordinator hides:

- Operation-specific normalizer/version lookup and exact request serialization.
- Canonical identity generation, explicit occurrence semantics, deduplication and completed-result reuse.
- Immutable request artifact storage/verification and response artifact provenance.
- Fresh G2/provider/TLP/disclosure/approval checks at consume time.
- The PostgreSQL transaction, ordered row locks, version/CAS checks, unique-claim handling, and Bridge/Retrohunt/acquisition attempt coordination.
- Minting and one-use checking of a short-lived in-process permit after the durable commit.
- Dispatch outcome classification, no-egress proof validation, ambiguity, late response and human reconciliation.
- Usage aggregation and one-time reservation settlement across pages/retries in one logical invocation.
- Safe operational diagnostics without request evidence or secrets.
- Append-only effect transition history and result lineage.

The implementation is deep only if all raw outbound adapters use this one seam and the module owns these rules. A pass-through that merely calls existing adapters would have little depth.

## 6. Responsibilities removed from callers

Callers no longer:

- Decide that a prior read/check is fresh enough to send.
- Interpret RUNNING or a reserved budget as permission to dispatch.
- Hand adapters caller-selected effect classifications or authorization booleans.
- Reimplement one-use identity, CAS, deduplication, or “unknown means review.”
- Decide which provider exceptions prove non-submission.
- Release reservations based only on a local exception or a missing response.
- Reuse observations by copying them without lineage or budget linkage.
- Call a provider port or HTTP/model gateway directly.
- Assume one Bridge turn reservation authorizes every retry/correction.
- Normalize one object for identity and later serialize a different object for transport.

Tool and Bridge orchestrators still choose the operation, purpose, evidence refs, and a stable occurrence for intended fresh work. They do not make the durable send decision.

## 7. Dependencies and adapters

| Dependency | Category | Module treatment |
|---|---|---|
| PostgreSQL | Locally substitutable but real ordering oracle | Production adapter implements row locks, uniqueness, CAS, append-only transition rows, and outcome settlement. Only disposable PostgreSQL with independent sessions proves races. In-memory tests remain useful for pure transitions only. |
| Request/response artifact store | Owned external persistence | Inject the existing versioned artifact adapter. PostgreSQL references its immutable SHA/version. Artifact writes before DB reference may orphan; they cannot authorize egress. |
| VT, Shodan, Retrohunt providers | True external | Provider HTTP adapters are send-only and require the permit; tests inject fake transports. No test calls a live provider. |
| Model/Bridge transport | True external | The model-conversation adapter requires the same permit. Scripted transport proves application outcomes, not provider receipt. |
| Normalization and identity | In-process | Pure, versioned functions; test every family with exact expected request bytes and digest. |
| Tool registry | In-process catalog | Registration names effect family/provider/normalizer, but the coordinator derives policy from the current catalog and actual transport operation. Registry mutability is not a permit. |

Each concrete transport is a real adapter at the seam because production transport and scripted/fake transport both satisfy PreparedTransport. Do not add a port around a single implementation without the test adapter.

## 8. Persistent source of truth and decision point

### Proposed persisted model

Add an additive ExternalEffectDispatch row per physical request:

- UUID primary key; investigation ID; state; state version; created/updated timestamps.
- Semantic identity SHA-256, effect key, provider/operation/route, effect family, tool schema version, normalizer ID/version, occurrence token, and exact request SHA-256.
- Immutable request artifact ID/version, byte count and media type.
- Exact envelope version, owner version, owner kind, and reservation operation key/amount digest.
- Exactly one owner form: ToolInvocation, or Bridge turn plus ModelRun. Optional linked Retrohunt slot/acquisition attempt IDs preserve their specialized provenance.
- Consumed time; outcome class; response artifact SHA/reference; observation IDs; safe error code; reconciliation actor/reason/evidence reference.
- No credential, signed URL, or unredacted transport header.

Add an append-only ExternalEffectEvent row keyed by dispatch ID and transition sequence. Add EffectResultUse links for cross-invocation reuse, each pointing to the source dispatch, original invocation, canonical observations and consumer invocation. Add one budget settlement per logical reservation and per-dispatch measured usage rows, with uniqueness to prevent duplicate accounting.

### Unique keys and constraints — PROPOSED

- Primary key on dispatch ID.
- UNIQUE(investigation_id, effect_key), where effect_key includes semantic identity and occurrence token but excludes client operation_key.
- UNIQUE(dispatch_id, transition_sequence) on append-only events.
- Composite FK from dispatch investigation/reservation key to investigation_budget_reservations(investigation_id, operation_key).
- Owner CHECK requires either one ToolInvocation owner or a Bridge turn plus its ModelRun, never both/neither. FKs and application guards verify all owner rows belong to the same investigation/turn.
- For Bridge, UNIQUE(bridge_turn_id, attempt/occurrence ID); for specialized children, UNIQUE(dispatch_id, child attempt/slot link) as appropriate.
- UNIQUE(source_dispatch_id, consumer_invocation_id, source_observation_id) for result-use lineage; CHECK reused budget delta is zero.
- UNIQUE(investigation_id, reservation_operation_key) on settlement; UNIQUE(dispatch_id, budget dimension) on measured usage.
- CHECK state values and legal event types; append-only event trigger/repository refuses update/delete.
- Preserve the existing unique ToolInvocation operation_key constraint. It remains client replay identity, not effect identity.

### Point of decision

The durable consume transaction commit is the single decision point, before transport I/O. It locks envelope → optional stop/cancel row → invocation → reservation and amounts → dispatch decision → operation attempt. It verifies the guards and atomically changes PREPARED to CONSUMED, links the exact owner/reservation, and moves a Bridge ModelRun from NOT_SUBMITTED to SUBMISSION_IN_PROGRESS. Only a commit-confirmed transaction returns a permit. No SQL lock is held through transport.

PREPARED is not permission. CONSUMED is permission for exactly one matching physical request, even if revocation commits before that request physically leaves the process. PostgreSQL cannot make HTTP departure atomic. A later retry/page/correction has to consume again against current authorization.

## 9. Positive, negative, crash, replay, and PostgreSQL race oracles

These are proposed acceptance oracles, not tests run for this artifact. Use fake transports with call counters and barriers. Use two independent PostgreSQL sessions, not two tasks sharing one session. Test actual PostgreSQL transaction/locking semantics.

### Positive

- Approved G2/provider/TLP/disclosure plus RUNNING invocation, RESERVED exact reservation, valid prepared bytes, and eligible Bridge attempt yields one committed consume and one matching fake send.
- The body/path bytes received by the fake hash to the persisted request_sha256. A changed byte, route, schema, normalizer version, owner, envelope version, or occurrence is denied with zero calls.
- Reusing a successful eligible read under a second invocation creates lineage links to the original invocation/observations and a zero-delta budget settlement for the consumer; provider call count stays one.

### Negative and direct-entry

- Parameterize absent/revoked/stale G2, disallowed provider, TLP RED, do_not_submit, external_llm_allowed=false, sample restriction, missing approval, stop/cancel, expired version, wrong reservation, wrong owner state, unknown normalizer, bad digest, copied permit, and second use. Each yields zero calls.
- Call each raw VT, Shodan, Retrohunt submit, acquisition download, advisor model, and Bridge transport entry with no permit. Each rejects before HTTP/provider send.
- An adapter-returned “not submitted” boolean or proof is ignored. Only a typed proof issued by the trusted transport/framework can establish CONFIRMED_NOT_SENT.

### PostgreSQL barrier races

1. **Replay changes RUNNING before consume.** Session A commits invocation RUNNING, RESERVED budget, and PREPARED decision, then waits at a barrier before taking consume locks. Session B follows the shared order (envelope, invocation, reservation) and commits NEEDS_REVIEW. Release A. A consume reloads under lock, rejects, stores no CONSUMED decision, releases only the unconsumed reservation, and fake call count is zero.
2. **Revocation wins.** Session A reaches a barrier immediately before consume. Session B locks the envelope, commits revoked/new version, then releases A. A locks/reloads envelope, denies, and fake call count is zero.
3. **Consume wins.** Session A locks all consume rows, commits CONSUMED, then waits at a barrier before fake send. Session B revokes and commits. Release A: exactly that already-consumed physical request may send once. Attempt a second page/retry/correction; its new consume sees revocation and has zero additional calls.
4. **Concurrent duplicate effect claim.** A and B use different client operation keys but identical normalized request plus the same reuse occurrence. Barrier both before unique insert. PostgreSQL UNIQUE(investigation_id, effect_key) yields one dispatch/consume; the loser reads the winner and either waits/reuses a terminal success or returns in-progress/review. Total fake sends = one.
5. **Intentional identical refresh.** Give B a new explicit occurrence ID. It gets a distinct effect key and may send only if a second consume independently passes current policy. This proves the exception is explicit.
6. **Consume commit acknowledgement lost.** Commit CONSUMED in session A but suppress its result before returning a permit. Restart/replay in session B. B must not reconstruct a permit or send. Mark the row NEEDS_REVIEW; total sends remain zero.
7. **Crash after consume / after departure.** Stop worker A after consume commit, before fake send; then separately let the fake accept once and drop the response. Recovery marks NEEDS_REVIEW, retains reservation, and replay sends zero additional requests. The second scenario has one accepted fake request total.
8. **Bridge attempt coherence.** Session A prepares a persisted ModelConversationTurn and ModelRun with exact input hash and NOT_SUBMITTED. Session B changes the owner/run to review or consumes a different decision. A must fail its joined consume guard and make zero transport calls. Then test a successful consume locks/updates the matching attempt with the effect row in one transaction.
9. **Reservation settlement/reuse.** Race two finishers for one logical reservation after multiple physical dispatch rows. A single settlement row wins; measured per-dispatch usage is included once. Reusing the completed result adds no provider usage and does not mutate source observation ownership.
10. **Deadlock order.** Run consume and revoke/settle/reconcile barriers repeatedly with independent sessions. Every path follows the documented lock order; deadlock/serialization failure returns no permit and causes zero send for that failed transaction.

The existing in-memory admission test is not a substitute for these database oracles. A fake proves application call count, not provider receipt or provider-side idempotency.

## 10. Compatibility, additive migration, and rollback

### Migration — PROPOSED

- Add a revision after the current Alembic head. Never edit 0001_baseline or rewrite 0002_c2_experiments.
- Add the dispatch/event/result-use/usage/settlement tables, checks, FKs, unique indexes, and only the minimum link columns required. Reuse ToolInvocation RUNNING/NEEDS_REVIEW and existing reservation states. Reuse existing ModelSubmissionState values where possible; avoid adding status values to existing constrained columns.
- The new dispatch state CHECK belongs to the new table. If implementation needs a new status in an existing invocation, turn, run, or reservation column, extend that CHECK in the same additive revision and test both old and new values.
- Follow ADR-0007 preflight discipline for installations stamped at the existing head, including any supported manual schema state. Create the canonical new objects only when absent; if any expected object exists, validate the complete shape before changing anything. Partial/non-conforming state fails before DDL. Do not use IF NOT EXISTS to hide drift.
- Do not backfill old RUNNING invocations with effect identities: raw parameters do not prove normalized transport bytes. On cutover, unresolved legacy RUNNING external work becomes NEEDS_REVIEW and is non-dispatchable. Historical succeeded invocations remain readable with their original refs.
- Roll out the mandatory permit requirement to every outbound adapter before enabling new sends. A mixed fleet with old direct-send code cannot satisfy this contract. During cutover, old outbound workers must be stopped or fenced; this deployment fact is an operational prerequisite, not a migration guarantee.

### Rollback — PROPOSED

A code rollback must preserve the permit gate or disable outbound effects. Restoring old code that can call raw transports would reintroduce bypass. Keep additive tables/columns in place during code rollback.

Downgrade is permitted only on a disposable database and only if all effect, event, result-use, settlement, and usage tables are empty and no new link is in use. Refuse downgrade otherwise; preserve canonical effect history. Live rollback is a forward-fix or isolated restore from a verified backup. This follows ADR-0007’s non-destructive rule for versioned schema.

## 11. Deletion test

Delete ExternalEffectCoordinator and its persisted contract. Complexity immediately reappears in each operator adapter, each Bridge/advisor/model path, Retrohunt, acquisition, the invocation service, and the repositories:

- Each caller must independently normalize and version exact requests, compute identities, persist payload hashes, lock/reload governance and owner state, check invocation/reservation, arbitrate duplicates, consume once, handle revocation races, classify uncertainty, preserve result provenance, and settle budget.
- Each adapter must independently invent a permit check and prove that the transport sends the same bytes that were persisted.
- Each recovery path must separately decide whether an absent response means retry, review, or release.
- Tests must rebuild all two-session PostgreSQL barriers once per path, and can still miss an unguarded raw transport.

Those rules spread across at least the operator dispatcher, VT/Shodan adapters, acquisition, Retrohunt, advisor, BridgeInvestigator, ModelConversationService, ModelGateway, and their repositories. Complexity has not vanished; it has been duplicated. The module earns its depth if callers learn one interface and maintainers verify one transaction/outcome implementation, with provider-specific normalization hidden behind internal adapters.

## 12. Before/after comparison with verifiable evidence

| Concern | Before — VERIFIED at study point | After — PROPOSED |
|---|---|---|
| Decision point | RUNNING/reservation commit precedes a read callback; no atomic send decision. investigation_service.py:1110–1237; M3 §1.6 | One PREPARED → CONSUMED transaction commit precedes every physical request. |
| Revocation race | Revoke locks/version-checks envelope; callback returns before adapter, so check and send are separate. M3 §1.6 and durable decision point | Revoke and consume contend on the same envelope row. Revoke-first blocks; consume-first authorizes only that physical request. |
| Replay race | RUNNING can become NEEDS_REVIEW while original worker’s callback omits invocation re-read. operator_tool_invocation.py:863–878; M3 §1.8 | Consume locks/reloads invocation and requires RUNNING at the expected version. |
| Effect identity | operation_key is client replay identity; normalized_parameters currently receive raw arguments; no common versioned effect identity. M3 §1.2–3 | Separate semantic identity and occurrence-keyed dispatch identity bind exact request bytes and normalizer/schema versions. |
| Bridge | Durable turn/run exists before gateway execution but is not atomic with consume/revocation. M3 §1.7, §1.10 | Exact turn/run is locked and transitioned with the decision row; each physical submit consumes separately. |
| Ambiguity | Separate recovery machines; external uncertainty generally reaches NEEDS_REVIEW, but there is no common consumed-decision record. M1-A §e; M3 §1.10 | CONSUMED without proof resolves only to result, trusted no-send, or NEEDS_REVIEW; no automatic resubmit. |
| Reuse | Exact replay and acquisition have local reuse rules; generic cross-invocation lineage/budget policy is unspecified. M3 §1.11 | EffectResultUse preserves original observation/invocation; consumer adds zero external charge. |
| Direct entry | Optional callback and callable lower adapters permit bypass paths. M1-A §d; M2-A missing oracle #10 | Every raw adapter requires a non-optional one-use permit; direct entry without it fails closed. |

## 13. Risks, UNKNOWNs, and limits

### Risks

- The consume commit cannot prove whether HTTP bytes physically left after the commit. A process can die between commit and send, during send, or after provider acceptance. All such cases without exact proof stay NEEDS_REVIEW. This is the unavoidable uncertainty cut, not a reason to resubmit.
- Application permits do not prove that a proxy, SDK, browser bridge, or provider retried internally. Hidden retries and redirects must be disabled or exposed as physical sends to the same seam.
- Large authorized evidence requires complete artifact storage and exact byte replay. The artifact adapter’s immutability, access control, and availability must be verified; never silently truncate to make persistence easier.
- PostgreSQL locks serialize only the state included in the transaction. Any mutable stop, sample restriction, approval, or provider policy used to deny egress must be versioned/locked at consume.
- A result can be reused only inside its original investigation and exact semantic identity; reuse must not be sent to a model/provider unless that new egress independently passes current disclosure policy.
- A consumed permit may be used after a revocation commits if consume committed first. This is the specified linearization rule; revocation cannot retract a decision already committed.

### UNKNOWN

- Provider-side idempotency and canonicalization for VT, Shodan, Retrohunt, acquisition, or any model provider.
- Whether production HTTP stacks, proxies, browser sessions, or SDKs make hidden retries, redirects, or multiple sends per method call.
- Whether every runtime composition path for advisor, acquisition, Retrohunt, and Bridge can be changed to require the permit without a secondary raw-send entry.
- Whether the existing model-conversation transport can expose every physical submission boundary, including any UI/session behavior, to the coordinator.
- Which current deployments carry manual schema objects beyond those documented in ADR-0007; no database was inspected.
- Whether a response artifact and all observations can be committed atomically with current repository/UOW boundaries; the proposed implementation may need a new focused repository adapter.

### Seven BLOCKED_DESIGN decisions — explicit resolution

1. **Prepared-effect interface:** each operation family has a typed, immutable PreparedEffect; its normalizer/schema version and complete application-level method/route/query/body bytes are persisted and SHA-256 bound to the request sent. Transport uses those bytes verbatim.
2. **Cross-adapter identity and intentional repeats:** effect identity is a versioned semantic request hash, distinct from operation_key. effect_key adds a stable occurrence token. No occurrence is result-reuse-only for operations explicitly marked reusable; a fresh poll/refresh, retry, correction, submit, or acquisition gets a new occurrence.
3. **Mandatory seam/direct entry:** ExternalEffectCoordinator is the only sender seam for registry tools, Bridge, advisor, acquisition, and Retrohunt. Each raw transport requires a coordinator-minted one-use permit and exact request binding; no owner/reservation means no permit.
4. **Consume model and locks:** an additive dispatch row coordinates exact envelope version, stop state, RUNNING invocation or Bridge turn/run, RESERVED budget row/amounts, prepared identity/version, and any specialized attempt. Lock order is envelope → stop/cancel → invocation → reservation/amounts → decision → operation attempt; commit CONSUMED before transport.
5. **Permit scope/revocation:** one permit authorizes one physical request only. Page, retry, redirect, fan-out, and corrective request each require new consume under current authorization. Revocation before consume denies; revocation after consume permits only the already-consumed physical request.
6. **Crash, ambiguity, reuse and budget:** pre-consume crash is provably no-egress; after-consume uncertainty is NEEDS_REVIEW and never resubmitted. Only trusted no-send proof or explicit human evidence may release a reservation. Late correlated responses resolve the same decision. Result-use links preserve source observation/invocation and charge the external budget once.
7. **Migration/statuses:** add dispatch/event/use/usage tables and constraints after current head; reuse existing invocation, reservation, Bridge turn/run states where possible. Extend existing CHECKs only if an existing status must change. Never backfill exact identities from raw parameters. Downgrade only when new tables are empty, and refuse destructive rollback.

### Conclusion

**PROPOSED verdict:** the safe seam is a single-physical-request ExternalEffectCoordinator backed by PostgreSQL. Its committed CONSUMED transition is the only permission to send. It closes the RUNNING/replay and revocation/read-check gaps at the database linearization point while leaving transport outside the lock. The hardest unresolved risk is proving one application-level dispatch equals one physical external request through every production adapter and runtime; provider idempotency remains UNKNOWN.

