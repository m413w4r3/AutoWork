# D6-S3-B — Flexibility and extension

**Design point:** aa0cef3ee07525ca77d6b1358252ebf5735a3dde on mission03/architecture-design  
**Scope:** investigation-scoped external effects only. This is a design artifact; no tests or providers were run.  
**Labels:** VERIFIED means observed in the current source at the design point; INFERENCE is a consequence/design judgment; UNKNOWN needs an implementation-time oracle or source trace.

## 1. Seam actuel et seam proposé

**VERIFIED — current seam.** Ordinary operator tools pass through OperatorToolInvocationService → InvestigationService.execute_tool_call → RegistryToolDispatcher → registered adapter → provider port/transport. Main Bridge turns bypass the tool registry and enter BridgeInvestigator.turn through a separate path. Retrohunt and acquisition each have their own durable state machine. advisor.consult is registered as EXTERNAL_READ although it submits to the model path. The current dispatcher callback is optional, normalization happens inside provider adapters, and no common durable effect identity or atomic consume exists (M1A_EXTERNAL_EFFECTS.md; M3_S3_DIAGNOSIS.md §§1–8; source: investigation_service.py:1007-1237, investigation_tools.py:412-427,779-784, bridge_investigator.py:1172-1291).

**Proposed seam.** Add one application module, OutboundEffectGate, between investigation orchestration and every in-scope physical outbound request. Its small public interface is prepare(...) -> EffectRef and dispatch(effect_ref) -> EffectOutcome. Registered VT/Shodan/Retrohunt/acquisition/advisor adapters and the main Bridge transport become operation-specific adapters behind that seam. The gate owns durable identity, the PostgreSQL consume decision, permit issuance, dispatch outcome state and replay/review behavior. An adapter owns operation normalization and exact provider encoding; it cannot make an outbound request without the gate-issued permit.

This is one real seam because production HTTP/model transports and local scripted/fake transports are already distinct adapters. The gate does not become a global policy engine: it coordinates a decision for one prepared physical request and delegates provider-specific meaning to the operation adapter. Local computations stay on their existing path.

## 2. Contrat public (types et pseudo-code)

### Public interface

    @dataclass(frozen=True)
    class EffectIntent:
        owner: ToolInvocationOwner | BridgeAttemptOwner
        family: Literal["vt_read", "shodan_read", "retrohunt_read",
                        "retrohunt_submit", "vt_acquire", "model_submit"]
        operation: str                 # registered operation name, never client supplied
        schema_version: str
        occurrence_id: UUID | None      # None = semantic claim; set only for intentional occurrence
        parent_effect_id: UUID | None   # page/retry/correction lineage
        step: Step                      # initial | retry(n) | correction(n) | page(n,cursor_sha)
        arguments: Mapping[str, JSON]   # validated input; adapter normalizes it

    @dataclass(frozen=True)
    class EffectRef:
        dispatch_id: UUID               # durable claim / dispatch row
        claimant_id: UUID               # this invocation/attempt's FK link; not a permit

    async def prepare(intent: EffectIntent) -> EffectRef:
        """Normalize, resolve/create the unique claim, and link this claimant."""

    async def dispatch(effect: EffectRef) -> EffectOutcome:
        """Reuse an exact completed result, or consume once and call its adapter."""

ToolInvocationOwner carries the persisted invocation id and exact budget reservation key. BridgeAttemptOwner carries the persisted conversation-turn id, ModelRun id, logical turn id and reservation key. For Retrohunt and acquisition, the owner additionally links the Retrohunt reservation or sample acquisition attempt; advisor links its invocation to the persisted model attempt. These are typed owner variants with real foreign keys, not free-form metadata. prepare resolves the semantic claim under its unique index and inserts a claimant link; it does not transfer the original dispatch owner. dispatch on a completed exact claim returns a source-linked reuse outcome without egress; dispatch on a consumed or ambiguous claim never sends. Only the claimant that owns the prepared dispatch may consume it. No caller passes an effect class, provider, endpoint, permit, normalized payload digest, or “admitted” boolean.

An internal EffectAdapter has a registered (family, operation, schema_version, normalizer_version, wire_contract_version), a pure prepare_request(intent) -> RequestDescriptor, and send(descriptor, ConsumedPermit) -> PhysicalOutcome. Only OutboundEffectGate invokes send. The permit is an opaque framework value tied to one committed dispatch row, one payload digest and one physical call; it is invalid for any other request. The production adapter checks it at its last request-building seam. Raw investigation transport entry without a permit returns a typed refusal before HTTP/model I/O. A missing or changed adapter/normalizer version fails closed.

### Prepared request and durable identity

The adapter returns one immutable RequestDescriptor per **physical** request:

    @dataclass(frozen=True)
    class RequestDescriptor:
        family: str
        operation: str
        normalizer_version: str
        wire_contract_version: str
        canonical_parameters: bytes       # versioned canonical JSON
        provider_route: str               # allowlisted route id, no credentials
        method: str
        canonical_target: str             # path/query; secret-bearing parts redacted
        body_ref: ArtifactRef | None      # immutable bytes, SHA-256, length, media type
        wire_digest: str                  # SHA-256 of exact canonical request bytes
        dynamic_secret_digest: str | None # verifies e.g. signed target without storing it
        request_ordinal: int

Persist canonical_parameters or a versioned immutable artifact reference and its SHA-256. Persist exact non-secret request bytes (or an immutable payload artifact) plus the wire digest. Never put provider credentials, AutoWork credentials, API keys, passwords, signed URLs, or raw auth headers in identity, descriptor, trace, or log. If a request value is secret-bearing and arrives only at runtime (for example a signed download target), persist the approved origin/route and a digest of the full ephemeral value; compare the in-memory value immediately before send and discard it afterward. This proves equality without retaining or exposing the secret.

effect_identity = SHA256(canonical_json({identity_version, investigation_id, family, operation, normalizer_version, wire_contract_version, route_id, canonical_parameter_digest})). It is distinct from the client operation_key. By default occurrence_id is absent, so one semantic request per investigation has one durable claim regardless of client idempotency key or invocation id. Enforce this with a partial unique index on (investigation_id, effect_identity) for claims that have not been released. SUCCEEDED and NEEDS_REVIEW keep the claim: a completed result can be reused and an ambiguous result cannot be sent again. Only framework-proven DENIED-before-consume or NOT_SENT releases that exact claim and reservation. An explicit occurrence_id adds a separate unique claim (investigation_id, effect_identity, occurrence_id); only an operation workflow may request it for an intentional identical poll/refresh, a proven pre-send retry, or a distinct page/attempt. A generic new tool invocation does not mint an occurrence merely because it has a new client key. A replay resolves the same claim. This is an AutoWork claim only; it is not a provider idempotency key and makes no claim about provider-side deduplication. Existing provider keys may still be sent where the current adapter requires them, but they do not prove receipt or safe retry.

### Variant / consumer matrix

| Consumer and current path | Exact prepared operation contract | Scope / occurrence | State owner checked at consume |
|---|---|---|---|
| vt.file_report, vt.object_report, vt.relationships, vt.intelligence_search, vt.behaviour, optional vt.behaviour_pcap (investigation_vt_tools.py:1571-1705) | Each tool has a versioned normalizer for its hash/object/query/cursor/limit fields and route contract. The descriptor records normalized target/query, selected allowlisted VT route, request body if any, cursor, and exact wire digest. A VT route fallback is its own request. | One consume for each HTTP request. Current max_pages / page-size bounds remain the plan’s existing bound; page n+1 gets its own descriptor, cursor digest, parent dispatch id and explicit occurrence. Exact repeated default reads resolve the existing semantic claim; an intentional refresh supplies a workflow occurrence. No page loop hides provider calls. | Invocation RUNNING, envelope scope, exact budget reservation; current VT capability/route. |
| shodan.host, shodan.search, shodan.domain, shodan.count (investigation_shodan_tools.py:138-340,1175-1222) | Normalize IP/domain/query and exact page/count operation; preserve the currently allowed passive route. search page is the explicit page input (schema range 1–100); one current request is one descriptor. | One consume per HTTP request. A user-requested next page is a new occurrence; there is no implicit page expansion. | Invocation RUNNING, envelope/provider scope, Shodan credit reservation and capability. |
| vt.retrohunt.submit (investigation_retrohunt_tools.py:81-181, retrohunt.py:108-239) | Bind exact approved rule version id and source SHA-256, corpus id and SHA-256, normalized parameters, subject and slot reservation. Encode the current POST body exactly. The consume reloads and verifies the human approval/test evidence for that exact version. | One consume for the POST. A pre-submission retry requires framework proof of no request departure and a new attempt occurrence. An uncertain POST is review-only. | Tool invocation, envelope, exact generic reservation, Retrohunt slot and submission token/state, exact approval/version. |
| vt.retrohunt.status | Bind investigation, Retrohunt reservation id and provider job id; exact status route. | Each intentional poll supplies a new explicit read occurrence; same default claim replays/reuses its result. One consume per GET. No automatic retry after an ambiguous consumed request. | Invocation, envelope/provider scope, reservation/job association. |
| vt.retrohunt.results | First, any status GET made by the existing results() flow is a separate status descriptor. Then each matching-files GET binds job id, page ordinal, input cursor digest, limit and exact query. Current max_result_pages and result limits remain explicit; each page is separately consumed. | One consume per status or results page. Page occurrence is explicit and linked to its parent. Preserve candidate-hit order, duplicate checks, cursor and raw-page provenance. | Invocation, envelope/provider scope, same Retrohunt job/reservation. |
| vt.download_sample (investigation_acquisition_tools.py:495-540,656-681; sample_acquisition.py:282-447) | Normalize requested hash and bind investigation/subject, source hit association, purpose, evidence refs and route policy. Each direct download, ticket request, signed-target request, redirect hop or configured fallback is its own request descriptor. Streamed bytes retain SHA-256 and quarantine evidence. | One consume per physical request/hop. Follow-up route is prepared from the prior response, with parent dispatch link and newly checked envelope. A redirect is not silently covered by the first request. Verified same-subject quarantine reuse remains local and creates no outbound effect. | Invocation and envelope reservation plus the sample acquisition attempt and its VT-read / hit-acquisition budget reservation. Reuse is checked before creating an outbound dispatch. |
| advisor.consult (investigation_advisor_tools.py:164-210,3329-3410) | Normalize consultation type, trimmed question, evidence UUIDs, approved editorial units, exact rendered message artifact/hash, prompt version and model route. Classify its real operation as model submission (EXTERNAL_SUBMIT), not a provider read. Keep all currently authorized evidence observable; no new byte ceiling or silent truncation. | One consume per actual model request. The normal execute path creates one chunk; any existing internal batch child is a separate explicit occurrence. | Tool invocation, envelope plus external_llm_allowed snapshot/version, exact Bridge/ModelRun attempt. |
| Main Bridge turn (InvestigationService.run → BridgeInvestigator.turn; investigation_service.py:523-672, bridge_investigator.py:1172-1445) | Bind exact persisted conversation turn / ModelRun, provider/model route, prompt version, context hash, rendered messages artifact/hash and generation settings. | Each initial request, confirmed pre-submission retry, and corrective request is its own consume. The existing max of 3 pre-submission attempts and up to 2 corrective attempts is preserved; changed correction context always creates a new descriptor. No implicit shared permit. | Current envelope and disclosure, logical Bridge budget reservation, turn and exact ModelRun attempt still eligible to submit. |
| sample.analyze, sample.set_qualification, advisor.reparse, and other LOCAL_COMPUTE / LOCAL_READ operations | No request descriptor and no outbound permit. Their existing local ports and evidence rules remain. | Outside this gate. | Their existing local invariants. |

The source confirms VT has page loops and route selection, Retrohunt results has a cursor/page loop, Shodan search takes an explicit page, acquisition can make a direct request plus ticket/redirect/fallback requests, and Bridge has retry/correction loops (investigation_vt_tools.py:869-977,1008-1154,1187-1240; virustotal_retrohunt_client.py:143-209; virustotal.py:440-550; bridge_investigator.py:1221-1433). The gate’s unit is therefore a physical request, not a whole adapter execution.

### The seven BLOCKED_DESIGN decisions from M3

1. **Prepared effect:** each registration names a concrete, versioned normalizer and wire contract. It returns one normalized physical request; exact bytes or immutable artifact refs and SHA-256 are persisted. Dynamic secret-bearing values are checked by digest immediately before send and are never stored in descriptors or logs.
2. **Durable effect identity:** identity is the canonical semantic request digest independent of operation_key. With no explicit occurrence, a partial unique claim is shared across distinct client keys and invocation ids. An operation-specific explicit occurrence permits an intentional identical poll/refresh or a proven pre-send retry. Same claim replays/reuses the existing outcome; a caller cannot create another physical request by changing its client key.
3. **Mandatory outbound seam:** all scoped investigation adapters and model transports require ConsumedPermit; only OutboundEffectGate can issue it. Direct VT/Shodan/Retrohunt/Bridge/acquisition calls without one fail closed. advisor.consult is registered as a submission. Local operations never request a permit.
4. **Consume row and lock order:** one additive investigation_effect_dispatches row per semantic claim / explicit occurrence has a constrained state, immutable identity/descriptor digests, reservation key, envelope version and result refs. A companion effect_dispatch_claimants row links each owning ToolInvocation or Bridge attempt and its reservation to the dispatch, preserving source lineage if the result is reused. Typed foreign keys and check constraints enforce the owner shape. In one short transaction lock in this order: investigation envelope; tool invocation **or** Bridge turn and ModelRun; exact budget reservation(s); Retrohunt slot, acquisition attempt or exact approval/sample-scope row when present; effect dispatch row; claimant row. Re-read each row under lock, compare versions and policy, transition PREPARED→CONSUMED, commit, then send. All competing writers that touch these rows must use the same order. A uniqueness conflict reloads the winner; it never sends a second call.
5. **Retry/page/correction scope:** exactly one physical request per consume. Each later request is a new prepared row and consume, linked by parent id and stable ordinal/cursor digest. Revocation after consume can’t retract that already-authorized call; it denies every later consume. A Bridge correction is not covered by the original prompt’s consume. Retries require framework-attested no-send proof; ambiguous outcomes stop.
6. **Crash, outcome, reuse and budget:** PREPARED crash is safe to resume after a fresh consume check. Any crash/timeout/cancellation after a successful consume with no proven outcome becomes NEEDS_REVIEW; no auto-resubmit, including a crash after commit but before HTTP. A complete response is archived with a dispatch and claimant lineage link. Measured provider usage is recorded at most once per dispatch; unknown usage stays unknown. A distinct claimant may reuse only an exact completed claim in the same investigation; it receives a source-linked reuse record and zero second provider charge. An in-flight/ambiguous claim is never resent. Existing verified acquisition reuse remains local, zero-download and zero VT-unit, and records its source lineage.
7. **Migration:** retain current ToolInvocation, Bridge, Retrohunt and acquisition statuses. Add a post-head additive dispatch table plus claimant-link table with CHECK-constrained states (PREPARED, CONSUMED, NOT_SENT, SUCCEEDED, NEEDS_REVIEW, DENIED), partial uniqueness for the default semantic claim, uniqueness for explicit occurrences, typed foreign keys and recovery indexes; add effect-dispatch linkage where existing attempt rows need it. Do not alter 0001_baseline or rewrite 0002. Any exact existing constraint/column extension is a new migration based on the inspected current head.

## 3. Invariants, timing, versions, errors, confidentiality

- prepare is deterministic for a registered (operation, schema_version, normalizer_version, wire_contract_version). It does not authorize egress. Canonicalization version, provider operation, target, body ref/hash, cursor/page/retry/correction identity and bounds are immutable after persistence. A missing historical adapter version fails closed; it is never silently re-normalized with new code.
- dispatch re-loads from PostgreSQL. A passed-in object is not trusted as canonical state. It verifies the descriptor hash against persisted bytes and checks the adapter’s exact name and versions. The adapter verifies the actual request digest immediately before network write. Any mismatch means DENIED with zero calls.
- A second claimant for a completed exact claim must pass current local owner/access checks. For model_submit reuse, it must also pass current external_llm_allowed and provider-scope checks; an old model result cannot turn a newly denied consultation into an allowed one. It receives a reuse link to the canonical source invocation and observation, not a fabricated new provider result; its unused reservation is released only with framework proof that this claimant caused no egress. A claimant cannot take over a PREPARED or CONSUMED dispatch owned by another invocation. The original owner must remain eligible at consume; otherwise the no-send claim is released and a later explicit workflow can prepare again.
- The consume transaction compares the exact current envelope version and all relevant approval/disclosure state: provider allowlist, G2 scope, TLP, do_not_submit, external_llm_allowed, sample restrictions, stop/cancel state, human approval, and exact reserved amount/key. It verifies the owner remains RUNNING/submit-eligible and the operation remains registered. A local effect cannot be upgraded to an external one by request data.
- Revocation and consume lock the same envelope row. If revocation commits first, consume is denied and egress count is zero. If consume commits first, that one physical request may proceed after revocation commits; the transaction releases every SQL lock before calling transport. Every next page/retry/correction/hop must consume again and sees the revoked envelope.
- States: PREPARED → CONSUMED → SUCCEEDED for a complete response; CONSUMED → NEEDS_REVIEW for timeout, cancellation after dispatch, uncertain transport error, late response that cannot be safely matched, or worker loss. CONSUMED → NOT_SENT is allowed only for framework-owned proof that no request bytes departed. A denied pre-consume attempt records DENIED. NOT_SENT permits a new child attempt; it does not reset or reuse the consumed row. NEEDS_REVIEW is terminal for automatic dispatch. Reconciliation may append a human decision and attach recovered evidence; it may not resubmit an ambiguous occurrence.
- Typed errors distinguish DENIED_STALE_ENVELOPE, DENIED_POLICY, DENIED_OWNER_STATE, DENIED_RESERVATION, DENIED_DESCRIPTOR, ADAPTER_VERSION_UNAVAILABLE, ALREADY_CONSUMED, PROVEN_NOT_SENT, and NEEDS_REVIEW. Only framework-generated proof can release an exact budget/effect claim. Adapter exceptions and provider error text are not proof of non-execution.
- API callers and durable workers use the same prepare / dispatch interface. A queue carries only EffectRef; no in-memory permit crosses a queue hop. The worker reloads the plan and consumes it immediately before the physical request. Operator tools may invoke it synchronously in the API path; main Bridge and existing durable investigation runs invoke it in the worker path. The gate does not own queueing, pacing, business workflow, or result interpretation.
- Do not assume provider idempotency, request canonicalization, hidden SDK retry behavior, or external receipt. Any SDK/HTTP retry not surfaced as one of these dispatches must be disabled or intercepted by the adapter; until confirmed, hidden transport retry behavior is UNKNOWN and blocks that adapter from production admission.
- Evidence remains canonical in PostgreSQL, versioned artifacts and MinIO/SHA-256 references. Each response page, acquired sample, provider result and model response keeps source, route/version, dispatch id, invocation/attempt id, request digest and limitations. Do not drop authorized raw evidence or add a prompt-size ceiling. Provider/model output remains untrusted data.

## 4. Exemple d'appel depuis un consommateur réel

A VT relationship call currently loops through pages in _RelationshipsAdapter. The revised caller stages one page at a time; it does not gain a provider session permit:

    intent = EffectIntent(
        owner=ToolInvocationOwner(invocation_id=invocation.id,
                                  reservation_key=invocation.operation_key),
        family="vt_read", operation="vt.relationships", schema_version="1",
        occurrence_id=uuid5(invocation.id, "page:0"), parent_effect_id=None,
        step=PageStep(index=0, cursor_sha256=sha256_or_none(input_cursor)),
        arguments={"object_type": object_type, "object_id": object_id,
                   "relationship": relationship, "cursor": input_cursor},
    )
    first = await gate.prepare(intent)
    page = await gate.dispatch(first)  # consume transaction commits before HTTP
    persist_page_evidence(page, invocation_id=invocation.id, dispatch_id=first.dispatch_id)
    if page.next_cursor is not None and not page.exhaustive:
        second = await gate.prepare(next_page_intent(first, page.next_cursor))
        page2 = await gate.dispatch(second)  # fresh envelope/reservation check

If the first page is ambiguous, the caller returns NEEDS_REVIEW and does not prepare page 2. If revocation commits after page 1, page 2 is denied. Existing max_pages, raw page archive and partial-result flags remain visible. For main Bridge, BridgeInvestigator similarly persists the current ModelRun, prepares that exact prompt attempt, then dispatches through the same gate from the durable worker.

## 5. Responsabilités cachées dans l’implementation

OutboundEffectGate owns the deep implementation: adapter/version resolution; canonical serialization and digest checks; claim uniqueness and replay; short consume transaction and lock order; envelope and disclosure reload; reservation and owner-state verification; opaque permit creation; one-request dispatch; no-send proof typing; ambiguity and recovery state; result/usage idempotency; and stable lineage references.

Operation adapters keep only domain-specific normalization and wire construction. VT owns hash/query/cursor/route forms. Shodan owns IP/domain/query/page forms. Retrohunt owns exact reviewed rule/corpus/parameters and result-page cursor. Acquisition owns hash association, ticket/redirect/fallback route, streaming hash verification and quarantine. Advisor and Bridge own rendered model inputs, prompt/context versions and turn/correction construction. These are real operation differences; they are not branches in a generic policy DSL.

Retrohunt and acquisition remain parallel state machines for their domain lifecycle. Retrohunt keeps subject slot limits, human rule authorization, submitted job identity, reconciliation and candidate observations. Acquisition keeps verified same-hash reuse, partial-byte/hash mismatch handling, quarantine, evidence and sample state. The gate wraps each physical egress and locks the corresponding Retrohunt slot or acquisition attempt at consume; it does not replace those state machines. The gate also binds advisor/main Bridge ModelRun attempts rather than replacing model conversation persistence.

## 6. Responsabilités supprimées des appelants

After all in-scope adapters use the gate, callers no longer assemble or persist a supposed normalized effect identity themselves; manually repeat the final envelope/reservation check; treat a read callback as the send decision; decide whether an exception permits retry; or make a provider call after checking only process-local state. The optional pre_dispatch_check protocol and its duplicated “check then call” use are removed after every consumer is migrated.

Callers still select the requested operation, retain authorization context, generate a stable occurrence for a real step, provide domain inputs, interpret results, persist evidence, pace work, and decide when a workflow wants a new page/poll/correction. The gate does not take those workflow decisions away.

## 7. Dependencies et adapters

| Dependency | Category | Adapter strategy / interface consequence |
|---|---|---|
| PostgreSQL | Local-substitutable, owned persistence | Production consume adapter uses real PostgreSQL row locks, constraints and independent sessions. In-memory repositories support fast tests only and cannot prove serialization. |
| VirusTotal, Shodan, Retrohunt, model providers | True external | Keep provider-specific request adapters and scripted/HTTP mock adapters behind the same consumed-request seam. Tests assert exact request bytes/route and zero-call denials; no live provider tests. |
| MinIO/versioned evidence artifacts | Owned persistence | Descriptor/request and response bodies use immutable artifact refs + SHA-256 where appropriate; PostgreSQL owns identity, state and references. |
| Identity and canonicalization | In-process | Pure operation normalizers and canonical request digest functions are tested directly through prepare; their versions are persisted. |
| Existing Retrohunt and acquisition ledgers | Local-substitutable durable domain state | Their current repositories remain authoritative for slots, download attempts, quarantine and reuse. The gate’s PostgreSQL adapter participates in their consume transaction using typed links. |
| Sync API and durable worker | Existing execution modes | Both call the same gate. API owns response delivery; job system owns scheduling/recovery. Only the durable EffectRef crosses a worker boundary. |

No new port is proposed for a hypothetical provider or queue. Production plus local test adapters justify the request/consume seam. An internal fake can replace the transport at the gate’s external interface; tests do not need to reach through the gate into provider SDKs.

## 8. Source de vérité persistante et point de décision

PostgreSQL owns EffectDispatch identity, owner FKs, immutable version/digest pointers, state, consume timestamp, reservation association and reconciliation facts. The investigation envelope and its budget reservation remain canonical there. Existing ToolInvocation, Bridge ModelConversationTurn / ModelRun, Retrohunt reservation and sample acquisition attempt remain the source of their own business state. Versioned artifact storage remains canonical for non-secret request/response bytes and CTI evidence; SHA-256 links them to the PostgreSQL row.

The atomic point is the commit transitioning exactly one effect row PREPARED → CONSUMED while holding the common row-lock order from §2. The commit is before outbound I/O. A process-local lock, prior reservation commit, adapter callback, or transport return is not the decision. No PostgreSQL lock survives into HTTP/model transport. A consume commit with uncertain client acknowledgement is treated as consumed; recovery reads the row and never guesses that it is safe to send.

The specialized budget detail is explicit: Bridge’s existing logical BRIDGE_TURNS reservation is shared by its bounded attempts and charged once per logical turn; each physical request has a distinct decision and measured usage receipt. A VT paginated invocation holds the existing upper bound and accounts each response page once. Acquisition must move its current separate VT-read / hit-budget pre-consumption into a reservation linked to the dispatch; verified reuse exits before any external reservation. Retrohunt binds both the generic invocation reservation (even if zero-valued) and the durable subject slot. No cost is charged twice on result replay.

## 9. Oracles positifs, négatifs, crash, replay, concurrence

These are required implementation oracles; **none were run for this design artifact**.

- **Positive exact request:** allowed G2 VT report and Shodan search each produce one consumed row, one fake transport request equal byte-for-byte to the persisted descriptor (excluding secret material verified by digest), a result artifact SHA-256, observation lineage and one budget receipt. A completed replay returns the same refs with no second request.
- **Normalizer identity:** semantically equivalent raw forms (for example case/whitespace variants normalized by a registered VT or Shodan operation) yield the same effect digest and cannot acquire a second default claim; a changed normalized request yields another digest. Assert the actual fake wire request equals the persisted prepared descriptor for VT, Shodan, Retrohunt, acquisition and advisor.
- **Positive variants:** VT relationship and Retrohunt results pages each create one child dispatch per physical page/cursor. Acquisition direct route → ticket → signed target or redirect is checked per hop; a changed route/payload digest is denied. Bridge initial, retry and correction each link to their own persisted ModelRun/request hash. Advisor consultation is denied when external_llm_allowed is false and allowed only under its exact model context.
- **Direct-entry negative:** call each in-scope production provider/model adapter without a permit, with a stale permit, a permit for another dispatch, or a mismatched payload digest; assert typed denial and zero fake transport calls. Include unregistered Bridge and acquisition entry paths.
- **Policy negative:** TLP, G2 provider scope, do_not_submit, external_llm_allowed, sample restriction, human rule approval/version, stop state, capability, or stale envelope failure before consume produces zero egress. Only framework proof releases the exact reservation. An adapter-reported “not sent” string is insufficient.
- **Owner/reservation negative:** invocation changed from RUNNING to NEEDS_REVIEW after initial reservation; Bridge turn/ModelRun no longer submit-eligible; reservation amount/key changed; Retrohunt slot/token changed; acquisition attempt moved to review. Consume refuses under locks and the fake call count stays zero.
- **Crash:** crash before consume leaves PREPARED and may be reloaded after fresh admission. Crash immediately after consume commit but before fake transport is entered leaves a consumed dispatch that recovery moves to NEEDS_REVIEW; it is not sent on replay. Drop a fake response after one recorded request: state becomes NEEDS_REVIEW, response evidence is not falsely finalized, and replay count remains one.
- **Retry/reconciliation:** a framework-attested pre-send failure becomes NOT_SENT and allows only a new child occurrence. A timeout, cancellation at/after send, lost response, or unknown SDK exception is NEEDS_REVIEW with no automatic retry. Reconciliation can attach a recovered result or analyst ABANDON/NOT_EXECUTED decision, with explicit provenance.
- **Page/revocation:** block between page responses; revoke and commit; next page consume is denied and has no fake call. Block in a fake transport after consume commit; a second independent session revokes successfully while transport is blocked, proving SQL locks were released; that already-consumed physical request may finish, but the next page/attempt is denied.
- **Two-session PostgreSQL race (mandatory):** worker A persists reservation + RUNNING owner + prepared descriptor + matching Bridge ModelRun, then pauses before consume. Worker B replays and commits NEEDS_REVIEW. A resumes; consume locks/reloads the invocation/attempt and denies with zero fake egress. Separately race revocation with consume under the envelope row lock: revocation-first means zero egress; consume-first means exactly one winning physical request, and later requests are denied. Concurrent identical default claims under distinct client keys and invocation ids yield one dispatch row and one egress winner. A completed winner may be linked to the second invocation with source lineage and zero second charge. A distinct explicit occurrence can make an intentional identical poll/refresh. Use separate PostgreSQL sessions, preferably worker processes; an in-memory barrier is not an oracle for row-lock ordering.
- **Budget / lineage:** concurrent completion/replay cannot create two usage receipts for one dispatch; all page/request artifacts link to the originating invocation or Bridge attempt; reused results retain their source invocation/observation plus a claimant link and zero second budget charge; acquisition same-hash verified reuse records source acquisition lineage and zero VT download usage.

PostgreSQL tests must demonstrate actual SELECT … FOR UPDATE, uniqueness, CAS and commit ordering. Fake transport proves the application’s invocation count and payload, not that a third party received bytes.

## 10. Compatibilité, migration et rollback

No migration is required merely to use existing PENDING, RUNNING, NEEDS_REVIEW or current submachine states. The proposed dispatch identity/state needs an additive Alembic revision after the exact current head: dispatch table, claimant-link table, state CHECK, a partial unique default-claim index on (investigation_id, effect_identity) while claim state is not DENIED or NOT_SENT, a unique explicit-occurrence index on (investigation_id, effect_identity, occurrence_id) while claim state is not DENIED or NOT_SENT, immutable artifact/digest columns, typed owner FKs and indexes for recovery. Thus completed and ambiguous rows remain claimants; only proven pre-consume refusal or NOT_SENT can make a later row eligible. Add nullable dispatch linkage to specific attempt rows only if needed to preserve FK integrity. Preflight must inspect any supported pre-existing/manual shape and fail before DDL if it is partial or non-canonical. 0001_baseline stays frozen; do not rewrite historical 0002; follow ADR-0007’s preflight and downgrade guard discipline.

Roll out in deny-by-default stages: add schema; deploy gate and permit-required transports with no external operation enabled until the path is registered; migrate one operation family at a time; verify every in-scope egress is routed before enabling that family. Existing persisted RUNNING operations without a dispatch row are treated as NEEDS_REVIEW and never backfilled as consumed or resent. Backfill only immutable metadata that can be proven from stored request/evidence; otherwise preserve legacy records as review-only.

Rollback is forward-safe, not a downgrade that reopens the old direct-send path. Disable new external dispatches by making the gate deny and keep history. Roll application code back only to a compatibility release that still requires permits; stop older workers before a rollback because an old binary cannot honor a new database gate. A populated dispatch table is not dropped. A disposable-database downgrade may be guarded to refuse while any dispatch/reconciliation evidence exists. Operational restore is from a verified backup in isolation, per ADR-0007.

## 11. Deletion test

After migration, delete RegistryToolDispatcher.pre_dispatch_check as an optional protocol and delete InvestigationService.execute_tool_call’s check-then-dispatch check_current_admission closure: it no longer owns a send decision. Delete any direct call from the scoped investigation adapters to VT, Shodan, Retrohunt or model transport that lacks a dispatch reference. Make the provider request methods require a permit so deleting the gate produces a compile/type error or a zero-call denial in the adapter contract test.

Do **not** delete provider-specific parameter validation, G2/TLP/disclosure checks used for user-facing errors, raw evidence archival, VT/Shodan route restrictions, Retrohunt slot/review logic, acquisition hash/quarantine/reuse logic, Bridge prompt/result persistence, or local compute. These retain domain meaning. A deletion review should use source search over the in-scope registration/runtime call graph and assert every physical external send is reachable only from a permit-requiring adapter; a successful search result is not enough unless direct-entry tests fail closed.

## 12. Comparaison avant / après avec éléments vérifiables

| Concern | Before (VERIFIED) | After (proposed) |
|---|---|---|
| Seam | Optional dispatcher callback; main Bridge and lower provider paths are separate. | Required prepare/dispatch seam for each in-scope physical request, including Bridge and acquisition. |
| Identity | Client operation_key; raw args stored under normalized_parameters; special Retro/acquisition/Bridge identities are uncoordinated. | Versioned canonical effect digest with a shared default claim across client keys; explicit occurrence only for an intentional identical request. Claimant links preserve lineage and charge usage once. |
| Revocation race | Fresh callback reads envelope/reservation then returns before adapter; no consume transaction joins these records. | Consume locks envelope, owner, reservation, specialized row and dispatch; commit is the serialized decision, then no lock through I/O. |
| Repeated physical calls | VT, Retrohunt results, acquisition routes and Bridge retry/correction loops can make several requests under one tool/turn execution. | Each page/hop/retry/correction is a distinct consume and lineage child; a post-consume revocation blocks later steps. |
| Ambiguity | Separate services mostly retain reservation and avoid automatic resend, but state is not unified with a send permit. | Common consumed→NEEDS_REVIEW rule; only framework proof produces NOT_SENT and permits a new child attempt. |
| Durable worker vs sync API | Tool route and worker Bridge take separate paths. | Same durable EffectRef interface; worker reloads, consumes and dispatches in its own execution context. |
| Test oracle | In-memory barriers and generic PostgreSQL uniqueness tests do not prove combined revocation/consume order. | Required independent-session PostgreSQL races plus fake exact-request/zero-call transport assertions. |

Source facts are cited in §1 and §2; the table’s “after” column is **INFERENCE / proposal**, not current implementation.

## 13. Risks, UNKNOWN et limites

- **Hardest unresolved risk — hidden physical requests.** VT download redirects/fallback and VT/Retrohunt pagination are nested below current application calls. Every request must be surfaced through the gate without changing route safety, streaming verification, archive completeness or provider limits. Hidden HTTP SDK retries must be ruled out or intercepted. **UNKNOWN** until every concrete httpx/model SDK call path and retry setting is traced and a direct-entry zero-call test exists.
- **PostgreSQL link and lock feasibility.** Current reservations may be embedded in the envelope aggregate while Retrohunt and acquisition have separate durable rows. The proposed typed owner links, exact lock order and transaction scope must be reconciled with concrete ORM tables/repositories and their current UoW. **INFERENCE** that a short transaction can lock them together; not verified by running PostgreSQL here.
- **Request byte retention.** Exact model prompts and provider bodies can be large. Reuse immutable versioned artifacts already holding authorized content where possible; descriptor references must preserve all authorized evidence and avoid silent size caps. Signed URLs and credentials are never retained. Confirm the existing model request persistence is sufficient for every adapter; otherwise add a secure, versioned request artifact design.
- **Budget semantics.** Bridge currently reserves one logical turn across retries/corrections; VT relationship/search reserves bounded units; acquisition also consumes separate investigation budget. The migration must avoid double charge while making each physical dispatch measurable. No new arbitrary cap is proposed.
- **Existing binaries.** Old workers can bypass a new application-level seam. Deployment must fence/stop them before enabling the gate; rollback to ungated code is not safe. This is an operational compatibility requirement, not proven by an offline test.
- **Provider idempotency / receipt.** **UNKNOWN.** No remote idempotency or canonicalization is assumed. PostgreSQL plus fakes can prove local serialization and call count only, not provider receipt or whether a provider processed a request.
- **Response timing.** Late responses after a row has moved to NEEDS_REVIEW must be attached by exact dispatch id and request digest. If they cannot be matched, preserve them as review material; do not reopen automatic submission.
- **Non-investigation callers.** This design is scoped to the investigation effects in the matrix. **UNKNOWN** until runtime composition is traced for other direct users of shared VT/model clients. If a shared raw client is reachable from these investigation adapters, split a permit-required investigation adapter from the generic client rather than assuming all callers already pass through this seam.

