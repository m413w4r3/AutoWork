# M06 OSINT_OPEN adversarial policy review

**Verdict: REVISE**  
**Base:** `7059ebbdd891b98d64f17c026fd3f6169c174063`  
**Findings:** 4 BLOCKER, 5 MAJOR, 0 MINOR  
**Mode:** read-only review; no tests, providers, database, MinIO or Docker were used.

## Review limits

The requested `docs/mission06-pre-run/recon/POLICY_SEAM.md` and
`backend/src/cti_app/api/identity.py` do not exist at this base. The identity
contract is in `backend/src/cti_app/application/identity.py`. The referenced
`S3_IMPLEMENTATION_AND_GATES.md` is also absent, so the policy's S3 claim and
consume protocol cannot be verified here. The findings below use the policy
draft and present runtime paths; they do not claim a future implementation was
tested.

## Findings

### B-1 — Activation and extension do not bind authority to an authenticated human

**Scenario.** A caller supplies a non-empty actor string such as
`dev-analyst`, `operator:static-token`, or `agent:worker`. A route forwards
it to `activate` or `extend`; the service accepts it and creates or expands a
durable `INSTANCE` or `SUBJECT_SET` authorization. A shared local/static ID
cannot identify which human approved the standing authority. The same missing
check can incorrectly label a G3 request as human.

**Evidence.** The draft requires only a non-empty `actor_id` at activation and
extension (`OSINT_OPEN_POLICY.md:47-56`) and exposes untyped actor arguments
(`OSINT_OPEN_POLICY.md:131-138`). The actual identity contract distinguishes
`HUMAN_OPERATOR` and `OPERATOR_CLIENT`, but `LocalIdentityProvider` is
explicitly unverified and defaults to the shared `dev-analyst` identity
(`backend/src/cti_app/application/identity.py:10-28,62-72`); the static-token
provider uses the shared `operator:static-token` actor
(`backend/src/cti_app/application/identity.py:75-105`). Existing envelope
approval passes only `identity.actor_id` (`api/investigation.py:1812-1841`),
while the G2 service checks only non-empty actor, reason and correlation
(`application/investigation_governance.py:174-185`). The G3 route similarly
constructs `QualificationActorKind.HUMAN` from that identity
(`api/investigation.py:2238-2262`).

**Required correction.** Make activation, extension and manual envelope
supersession accept a server-derived principal with an explicit
`HUMAN_OPERATOR` scope and verified attribution. Reject operator-client and
agent principals. Define whether a local identity is allowed only on a
loopback, isolated development instance; do not treat its shared default ID as
personal attribution. Never accept actor identity from request payloads.
Apply the same verified-human requirement to G3 confirmation/rejection.

**Required proof.** Exercise each mutation with a valid authenticated human, an
operator-client, an agent/system principal, a client-supplied actor override,
an unverified local provider on an exposed bind, and the permitted isolated
local case. Only the explicitly allowed human cases may write a version or
decision; stored actor identity must equal the server-derived principal.

### B-2 — Inheritance manufactures an ordinary HUMAN G2 approval

**Scenario.** Policy activation for subject A creates a per-investigation
`PIVOT_APPROVE` whose actor is the policy activator and makes that decision
the envelope's `approval_decision_id`. Audit consumers and G2 logic see an
ordinary approval for that investigation, even though that human approved a
standing policy rather than this individual pivot. `inherited: true` in JSON
details does not change the decision type or authority model.

**Evidence.** The draft explicitly creates a `PIVOT_APPROVE` with the
activation actor and stores it as the inherited envelope's approval
(`OSINT_OPEN_POLICY.md:70-79`). Current `AnalystDecision` has no authority
kind, only a decision type, actor, reason and correlation
(`domain/editorial.py:24-45,81-99`). Current envelope authority is likewise
represented by `approval_decision_id` and non-null `approval_actor_id`, with
no policy authority discriminator (`domain/investigation.py:734-755);
`g2_envelope_authorization_state` interprets the envelope by looking up that
decision (`application/investigation_governance.py:100-123`).

**Required correction.** Add a typed `EnvelopeAuthorityKind` with an
`OSINT_POLICY` case and explicit `inherited_policy_id` / version provenance.
For inherited authority, `approval_decision_id` must identify the policy
activation/version event, not a newly fabricated `AnalystDecision`.
Retain the actual human activator only on the policy event. A direct human G2
approval must remain distinguishable and be the only kind represented as a
human per-investigation approval.

**Required proof.** Inheritance creates exactly one envelope and lineage, no
`AnalystDecision(PIVOT_APPROVE)`, and the authority fields point to the exact
policy event/version. Normal human approval and rejection continue to use
their existing decision records. Audit/API projections must label inherited
authority as policy-derived, never as an individual human approval.

### B-3 — A no-envelope Bridge path bypasses G2 and the proposed evaluator

**Scenario.** An investigation starts with no envelope and an article snapshot
whose `external_llm_allowed` is true, `do_not_submit` is false and TLP is not
RED (including absent TLP). The Bridge-led worker uses its legacy budget and
submits to Bridge without an inherited policy envelope. Alternatively, an
operator-led invocation reaches its service without first ensuring inherited
authority. A policy check added only to one API route leaves these paths open.

**Evidence.** The start route loads an optional envelope and passes it through
without calling an inheritance evaluator
(`api/investigation.py:850-865`). In the Bridge loop, a missing envelope
selects the unapproved legacy budget (`application/investigation_service.py:717-729`),
then calls `external_llm_allowed` before `bridge.turn`
(`application/investigation_service.py:772-849`). The no-envelope branch
allows submission on source flags alone
(`application/investigation_service.py:2803-2819`). The operator endpoint
only requires operator-client scope before invoking its service
(`api/investigation.py:1462-1487`); that service reads the envelope directly
and checks source disclosure, without an OSINT inheritance call
(`application/operator_tool_invocation.py:913-932`). The dispatcher does deny
external-read/acquire/submit tools without an envelope
(`application/investigation_tools.py:886-905`), but it does not cover the
direct Bridge turn path.

**Required correction.** Put one fail-closed authorization evaluator at the
shared effect boundary. Ensure inheritance transactionally before Bridge-led
start, Bridge-led turns, operator-led tool calls, acquisition, direct adapter
calls and transport submission. No envelope, stale policy version, failed
inheritance or unavailable evaluator must deny external effects locally.
Local computation must use an explicitly defined local capability path and
must not implicitly grant provider or acquisition access.

**Required proof.** With no envelope and source disclosure set to permissive,
assert zero Bridge submissions, provider calls and acquisitions through the
start route, Bridge worker, operator-led endpoint, dispatcher, direct adapter
and transport. With an active in-scope policy, each valid path inherits once
and consumes the same authority. With an out-of-scope or revoked policy, every
path denies before emission.

### B-4 — Revocation is not serialized with effect claims and emission

**Scenario.** A worker checks the active envelope, then policy revocation
commits before the Bridge/provider request is emitted. The request still
leaves because the check and emission are separate operations. A similar race
can occur after inheritance checks the policy but before a budget reservation
or effect claim is consumed. A concurrent human extension can also race the
draft's `ACTIVE` to `SUPERSEDED` decision.

**Evidence.** The draft locks the policy and active lineages for inheritance
and revocation, but does not include budget reservation/effect claim or
transport emission in that serialization protocol
(`OSINT_OPEN_POLICY.md:90-103`). Its oracle promises that revocation before
emission means zero calls (`OSINT_OPEN_POLICY.md:184-186`). Current Bridge
admission reads the current envelope and returns a boolean
(`application/investigation_service.py:2952-2960`), then Bridge is called
separately (`application/investigation_service.py:838-849`). Dispatcher
pre-dispatch checks also precede adapter execution
(`application/investigation_tools.py:795-805`). The referenced S3 protocol
is not present at this base.

**Required correction.** Define an atomic, idempotent effect-claim transition
that serializes policy version/status, investigation scope, budget reservation
and claim against revocation. Specify the linearization point: a claim committed
before revocation may finish once; a claim after revocation is rejected. Make
the transport honor that claim and preserve uncertain outcomes for
reconciliation. Serialize manual human supersession with policy revocation.
A stopped/cancelled investigation must not regain policy rights when resumed
after revocation.

**Required proof.** Use deterministic barriers to force revoke-before-claim,
claim-before-revoke, revoke-between-check-and-send, and concurrent human
extension/revoke. The first and post-revoke cases emit zero times; a committed
pre-revoke claim emits at most once, including replay/uncertain recovery.
Also revoke, cancel, resume and attempt a new inheritance; no new policy
authority may appear.

### M-1 — Subject scope is checked only at inheritance; workspace binding is undefined

**Scenario.** An investigation inherits a policy while its subject is in the
allowed set. The investigation's `subject_id` is later changed, or is changed
concurrently after the check, and the already inherited envelope continues to
authorize provider effects for the new subject. If workspaces/tenants can share
or address subjects, the same subject-set policy also has no workspace
boundary.

**Evidence.** Scope is defined only as `INSTANCE` or `SUBJECT_SET` and
`scope_ref` is not defined (`OSINT_OPEN_POLICY.md:20-28`). Inheritance checks
the subject at step 2 but stores no subject/workspace snapshot in the specified
lineage (`OSINT_OPEN_POLICY.md:64-79,153-155`). The current investigation is a
mutable dataclass with a `subject_id` field
(`domain/production.py:1373-1390`); subject equality is checked when created
from synthesis, not as a continuing authorization invariant
(`domain/production.py:1403-1425`).

**Required correction.** Persist the exact subject and, if the application has
workspace/tenant isolation, that scope in each inheritance. Either make those
identities immutable while inherited authority exists or revalidate them at
every effect admission under the same lock as inheritance/revocation. State
explicitly if this application has no workspace authorization dimension and
`INSTANCE` is intentionally global.

**Required proof.** Inherit for subject A, attempt subject B and a cross-workspace
subject binding, and race a subject change against inheritance/effect claim.
Each unauthorized or changed binding must fail closed before any effect; an
unchanged in-scope binding continues to work.

### M-2 — Unknown TLP is not defined as non-shareable

**Scenario.** A source has `tlp=None` (or an unrecognized/empty TLP) but
`external_llm_allowed=true`. The policy's CLEAR default is applied and the
external evaluator treats the source as shareable, so source classification
uncertainty becomes permission.

**Evidence.** The draft states a CLEAR default for freely shareable sources and
preserves explicit source restrictions, but does not define unknown-TLP
handling or a formal source/policy intersection
(`OSINT_OPEN_POLICY.md:38-40,105-114`). The existing evaluator rejects RED
only and permits absent TLP when other flags permit
(`application/investigation_service.py:2807-2818`). The API disclosure helper
uses rank zero for `None` and disables external access only for
`do_not_submit` or RED (`api/investigation.py:3112-3152`).

**Required correction.** Define a monotone disclosure intersection for every
source: policy permission AND source permission, no clearing of
`do_not_submit`, no clearing of `external_llm_allowed=false`, and no TLP
downgrade. Treat missing, invalid or unknown source TLP as denied for external
submission until explicitly classified. Keep historical source rows unchanged.

**Required proof.** Verify inherited CLEAR against each known source TLP and
against missing, empty and invalid TLP. RED, `do_not_submit=true` and
`external_llm_allowed=false` must remain denied; GREEN/AMBER source
classifications must remain unchanged and cannot be widened by extension.

### M-3 — The circuit breaker is optional and finite budgets have no hard ceiling

**Scenario.** An activation enables Bridge or a provider but omits its matching
budget dimension and leaves `max_effect_claims_per_investigation` unset.
The current budget model treats absent limits as unlimited, so a standing
policy can submit indefinitely. An arbitrarily large but technically finite
limit also defeats the intended G2 bound.

**Evidence.** The policy makes the per-investigation effect-claim cap optional
(`OSINT_OPEN_POLICY.md:34-40`) and says budgets are generous/configurable/
finite without setting required dimensions or maxima
(`OSINT_OPEN_POLICY.md:118-123`). In the current budget model, missing limits
produce `remaining=None` and reservations are rejected only when a limit
exists (`domain/investigation.py:270-299`). The governance contract also says
context budget consumption by the constructor is not wired
(`docs/investigation_governance.md:46-49`).

**Required correction.** Require finite, positive limits for every enabled
provider/effect and all relevant dimensions; reject missing or `None` caps.
Define absolute configured maxima and make extension growth-only within those
maxima. Make the effect-claim cap mandatory for each investigation, count claims
idempotently, meter context generation, and refuse locally before emission on
exhaustion.

**Required proof.** Reject enabled providers with absent/unbounded dimensions,
reject a value over the hard maximum, and test exact-boundary success followed
by one over-limit claim. Under concurrent claims only the permitted number may
reach the effect; exhaustion, replay and missing metering all result in zero
additional emissions.

### M-4 — Audit fields can contain secrets, and invocations lack policy lineage

**Scenario.** An operator pastes a provider token or signed URL into the free-text
reason or correlation. The event persists it despite the prose rule against
secrets. Later, an auditor sees a tool invocation but cannot determine which
policy version/event authorized it, especially after extension, supersession or
revocation.

**Evidence.** The draft stores actor, free-text reason and correlation and says
only that the payload is non-secret; it does not define bounds, safe field
types, canonical hashing or secret rejection
(`OSINT_OPEN_POLICY.md:47-60`). The inheritance table stores one investigation,
policy/version, decision and status, but the event/table specification does not
link individual claims or tool invocations to the policy event
(`OSINT_OPEN_POLICY.md:78-83,148-155`). Current `ToolInvocation` records
investigation and operation identity but no policy authority/version
(`domain/investigation.py:430-465`).

**Required correction.** Define an immutable event ID and canonical, complete
non-secret payload hash; enforce unique correlation/idempotency per scope and
version. Use structured bounded reasons and opaque correlation IDs, reject or
redact credential-shaped input before persistence, and never log raw request
payloads. Store the authorizing policy event/version on each envelope transition
and effect claim/tool invocation so provenance remains reconstructable after
supersession and revocation.

**Required proof.** Retry identical activation/extension/revocation content and
get one event; reuse a correlation with changed content and get a conflict.
Submit secret sentinels through every free-text field and assert they appear in
neither persisted events nor captured logs. For each emitted and denied
invocation, reconstruct the exact policy event/version and verify the hash from
the canonical stored payload.

### M-5 — Migration compatibility does not define legacy authority interpretation

**Scenario.** Migration 0003 adds an authority discriminator as nullable to
preserve old rows, but a reader treats `NULL` as unknown/denied or infers
OSINT policy authority. Existing human-approved envelopes then stop working or
are silently reclassified.

**Evidence.** The draft requires additive 0003, no backfill and unchanged
historical envelopes (`OSINT_OPEN_POLICY.md:146-157`), but specifies neither
`EnvelopeAuthorityKind` nor how a nullable authority field is read. Existing
envelope rows contain only the approval decision/actor fields and no authority
kind (`database/models/investigation.py:547-582`). The proposed migration
oracle mentions a downgrade guard for policy lineages/events but not legacy
authority decoding (`OSINT_OPEN_POLICY.md:189-190`).

**Required correction.** Specify `EnvelopeAuthorityKind` with
`OSINT_POLICY` and `HUMAN_G2`. Leave historical rows unchanged and interpret
`NULL` as `HUMAN_G2` in compatibility reads; all new writes must set the
kind explicitly. Keep 0001/0002 frozen and make 0003 downgrade refuse removal
when any policy event, inheritance or policy-authorized envelope exists.

**Required proof.** Upgrade a fixture containing historical approved and
revoked envelopes; assert no row is backfilled or reclassified and `NULL`
reads as `HUMAN_G2`. New inherited envelopes read as `OSINT_POLICY`. Verify
downgrade succeeds only on empty policy tables and refuses once any policy
event, inheritance or policy-authorized envelope exists.

## Coverage that is sound as stated

The G3 separation is correct in the draft: agents may propose candidate,
related or uncertain; confirmed/rejected remain human-only and no
auto-promotion is allowed (`OSINT_OPEN_POLICY.md:124-126`). The current
qualification service rejects non-human actors for confirmed/rejected states
(`application/investigation_governance.py:517-526`) and creates the matching
member decision on those transitions (`application/investigation_governance.py:566-588`).
Keep the draft's G3 oracle (`OSINT_OPEN_POLICY.md:187-188`), with the verified
human attribution correction from B-1.

## Required changes before implementation approval

1. Bind every policy mutation and manual supersession to a verified,
   server-derived human principal.
2. Replace inherited `PIVOT_APPROVE` records with typed
   `OSINT_POLICY` envelope authority and explicit policy event/version links.
3. Fail closed on every no-envelope, direct Bridge, operator, acquisition,
   adapter and transport path; serialize claims with revocation.
4. Bind subject/workspace scope, define unknown-TLP behavior, require hard
   finite budgets and a mandatory claim cap.
5. Complete event secrecy/idempotency/invocation provenance and specify
   historical `NULL = HUMAN_G2` migration compatibility.
