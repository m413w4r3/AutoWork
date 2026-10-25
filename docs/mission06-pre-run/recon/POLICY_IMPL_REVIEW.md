# M06 OSINT_OPEN policy implementation review

**Verdict: REVISE**  
**Candidate:** `f0787d52260937ef751ceb830129ebe456855139`  
**Base reviewed:** `bed7c9af99675241321e999a16a39d1e0b59998b`  
**Findings:** 1 BLOCKER, 3 MAJOR, 4 MINOR  
**Mode:** Read-only review. No code or migrations were changed. No provider, live PostgreSQL, MinIO, secret, or runtime directory was accessed.

The candidate update is test-only: `f0787d52` changes `backend/tests/test_osint_open_policy.py` and no production source. It fixes test setup without weakening the intended no-envelope external denial, local-read success, or hard-budget rejection assertions. The checkout remains at `bed7c9af`; the delta was reviewed from the commit object.

## f0787d52 delta review

The complete `bed7c9af..f0787d52` diff is limited to 21 insertions and 2 deletions in `backend/tests/test_osint_open_policy.py`. The capability helper now maps budget-dimension kwargs into `budget_limits`, so the existing hard-budget rejection test configures the intended budget. The operator-tool fixture now supplies the full local tool definition and starts the investigation in `OPERATOR_LED`, matching the route's allowed mode. The import added for that mode has an unresolved Ruff `I001` ordering finding.

The corrected tests retain their security assertions: the Bridge fake records zero submissions when no envelope exists despite permissive source flags; the external operator tool still raises while `local.read` succeeds; and the hard-budget test still expects `OsintPolicyError` when a configured dimension exceeds its hard maximum. The test suite passes on the exact f078 test content (22 passed); the Ruff finding is reported separately below. These fixes do not change production behavior or resolve the previously identified BLOCKER/MAJOR findings, so the verdict on f078 remains **REVISE**.

The requested `POLICY_SEAM.md` and `POLICY_DESIGN_REVIEW.md` are absent from this checkout. I read the recoverable versions from `c921396d` and `d53538b0`, respectively, plus the current OSINT policy and governance contract. `0001_baseline` and `0002_c2_experiments` are unchanged between the base and candidate.

## Requirement status

| Item | Result |
|---|---|
| B-1 mutation identity | **Pass in route code.** Policy activate/extend/revoke, G2 approve/extend/revoke, and G3 confirmed/rejected use the shared verified-human gate; actors come from server identity. |
| B-2 inherited authority | **Pass with a minor audit-field concern.** Typed policy authority and event lineage are present; no inherited `PIVOT_APPROVE` is created. |
| B-3 no-envelope / TLP | **Pass for reviewed paths.** No envelope and unknown TLP deny external Bridge use; operator external tools deny while local reads and operator-client context reads remain available. |
| B-4 revocation | **Partial.** Revocation updates the pinned envelope and API/effective status; the effect-claim-to-send race remains for S3-CORE. |
| M-1 subject pin | **Partial.** The subject is stored and checked by `effective_for`, but the Bridge worker does not call that check at effect admission. |
| M-2 disclosure | **Pass.** Source and policy restrictions intersect monotonically; unknown TLP, RED, `do_not_submit`, and disabled external disclosure deny. |
| M-3 budgets | **Partial.** Required finite dimensions, positive enabled-capability budgets, hard maxima, and growth-only extensions are implemented. The effect-claim ceiling is stored/validated but never consumed or enforced. |
| M-4 audit | **Partial.** Policy events have correlation, canonical payload hash, and version lineage; individual invocations do not retain the authorizing policy event/version. |
| M-5 migration | **Pass in migration code.** 0003 is additive, has no backfill, preserves NULL-as-HUMAN_G2 reads, explicitly writes new authority kinds, and guards downgrade. Full integration migration assertions are stale and the new downgrade/legacy-read guards lack direct proof tests. |

## Findings

### BLOCKER — B-4: Revocation can commit after the last check and before Bridge submission

**Failure scenario.** The worker loads an approved envelope at `investigation_service.py:718-729`, checks disclosure at `:773`, and provides `_bridge_submission_allowed` as a pre-submission callback at `:788-850`. That callback re-reads the envelope and returns a boolean at `:2971-2979`; it does not hold a transaction or lock through transport submission. Policy revocation can therefore commit after the callback returns true and before `_bridge.turn` emits the request. The worker still holds the earlier permissive envelope.

The policy service does lock policy scope and active pins and updates inherited envelopes in the revocation transaction (`application/osint_open_policy.py:203-277,488-541`; repository locking at `repositories/osint_open_policy.py:54-100,237-247`). The comment at `application/osint_open_policy.py:386-388` gives the intended S3-CORE order. However, `effective_for(for_update=True)` is not itself the claim transaction: it reads the subject before locking current policy, then locks the pin, and does not lock the investigation, envelope, or a unique claim (`:370-417`).

**Required correction / next gate.** S3-CORE must implement the atomic, idempotent effect-claim transaction and make each transport honor its committed claim. Use the documented order (current policy status, pin, investigation, envelope, unique effect-claim key), commit the claim and budget reservation together, and preserve uncertain outcomes for reconciliation. A claim committed before revocation may complete at most once; a claim after revocation must refuse locally. This is the separate S3-CORE lot and remains a blocker before external authority can be considered race-safe.

**Proof required.** Two-session barrier tests for revoke-before-claim, claim-before-revoke, revoke-between-final-check-and-send, replay/uncertain recovery, and human supersession racing revoke. Assert zero sends after revocation and at most one send for a committed pre-revocation claim.

### MAJOR — M-1: Bridge worker does not revalidate the pinned subject

**Failure scenario.** The inheritance stores `subject_id` and `effective_for` compares it with the current investigation (`application/osint_open_policy.py:370-380`). Operator external-tool calls use an effective-policy resolver (`operator_tool_invocation.py:1320-1329`), but the Bridge worker does not: `InvestigationService.run` loads the investigation and envelope and proceeds to Bridge using the investigation subject (`investigation_service.py:701-729,839-850`). The subject field is mutable (`domain/production.py:1373-1384`), and repository saves update the investigation row (`infrastructure/database/repositories/production.py:105-115`). A subject change after pinning can therefore leave the Bridge worker using the old envelope without the required pin-to-subject check.

**Required correction.** Make the subject immutable while a policy pin exists, or revalidate the pinned subject at every external-effect admission under the same locks used by S3-CORE.

**Proof required.** Change/race the subject after inheritance and assert Bridge, provider, and acquisition effects all refuse before emission; unchanged in-scope subjects continue to work.

### MAJOR — M-3: Effect-claim cap is validated but not enforced

**Failure scenario.** `OsintCapabilities` requires a positive `max_effect_claims_per_investigation` (`domain/osint_open.py:91-96`), and the service checks it against a configured maximum (`application/osint_open_policy.py:482-486`). The field is persisted and returned by the API, but no runtime claim counter or claim repository consumes it. A policy can therefore exceed its stated effect-claim ceiling while its other budgets remain available.

**Required correction.** Enforce the cap atomically and idempotently in the S3-CORE claim transaction. Refuse locally at the exact limit before any effect can be emitted.

**Proof required.** Test positive/mandatory configuration, hard maximum, growth-only extension, exact-boundary success, one-over refusal, replay, and concurrent claims with no excess sends.

### MAJOR — M-4: Tool invocations cannot reconstruct their authorizing policy event

**Failure scenario.** Policy version, event, and subject are available on the pin and inherited envelope. A durable `ToolInvocation` stores investigation/operation identity and budget fields, but no authority kind, policy ID/version, or policy event ID (`domain/investigation.py:430-445`). After a manual G2 supersession or later policy revocation, an auditor cannot associate a specific emitted or denied invocation with the authority that applied at its execution time.

**Required correction.** Persist the authorizing authority and policy event/version on each effect claim or invocation, including the human-G2 case, and retain it through retries and reconciliation.

**Proof required.** For emitted, denied, replayed, and uncertain invocations, reconstruct the exact authority event/version from the stored invocation/claim and verify the canonical payload hash.

### MINOR — B-2: Policy activator is also returned as `approval_actor_id`

The inherited-envelope constructor sets `approval_actor_id=actor_id` even though the decision UUID is a policy event (`application/investigation_governance.py:182-218`). The API exposes that value under the per-envelope approval label (`api/investigation.py:3086-3092`). The explicit `OSINT_POLICY` kind and event ID prevent a fabricated HUMAN G2 decision, and the concurrency integration test asserts no AnalystDecision exists (`tests/integration/test_osint_open_policy_postgres.py:199-225`). Still, the activator is stored twice and can be misread as the individual investigation approver. Keep the activator solely on the policy event or expose it under a policy-specific field.

### MINOR — B-1: Route authorization is sound; route-level proof is incomplete and status can overstate approval availability

All requested mutation routes use `_require_verified_human_operator`; its checks require HUMAN_OPERATOR, reject OPERATOR_CLIENT and reserved system/agent/bridge IDs, accept a verified authenticated provider, and permit local identity only on loopback in a non-protected environment (`api/investigation.py:1837-1986,2263-2303,3913-3932`; `api/osint_policy.py:232-296`). The request models forbid extra fields and carry no actor ID (`api/investigation.py:202-267`; `api/osint_policy.py:33-75`). No mutation bypass was found.

The focused policy test covers operator-client context read versus G2 approve/policy activate (`tests/test_osint_open_policy.py:435-469`), but it does not exercise actor-body spoofing or every mutation route with agent/system identities. Existing security tests cover selected additional routes. Also, `api/main.py:702-721` can report `approvals_allowed=true` for a local identity on a protected loopback environment, while the mutation helper denies that case; align the status projection with the actual gate.

### MINOR — M-5: Integration migration assertions still describe the 0002 head

Before running the full integration suite, update `backend/tests/integration/test_migrations.py` and Alembic metadata registration:

- Import `osint_open_policy` in `migrations/env.py:9-27` and in `test_migrations.py:60-77`; otherwise the three new ORM tables are absent from `Base.metadata`.
- Add `osint_open_policies`, `osint_open_policy_events`, and `osint_open_inheritances` to the investigation table registration set at `test_migrations.py:106-130`.
- Add the new trigger/function pairs to `EXPECTED_TRIGGERS` at `:151-253`: `trg_osint_open_policy_events_append_only → reject_osint_open_policy_event_mutation` and `trg_osint_open_policies_immutable → reject_osint_open_policy_version_mutation`. `EXPECTED_FUNCTIONS` is derived from this map.
- Update the stale head comment at `:81-86` and hard-coded head assertion at `:1032` from 0002 to 0003.
- After the C2 downgrade-guard test attempts a downgrade from head and catches the 0002 refusal, assert the still-stamped revision is 0003 at `:1236`; PostgreSQL transactional DDL rolls back the preceding 0003 downgrade too.
- Add PostgreSQL assertions for 0003 refusing downgrade with policy rows or policy envelope lineage, and for a legacy envelope with NULL authority reading as HUMAN_G2 without backfill. Current policy migration tests only upgrade a shape without a populated legacy envelope (`tests/integration/test_osint_open_policy_postgres.py:474-519`).

The candidate chain test itself is updated correctly (`tests/test_alembic_chain.py:9-27`).

### MINOR — f078 test delta has a Ruff import-order failure

The corrected test still asserts zero Bridge submissions for permissive source flags without an envelope (`tests/test_osint_open_policy.py:307-318`), external-tool refusal plus successful local read (`:320-353`), and rejection above the configured hard budget (`:273-279`). The delta only fixes the helper's budget-dimension overrides and sets the operator fixture to OPERATOR_LED (`f0787d52:backend/tests/test_osint_open_policy.py:84-100,324-353`). It does not weaken those assertions.

Ruff check on the exact f078 test blob reports `I001` for import ordering after adding `InvestigationExecutionMode`; `ruff format --check` passes. Reorder the imports before merge.

## Checks and test evidence

- Requested command `cd backend && uv run --python 3.12 pytest tests/test_osint_open_policy.py -q --tb=short`: **blocked** because uv could not create a temporary file under read-only `/home/nill/.cache/uv`.
- Fallback using the installed main-repo virtualenv and a temporary copy of the exact f078 test file, with candidate `backend/src` and `backend` on `PYTHONPATH`: **22 passed in 2.38s**.
- Installed Ruff 0.16.3: `ruff check` and `ruff format --check` on the 20 changed Python files at bed7 passed; f078 delta check finds the I001 noted above, and its formatter check passes.
- PostgreSQL integration tests: **NOT_RUN**; no disposable PostgreSQL run was confirmed. The concurrency-start test is a real two-session test: it uses two concurrently gathered start calls, each with a UOW that creates a SQLAlchemy session (`tests/integration/test_osint_open_policy_postgres.py:80-84,156-165,199-225`). It proves one pin/envelope and no fake G2 decision. It does not test revocation racing a claim or send.
- No live providers, database, or object store were contacted.

**Tokens/cost:** UNKNOWN.  
**Next permissible action:** implement the listed corrections, complete the separate S3-CORE claim transaction, update migration assertions, then rerun the focused suite, Ruff, and disposable-PostgreSQL integration gates.
