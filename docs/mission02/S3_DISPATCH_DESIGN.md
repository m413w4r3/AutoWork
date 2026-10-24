# M02-S3 — external-effect identity and dispatch permit

**TASK_ID:** M02-S3 · **BASE_SHA:** `e4abba495d9f5fdc296e2ba80b65442855625474`

**Scope:** design only; no provider, database, MinIO, or runtime access.
Labels below distinguish **VERIFIED**, **INFERENCE**, and **UNKNOWN**.

## A. Effect identity (S3A)

### Registered effects and normalizers

The registry validates strict JSON types with Pydantic `TypeAdapter`, then passes
the original argument mapping to the adapter; validation does not return a
canonical model (`investigation_tools.py:597-605,742-784`). Current registered
external effects traced here are:

| Effect class | Registered operation(s) | Authoritative normalization and canonical-key feasibility |
|---|---|---|
| VT reads | `vt.file_report`, `vt.object_report`, `vt.relationships`, `vt.intelligence_search`, `vt.behaviour`; `vt.behaviour_pcap` when configured (`investigation_vt_tools.py:1571-1705`) | **VERIFIED:** hash trims/lowercases; object IDs normalize by type (IP compression, IDNA/lowercase domain, URL validation); relation trims/lowercases; dork trims and normalizes `content:` spacing; cursor is optional; evidence refs parse as UUID; sandbox ID trims (`investigation_vt_tools.py:691,742-755,862-869,998-1022,1161-1178,1186-1188,1348-1350,1732-1750,1790-1847`). **FEASIBLE:** key the normalized target/query, cursor/order, adapter bounds/config and schema/normalizer version. |
| Shodan reads | `shodan.host`, `.search`, `.domain`, `.count` (`investigation_shodan_tools.py:191-258,1202-1222`) | **VERIFIED:** IP trims/canonicalizes, domain trims/lowercases/IDNA, search query trims, page defaults to `1`, evidence refs parse to UUID, justification is sanitized (`shodan.py:139-175`; `investigation_shodan_tools.py:672-679,774-783,936-945,1065-1082`). **FEASIBLE:** normalized provider parameters plus page and adapter/schema version. |
| Bridge advisor | `advisor.consult` is `EXTERNAL_READ`; `advisor.reparse` is local compute (`investigation_advisor_tools.py:3334-3349,3728-3755`) | **VERIFIED:** consultation type is exact-match; question trims and defaults to `""`; evidence/finding refs parse as UUID; deeper consultation-specific defaults and selections are built into prompt data (`investigation_advisor_tools.py:178-205,287-309,1545-1577,2127-2155`). Final encoded context is SHA-256 hashed before transport (`:664-666`); the Bridge key binds investigation, derived turn index, that hash and prompt version (`bridge_investigator.py:264-269,1460-1474`). **FEASIBLE only after a preparation phase:** key the final redacted prompt/context bytes and their source digests/version, not raw caller arguments. |
| VT acquisition | `vt.download_sample` is `ACQUIRE` (`investigation_acquisition_tools.py:663-681`) | **VERIFIED:** SHA-256 is normalized and association identity is deterministic from investigation + hash (`investigation_acquisition.py:114-141,788-792`); verified same-subject reuse can avoid a VT download (`:201-243,245-288`). The adapter passes `context.idempotency_key` into acquisition (`investigation_acquisition_tools.py:504-516`). **FEASIBLE:** key the investigation/subject, normalized SHA-256 and operation kind; keep “reuse vs download” state in the prepared intent. Provider idempotency is **UNKNOWN**. |
| VT Retrohunt | `vt.retrohunt.submit` is `EXTERNAL_SUBMIT`; `.status` and `.results` are `EXTERNAL_READ` (`investigation_retrohunt_tools.py:56-60,81-105`) | **VERIFIED:** submit trims `corpus_id`, requires lowercase SHA-256s, bounds/canonical-JSON-validates parameters and derives a durable reservation operation key (`retrohunt.py:129-150,500-537`). Status/results normalize `reservation_id` through UUID (`investigation_retrohunt_tools.py:123-142`). **FEASIBLE:** include operation/schema/normalizer version, source and corpus hashes, trimmed corpus ID and the normalized parameter object; reads key by reservation ID. Nested/provider-side parameter equivalence is **UNKNOWN**. |

The main `BridgeInvestigator` turn also submits externally but is not a registered
`ToolDefinition`; its turn identity uses investigation, turn index, context hash,
prompt version and attempt (`bridge_investigator.py:1185-1220,1233-1239,1296-1300,1460-1474`).
Its retry key is derived per context/attempt, not a persisted invocation UUID.
It currently calls its transport directly after a read-only callback, so covering
it requires the same new outbound gateway/refactor as the registered adapters
(`investigation_service.py:610-613,661-672`; `bridge_investigator.py:1240-1300`).
Other nearby registrations such as `sample.analyze`,
`sample.set_qualification` and `advisor.reparse` are local compute, not external
effects (`investigation_acquisition_tools.py:683-714`;
`investigation_advisor_tools.py:3728-3756`).

### Identities actually available

* **VERIFIED — request key:** API `idempotency_key` is combined with the
  investigation ID and SHA-256 of raw tool name, schema version, arguments,
  purpose and expected version to derive `operation_key`
  (`operator_tool_invocation.py:735-784`). `ToolInvocation` persists that key and
  a UUID at creation; existing-key replay returns the stored invocation rather
  than dispatching again (`investigation_service.py:1031-1040,1079-1108`). It is
  durable for the same request key/payload, but does not identify semantic
  equivalence after adapter normalization.
* **VERIFIED — `ModelRun.id`:** `ModelConversationService` allocates and persists
  the run, turn idempotency key and input SHA before its model gateway call
  (`model_conversations.py:1189-1202,1267-1305`; `domain/model_runs.py:88-115`).
  This is durable per model conversation turn, but is not used by the main
  `InvestigationService` → `BridgeInvestigator` path above. That path currently
  has no durable invocation UUID; it needs one per persisted Bridge turn/attempt.
  Neither identity is shared by VT, Shodan or a new operator invocation.
* **VERIFIED — envelope/reservation:** envelope identity is
  `(investigation_id, approval_decision_id, version)`, not a separate envelope
  UUID (`domain/investigation.py:735-745`). A budget reservation has no UUID;
  its identity is `(operation_key, amounts, status)` and is durable in the
  envelope snapshot (`domain/investigation.py:218-230,283-299`). The service
  commits `RUNNING` plus the reservation before dispatch
  (`investigation_service.py:1139-1153`).
* **VERIFIED — adapter key and payload SHA:** the dispatcher passes adapters
  `ToolExecutionContext.idempotency_key = operation_key`; it is durable for the
  same raw request replay but is not a normalized effect identity
  (`investigation_tools.py:653-669`). The operator payload digest covers raw
  arguments (`operator_tool_invocation.py:753-784`). Advisor `input_sha256` covers
  assembled pre-send context; VT/Shodan `raw_sha256` hashes are response-side
  (`investigation_advisor_tools.py:664-666,812-835`;
  `investigation_shodan_tools.py:697-698,819`; `investigation_vt_tools.py:606`).
  No normalized request digest is currently persisted before reservation.

**VERIFIED:** the current semantic fingerprint is a heuristic, not the adapter
normalizer. B1A1 R3 documents accepted equivalent `review_text`, nested UUID,
duplicate UUID and VT field variants (`docs/mission01/B1A1_REVIEW.md:139-150`);
do not extend that heuristic.

### Identity options

1. **Typed, versioned canonical effect key — preferred.** Add a non-submitting
   `prepare_effect(arguments, context) -> PreparedEffect` step that returns the
   exact normalized intent, normalizer version and digest before reservation.
   Persist the digest separately from the client request key with a unique
   `(investigation_id, effect_digest)` claim before reservation. Reuse a known
   terminal result; block an equivalent `RUNNING`/`NEEDS_REVIEW` effect; release
   the claim only after framework-proven non-submission. A deliberate identical
   refresh/poll needs an explicit application-controlled occurrence ID. For
   advisor, preparation must finish prompt assembly/redaction and bind source
   digests because the current adapter builds that context after dispatch
   (`investigation_advisor_tools.py:1545-1577,363-365,664`).
2. **Application-supplied durable effect/request ID.** Stable IDs survive retries
   when the application reuses the same ID, but distinct IDs do not detect
   equivalent normalized requests. It is useful as an explicit refresh/occurrence
   discriminator for intentionally repeating a completed VT/Shodan read; it
   must not be the only ambiguity guard.
3. **`NEEDS_REVIEW` fallback.** On any post-decision outcome without proof of
   non-submission, retain the reservation and block an identifiable equivalent
   retry until reconciliation. The existing service already turns persisted
   `RUNNING` into `NEEDS_REVIEW` without redispatch (`investigation_service.py:1080-1108`).

**INFERENCE:** all registered operations have a deterministic application-level
intent that can be fingerprinted using the normalizers listed above; this does
not establish hidden provider canonicalization or provider-side idempotency.
Where preparation cannot produce the complete intent (including advisor source
context), fail before reservation/send or preserve ambiguity as `NEEDS_REVIEW`;
never invent a provider key.

Option 2 is implementable for each row only by allocating and persisting a new
application-owned request ID; existing caller `operation_key` is not an
equivalence identity. Option 3 can block by exact prepared identity for every
row. Advisor needs assembled prompt/source context first; until then block the
same durable invocation and retain `NEEDS_REVIEW`. Retrohunt can key its
canonical submitted JSON, but hidden provider-side equivalence remains **UNKNOWN**.

## B. Dispatch permit and revocation (S3B)

### Current seam

`InvestigationService.execute_tool_call` creates the durable invocation and
reservation, commits them, then builds a `ToolExecutionContext` with no
invocation ID or permit (`investigation_service.py:1031-1040,1139-1188`). Its
`check_current_admission` reloads the envelope/reservation and returns; the
dispatcher then calls the adapter after the check has ended
(`investigation_service.py:1190-1236`; `investigation_tools.py:779-784`). The
context and `ToolAdapter.execute(arguments, context)` interfaces contain no
permit (`investigation_tools.py:224-243,337-340`). The dispatch callback itself
is optional (`investigation_tools.py:412-427,779-784`), so direct adapter or
dispatcher entry is not fail-closed on a permit today. Advisor transport is
entered at `investigation_advisor_tools.py:688-701`.

Revocation saves a newer envelope version and commits its audited decision
(`investigation_governance.py:399-430,460-486`). The SQL envelope repository uses
`SELECT … FOR UPDATE` in `save_if_version` (`repositories/investigation.py:1422-1450`).
The read-only callback is not serialized with that write; a revoke can commit
after callback success and before transport (`investigation_service.py:1190-1226`;
`investigation_tools.py:779-784`).

### Minimal typed contract

Use one small `DispatchPermitAuthority` at the existing dispatcher/service seam;
this is not a global policy engine. Its opaque, immutable permit is minted only
from persisted state, with a private issuer capability (no public constructor,
booleans or caller-filled bindings). Bind:

```text
invocation_id; tool_name + schema_version; ToolEffect;
investigation_id + approval_decision_id + envelope_version;
reservation operation_key + amounts + RESERVED status;
dispatch_decision_id (single logical adapter execution)
```

There is no reservation UUID today, so bind the persisted reservation key and
amounts rather than fabricating one (`domain/investigation.py:218-230`). The
service mints an opaque, nonserializable capability; the gateway atomically
claims its durable `dispatch_decision_id` once (CAS `MINTED → CONSUMED`) while
locking the same envelope row used by revocation. That consume is the send
decision. Only after commit does the gateway enter the provider call. A copied,
replayed or stale permit cannot claim twice. Crash after consume is ambiguous and
stays `NEEDS_REVIEW`; it is never auto-resubmitted. The capability starts one
logical adapter execution; paginated/multi-request adapters use only its scoped
gateway session, which owns provider handles and checks its immutable bindings at
each egress. Direct adapter/provider access outside that session is denied.

For the main Bridge path, persist a durable turn-attempt invocation UUID before
transport; advisor and other operator effects bind their existing
`ToolInvocation.id`. If the permit source/storage is unavailable, stale, bound to
a changed reservation, wrong tool/effect, or unverified, the gateway makes zero
provider calls. Local tools do not use permits.

The ordering cut is gateway consume under the envelope lock. Revocation committed
before consume means zero departure, including a worker paused after mint and
resumed after revocation. Once consume commits, the adapter execution is
authorized: a revocation that commits before the physical HTTP write may still
be followed by that write, and cancellation of that execution is not promised.
This is the accepted serialization semantics; PostgreSQL cannot atomically commit
an arbitrary HTTP departure. A crash or timeout after consume without a proven
outcome retains the reservation and becomes `NEEDS_REVIEW`.

### Serialization choices

* **Lease/outbox:** durable work and lease recovery improve crash visibility,
  but require a queue table/worker/state transitions. Lease expiry can repeat a
  provider call; provider idempotency is **UNKNOWN**, so ambiguous sends still
  need review.
* **Serialization at the gateway:** a new gateway boundary is required; none
  currently spans the direct VT, Shodan, acquisition, Retrohunt, advisor and main
  Bridge transport calls. Its durable consume decision must be shared across
  processes; the dispatcher's current `asyncio.Lock` is process-local
  (`investigation_tools.py:619-625`; VT `investigation_vt_tools.py:696`, Shodan
  `investigation_shodan_tools.py:679,792,945,1082`, advisor
  `investigation_advisor_tools.py:688-701`, main Bridge
  `bridge_investigator.py:1296-1300`).
* **Short database lock — smallest fit:** serialize only final envelope check
  plus durable send-decision write against revocation, then release the SQL
  transaction/connection before HTTP. A SQL lock held through the adapter would
  hold the row and connection for the transport interval; advisor's default
  `tool_seconds` is 3,600 seconds (`config.py:390`; `investigation_advisor_tools.py:3201-3202,3335-3340`).
  Revocation would wait on that row and the DB connection would be unavailable
  to the pool for the same interval (**INFERENCE** from the row lock and call
  order above). Runtime overrides and actual PostgreSQL contention are **UNKNOWN**.

## C. Verdict

**IMPLEMENTABLE_NOW**, if the committed gateway consume is accepted as the
serialization cut. Smallest patch shape: (1) add typed preparation and persist
its versioned digest under an atomic investigation-scoped unique effect claim,
separate from `operation_key`; (2) replay known results, block ambiguous claims,
release only on proven non-submission, and require an application occurrence ID
for deliberate identical refresh/poll; (3) add a durable decision/CAS record,
including a persisted Bridge turn-attempt invocation ID, using the existing
service/envelope seam; (4) route all listed provider calls through a new shared
outbound gateway that consumes the opaque permit once and scopes adapter I/O.
The effect claim and decision record need additive schema changes; follow
`backend/AGENTS.md` and ADR-0007. Preserve `NEEDS_REVIEW`; infer no provider
idempotency.

The design promises ordering at the committed send decision only. PostgreSQL
cannot atomically commit an arbitrary external HTTP departure; revocation after
that decision does not cancel a request already sent or in flight.

## D. Future oracle plan (not run in this design lot)

1. Fake clock/barrier: mint permit, pause worker, commit revocation, resume before
   gateway consume → denied, zero departure, confirmed pre-submission and
   reservation released.
2. Race revocation and gateway consume from independent sessions: row-lock winner
   determines outcome. Also pause after consume but before physical HTTP, revoke,
   resume: one departure is allowed by the declared cut; no cancellation promise.
3. Direct adapter without permit, forged constructor/flags, copied, expired,
   reused or misbound permit, and unavailable permit source → zero calls; present
   one valid permit concurrently twice → exactly one `MINTED → CONSUMED` winner.
4. Concurrently claim the same `(investigation_id,effect_digest)` from distinct
   request keys: one claim, no double reservation/submission; a distinct explicit
   occurrence ID allows intentional refresh. Verify multi-request pagination
   stays within one scoped execution session.
5. Crash/restart at pre-decision, post-consume/pre-HTTP, post-departure/pre-
   response, plus cancel, timeout and late response. An ambiguous consumed
   decision never resubmits; a retry is allowed only after proven non-submission
   releases the claim. Unresolved cases stay `NEEDS_REVIEW`.
6. Assert reservation coherence at each point: release only on framework-proven
   non-submission; retain ambiguity after authorization; commit measured usage
   once on known completion. Check no duplicate invocation or response refs.
7. Use fakes for Bridge/VT/Shodan/acquisition/Retrohunt transport. Add a
   disposable-PostgreSQL concurrency test because the required serialization
   depends on real `SELECT … FOR UPDATE` ordering; never contact providers.

**UNKNOWN:** no PostgreSQL multi-process race was run here; provider idempotency
and server-side canonicalization were not established. The proposed explicit
application occurrence ID is a design choice for intentional identical refresh
or polling. No tests were run, as this is a design-only lot.
