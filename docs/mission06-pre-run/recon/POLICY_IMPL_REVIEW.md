# M06 OSINT_OPEN policy implementation review

**Verdict: APPROVED (policy lot; external-effect release gated on S3-CORE)**  
**Candidate:** `331049682a79bc12c4b49588fa722cf680293672`  
**Base reviewed:** `bed7c9af99675241321e999a16a39d1e0b59998b`  
**Residuals:** 1 policy-owned MINOR coverage gap; 1 S3-CORE BLOCKER and 2 S3-CORE MAJOR release gates. No policy-owned BLOCKER/MAJOR remains.  
**Mode:** Read-only review. No code or migrations were changed. No provider, MinIO, secret, or runtime directory was accessed. The requested PostgreSQL module ran against the disposable local database.

The final candidate adds production and test changes through `33104968` on top of `7ef241f2`. The review checkout remains at `bed7c9af`; the final delta was inspected from the commit objects, and tests/lint ran read-only from the implementer worktree at `33104968`.

## f0787d52 delta review

The complete `bed7c9af..f0787d52` diff is limited to 21 insertions and 2 deletions in `backend/tests/test_osint_open_policy.py`. The capability helper now maps budget-dimension kwargs into `budget_limits`, so the existing hard-budget rejection test configures the intended budget. The operator-tool fixture now supplies the full local tool definition and starts the investigation in `OPERATOR_LED`, matching the route's allowed mode. The import added for that mode has an unresolved Ruff `I001` ordering finding.

The corrected tests retained the intended assertions: the Bridge fake records zero submissions without an envelope despite permissive source flags; the external operator tool still raises while `local.read` succeeds; and the hard-budget test still expects `OsintPolicyError` above the configured hard maximum. The import-order issue in f078 is corrected in 7ef.

The requested `POLICY_SEAM.md` and `POLICY_DESIGN_REVIEW.md` are absent from this checkout. I read the recoverable versions from `c921396d` and `d53538b0`, respectively, plus the current OSINT policy and governance contract. `0001_baseline` and `0002_c2_experiments` are unchanged between the base and candidate.

## Requirement status

| Item | Result |
|---|---|
| B-1 mutation identity | **Pass in route code and expanded tests.** All eight mutation operations reject agent/system identities and client-supplied actor overrides. Status and mutation gate now use the same bind source; a positive local-loopback mutation test is still missing (MINOR). |
| B-2 inherited authority | **Pass in final candidate.** Both `/envelope` and `/operator-status` separate policy attribution from G2 individual actor/decision fields; activator resolves from the durable v1 ACTIVATED event. |
| B-3 no-envelope / TLP | **Pass for reviewed paths.** No envelope and unknown TLP deny external Bridge use; operator external tools deny while local reads and operator-client context reads remain available. |
| B-4 revocation | **S3-CORE release BLOCKER.** Policy revocation and Bridge rechecks are present, but the effect-claim-to-send race remains outside this policy lot. |
| M-1 subject pin | **Pass for Bridge admission in 7ef.** The pre-submission callback revalidates authorization and pinned subject, refusing with zero mocked submissions on subject change or revocation. The separate B-4 race still applies. |
| M-2 disclosure | **Pass.** Source and policy restrictions intersect monotonically; unknown TLP, RED, `do_not_submit`, and disabled external disclosure deny. |
| M-3 budgets | **S3-CORE MAJOR release gate.** The effect-claim ceiling is stored/validated but not consumed by a claim counter. |
| M-4 audit | **S3-CORE MAJOR release gate.** Envelope and operator views expose policy lineage; individual invocations still do not retain their authorizing policy event/version. |
| M-5 migration | **Pass by source and test review.** 0003 remains additive; migration metadata and integration assertions now cover the 0003 head, tables, triggers/functions, downgrade refusal, and NULL-as-HUMAN_G2 without backfill. The full migration test module was not run. |

## Findings

### S3-CORE RELEASE BLOCKER — B-4: Revocation can commit after the last check and before Bridge submission

**Failure scenario.** The worker loads an envelope before the Bridge turn. The new `_bridge_submission_allowed` callback re-reads the envelope and effective policy and checks the pinned subject (`application/investigation_service.py:2974-3004`). `BridgeInvestigator.turn` calls it after pacing, then awaits transport submission (`application/bridge_investigator.py:1277-1316`). There is still no claim transaction or lock spanning the final check and transport effect: revocation can commit after the callback returns true and before `submit()` emits the request.

The policy service locks policy scope and active pins and updates inherited envelopes in the revocation transaction (`application/osint_open_policy.py:203-277,488-541`; repository locking at `repositories/osint_open_policy.py:54-100,237-247`). The comment at `application/osint_open_policy.py:386-388` documents S3-CORE's intended order. `effective_for_investigation` is a separate read transaction, not a claim (`:426-431`); it does not hold the policy/pin/investigation/envelope locks through the effect.

**Required correction / release gate.** S3-CORE must implement the atomic, idempotent effect-claim transaction and make each transport honor its committed claim. Use the documented lock order (current policy status, pin, investigation, envelope, unique effect-claim key), commit the claim and budget reservation together, and preserve uncertain outcomes for reconciliation. The registry lists `M06-S3-CORE-1` as **PLANNED** (`docs/mission06-pre-run/MULTI_AGENT_REGISTRY_AND_REVIEWS.md:33`). This is accepted as a mandatory cross-lot gate for policy-lot approval; it must be completed and independently verified before any external-effect release. Until then, OSINT policy authority must not be treated as race-safe for external effects.

**Proof required before release.** Two-session barrier tests for revoke-before-claim, claim-before-revoke, revoke-between-final-check-and-send, replay/uncertain recovery, and human supersession racing revoke. Assert zero sends after revocation and at most one send for a committed pre-revocation claim.

### Resolved in 7ef — M-1: Bridge worker revalidates the pinned subject and policy status

**Verified change.** Composition injects `OsintOpenPolicyService.effective_for_investigation` into `InvestigationService` (`application/investigation_composition.py:1611-1640`). Before transport submission, `_bridge_submission_allowed` refuses if effective policy is missing/unauthorized or if the pinned subject differs from the current investigation (`application/investigation_service.py:2989-3004`). The unit suite asserts zero Bridge submissions for both a subject change and a revoked pin (`tests/test_osint_open_policy.py:352-391`). This closes M-1's missing validation; B-4's check-to-send race remains separate.

### Resolved in 331 — B-2: `/operator-status` separates policy authority from individual G2 approval

**Verified change.** The status route builds a separate `policy_authority` object with policy ID/version/event, current status, and activator (`api/investigation.py:1727-1741`); the operator-view builder suppresses policy envelope decision and actor fields from `g2` (`application/investigation_operator_view.py:416-449`). A version-2 test asserts null individual G2 fields and the policy lineage object (`tests/test_osint_open_policy.py:650-731`).

### S3-CORE RELEASE MAJOR — M-3: Effect-claim cap is validated but not enforced

**Failure scenario.** `OsintCapabilities` requires a positive `max_effect_claims_per_investigation` (`domain/osint_open.py:91-96`), and the service checks it against a configured maximum (`application/osint_open_policy.py:482-486`). The field is persisted and returned by the API, but no runtime claim counter or claim repository consumes it. A policy can therefore exceed its stated effect-claim ceiling while its other budgets remain available.

**Required correction before release.** Enforce the cap atomically and idempotently in the S3-CORE claim transaction. Refuse locally at the exact limit before any effect can be emitted.

**Proof required before release.** Test positive/mandatory configuration, hard maximum, growth-only extension, exact-boundary success, one-over refusal, replay, and concurrent claims with no excess sends.

### S3-CORE RELEASE MAJOR — M-4: Tool invocations cannot reconstruct their authorizing policy event

**Failure scenario.** Policy version, event, and subject are available on the pin and inherited envelope. A durable `ToolInvocation` stores investigation/operation identity and budget fields, but no authority kind, policy ID/version, or policy event ID (`domain/investigation.py:430-445`). After a manual G2 supersession or later policy revocation, an auditor cannot associate a specific emitted or denied invocation with the authority that applied at its execution time.

**Required correction before release.** Persist the authorizing authority and policy event/version on each effect claim or invocation, including the human-G2 case, and retain it through retries and reconciliation.

**Proof required before release.** For emitted, denied, replayed, and uncertain invocations, reconstruct the exact authority event/version from the stored invocation/claim and verify the canonical payload hash.

### Resolved in 331 — B-2: `policy_activated_by` resolves the pinned policy's original activator

**Verified change.** `_policy_activation_actor` loads the version-1 event and checks its policy ID/version, `ACTIVATED` event type, and activation correlation before returning the actor (`api/investigation.py:3911-3929`; repository lookup `infrastructure/database/repositories/osint_open_policy.py:146-153`). The `/envelope` and `/operator-status` tests use a v2 extender actor and assert that `human:policy-activator` from the v1 event is returned (`tests/test_osint_open_policy.py:567-647,650-731`).

### Resolved in 331 — B-1: Approval status and local mutation gate use the same bind-scope rule

**Verified change.** The identity status and mutation gate now both use `request.scope.server` first and configured bind host as fallback, then apply the same literal loopback, local mode, and non-protected environment checks (`api/main.py:707-723`; `api/investigation.py:3984-3995`). The new test uses an exposed request bind and asserts both `approvals_allowed=false` and a 403 mutation refusal (`tests/test_first_run_security_contract.py:513-538`). All eight mutation routes are covered for actor-body override and agent/system refusal (`tests/test_osint_open_policy.py:734-877`).

### MINOR — B-1: Positive local-loopback mutation path lacks a direct test

The new bind-scope test covers the exposed/denied case, and existing tests cover local loopback status plus refusal for a reserved system actor. No test proves that a HUMAN_OPERATOR local identity on a non-protected loopback request reaches a mutation route beyond the identity gate. The gate code allows it (`api/investigation.py:3984-3995`), and the status projection uses the same bind rule, so this is a non-blocking coverage gap. Add a test with a valid local human identity and loopback request asserting the request passes authorization and then reaches its normal handler result.

### Resolved in 7ef — M-5: Migration metadata and integration assertions include 0003

The final delta imports `osint_open_policy` into migration/test metadata, excludes the new tables and envelope columns while executing the frozen baseline, registers all three policy tables and both trigger/function pairs, and advances head assertions to `0003_osint_open_policy` (`migrations/env.py:9-111`; `tests/integration/test_migrations.py:68-166,1042-1045`). It also updates the C2 downgrade rollback expectation and adds tests that 0003 refuses downgrade with populated policy/envelope lineage and reads a legacy NULL `authority_kind` as `HUMAN_G2` without backfill (`tests/integration/test_migrations.py:1239-1406`; `tests/integration/test_osint_open_policy_postgres.py:474-539`). The 0001/0002 revision files remain untouched. The full `test_migrations.py` module was not run; its source assertions now cover the requested cases.

## f0787d52..7ef241f2 final delta review

The delta changes 10 files. The Bridge worker now checks the active pin's authorization and subject both before and after pacer delay; unit tests assert zero mocked sends after subject change and revocation. This resolves M-1's missing pre-submission validation, while the final check-to-send race remains B-4.

The dedicated `/envelope` response now returns policy ID/version/event and subject, and clears `approval_actor_id` for policy authority. `/operator-status` did not receive the same authority-aware projection: it still puts the stored policy event actor and ID in the `g2` object. That is a false individual G2 attribution and remains MAJOR. Also, `policy_activated_by` can name a later version's extender; see the MINOR finding.

The identity status now requires HUMAN_OPERATOR scope and reports local approvals unavailable in a protected environment. A configured-versus-runtime bind-host mismatch remains. Alembic metadata and `test_migrations.py` now include 0003's tables, triggers/functions, head, rollback stamp, downgrade guard, and NULL-as-HUMAN_G2/no-backfill cases. Ruff's f078 import-order finding is fixed.

None of the PG test fixes weakens its invariant: `decisions == []` matches the repository's list return; the reuse-subject fixture now associates each subject with its own edition and includes all three in the policy scope, preserving prior-invocation/manual-envelope/no-retroactive-inheritance assertions; and `_copy_budget` restores existing reservations so revocation preserves already-reserved usage while setting external limits to zero. The added test asserts both facts. The policy PG module passes below.

## 7ef241f2..33104968 final delta review

This delta changes four files: the investigation API, operator-view builder, and the identity-status and policy unit tests. The operator-view builder now clears `g2.approval_decision_id` and `g2.approval_actor_id` for `OSINT_POLICY` envelopes while returning a distinct `policy_authority` object with policy ID, version, event ID, status, and original activator (`backend/src/cti_app/application/investigation_operator_view.py:416-448`; route projection `backend/src/cti_app/api/investigation.py:1715-1724,1810-1818`). The envelope's policy event UUID remains its `approval_decision_id`, as required for inherited-envelope lineage; this does not fabricate an `AnalystDecision(PIVOT_APPROVE)`.

`policy_activated_by` now resolves the version-1 `ACTIVATED` event and validates its policy, version, type, and activation correlation rather than using the current policy version's actor (`backend/src/cti_app/api/investigation.py:3911-3929`). The changed `/envelope` and `/operator-status` tests distinguish a v2 extender from the v1 activator and assert the expected actor and policy lineage (`backend/tests/test_osint_open_policy.py:567-731`). The bind-scope test now asserts that the exposed request reports `approvals_allowed=false` and the mutation gate responds 403 (`backend/tests/test_first_run_security_contract.py:513-538`).

The delta preserves the previous external-tool-denial, local-read-allowed, and hard-budget-rejection assertions; no earlier assertions were removed or relaxed. The final candidate therefore has no policy-owned BLOCKER or MAJOR finding. One MINOR coverage gap remains: a positive local HUMAN_OPERATOR loopback mutation path lacks a direct test. The B-4 revocation/claim race and M-3/M-4 effect-claim enforcement and invocation lineage remain cross-lot S3-CORE gates; registry item `M06-S3-CORE-1` is still **PLANNED** with scope `claim/permis` (`docs/mission06-pre-run/MULTI_AGENT_REGISTRY_AND_REVIEWS.md:33`). They are mandatory before any external-effect release.


## Checks and test evidence

- The requested `uv run --python 3.12 pytest tests/test_osint_open_policy.py -q --tb=short` command was blocked because uv could not create a temp file under read-only `/home/nill/.cache/uv`. On exact candidate `33104968`, `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$PWD/src:$PWD" /home/nill/perso/work/AutoWork/backend/.venv/bin/python -m pytest tests/test_osint_open_policy.py -q --tb=short -p no:cacheprovider` ran from `backend/`: **51 passed in 1.67s**.
- Related identity-status/security unit module on `33104968`: `tests/test_first_run_security_contract.py` **26 passed, 2 skipped in 2.36s**; both skips report that the host kernel cannot provide measured bwrap isolation.
- The requested `uv run --project ... --no-sync` PostgreSQL command hit the same read-only uv cache issue. The equivalent direct installed-venv invocation against the designated disposable database at `127.0.0.1:55432` passed: `tests/integration/test_osint_open_policy_postgres.py` **6 passed in 3.30s**. This is a local disposable test database, not a live service.
- Ruff 0.16.3 on all four files changed by `7ef241f2..33104968`: `ruff check` **passed**; `ruff format --check` reported **4 files already formatted**. `git diff --check 7ef241f2..33104968` was clean.
- The concurrency-start integration test is a true two-session test: concurrently gathered starts each use a UOW that creates a SQLAlchemy session (`tests/integration/test_osint_open_policy_postgres.py:80-84,156-165,199-225`). It proves one pin/envelope and no fake G2 decision. It does not test revocation racing a claim or send. The full `tests/integration/test_migrations.py` module was **NOT_RUN**.
- The implementer worktree resolved to exact candidate `331049682a79bc12c4b49588fa722cf680293672` and remained clean after the read-only test and lint commands. The review worktree remained at base `bed7c9af`; this report is its only untracked output.
- No live providers, MinIO, secrets, or runtime directories were accessed.

**Tokens/cost:** UNKNOWN.  
**Next permissible action:** S3-CORE must implement and independently verify the atomic revocation-safe effect-claim/budget transaction, effect-claim cap enforcement, and durable invocation authority lineage before any external-effect release. Run the full migration test module and the required two-session claim/revocation gates before release; adding the positive local-loopback mutation test is a MINOR coverage follow-up.
