# M06 S3 Core — Final Delta Review

**Verdict: REVISE**  
**Reviewed candidate:** `0637bdeab65d480ea505f48fc500fa637623ebd5`  
**Delta:** `642fae96c192a9a73c00fe5dcf454e855d4c05dd..0637bdeab65d480ea505f48fc500fa637623ebd5`  
**Findings:** 3 MAJOR, 0 BLOCKER  
**Review worktree:** remained at `c5f27331a2424ab816bbdf807345a5080bbe2c82`; candidate source was read from Git objects and an isolated `/tmp` archive. No tracked source was changed; no provider or database was contacted.

The delta fixes the earlier false `DEFINITIVELY_NOT_SUBMITTED` classification for transport exceptions, detached pre-entry sends, and unentered returns. It does not yet make the gate own the physical operation. Two further persistence/authority gaps remain below. The 0003→0004 migration and PostgreSQL checks are still integration gates, separate from these code findings.

## Findings

### MAJOR — Gate entry is not bound to exactly one physical submission or its expiry

`PermitHandoffGate.enter` validates and burns the permit, records a durable nonce, then returns to the transport (`backend/src/cti_app/application/external_effects.py:424-467`). The transport receives the raw request and gate separately (`:252-260`); after `enter` returns, the coordinator accepts `gate.entered` and persists completion (`:578-598`). No operation/callback is passed through the gate, so the gate cannot control which bytes the adapter submits, when it submits them, or how many times it submits them.

**Failure scenario:** a transport enters the gate, waits past the permit TTL, transmits twice, then returns one `TransportResponse`. I reproduced this against the candidate with a fake transport: TTL 0.05s, 0.12s delay after `enter`, two fake physical sends; the coordinator returned `Completed` with `physical_calls=2`. The permit is valid at gate entry but expired at both transmissions. The existing expiry test only delays *before* `enter` (`backend/tests/test_external_effects.py:442-453`), and the normal fake transport sends immediately after gate entry.

**Required correction:** bind gate admission to one coordinator-controlled physical-submission operation using the exact prepared request. Make the one-use/expiry check occur at invocation of that operation, and prevent the adapter from invoking it twice or retrying through the same authority.

**Required tests:** delay after gate entry past TTL and assert the physical operation is not invoked and the result cannot be `Completed`; attempt a second submit/retry and assert one physical invocation maximum. Include a request-mismatch case at the operation boundary.

### MAJOR — URL fragments still persist OAuth token aliases

The URL validator parses query and fragment fields, but the sensitive-name set omits `id_token` and `refresh_token` (`backend/src/cti_app/domain/external_effects.py:486-514`). `request_ref` and evidence references use this validator (`:121-122`; `EvidenceReference.__post_init__` at `:54-59`), and canonical request strings are checked by the same safe-reference path.

**Failure scenario:** `https://host/path#id_token=credential-probe` and `https://host/path#refresh_token=credential-probe` pass persistence validation. A candidate-only probe confirmed `id_token` was accepted in both a canonical request and an `EvidenceReference`; `request_ref` follows the same validator. These credentials can therefore be stored in claim metadata/evidence references.

**Required correction:** include OAuth ID/refresh token aliases and other credential-bearing fragment names in normalized URL parameter rejection.

**Required tests:** test both aliases in query and fragment forms through canonical request, `request_ref`, and `EvidenceReference`; assert neither the secret nor the URL is retained in error text or reprs.

### MAJOR — The no-handoff proof registry accepts caller-selected issuers

The process-global `_NO_HANDOFF_PROOFS` exposes `issue(claim_id, issuer)` with no coordinator authorization check (`backend/src/cti_app/application/external_effects.py:200-232,235-241`). The SQL store accepts any proof registered by that function and then CASes a consumed claim to `DEFINITIVELY_NOT_SUBMITTED` if no handoff nonce exists (`backend/src/cti_app/infrastructure/database/repositories/external_effects.py:456-489`). The coordinator uses the same global method at `application/external_effects.py:663-675`.

**Failure scenario:** code with access to the claim store imports the module singleton, calls `_NO_HANDOFF_PROOFS.issue(claim_id, object())`, and passes that registered proof to `definitely_not_submitted`. The current unit test itself directly issues a proof with an arbitrary `object()` issuer and expects the store transition to accept it (`backend/tests/test_external_effects.py:498-524`). Object identity prevents copying/reuse, but does not establish that the coordinator issued the proof.

**Required correction:** make issuance depend on a coordinator-controlled capability that the registry/store validates, rather than accepting an arbitrary caller-supplied issuer. Do not expose a process-global mint operation that any importing code can invoke.

**Required tests:** a foreign caller/issuer must be unable to mint an accepted proof; copied, reused, wrong-claim and forged proofs remain refused; only the coordinator's pre-transport seal can change a consumed claim. Prove the durable transition boundary, not only a fake store.

## Prior findings and requested checks

- **Late seal / false no-send:** corrected for coordinator-managed flows. `_seal_zero_handoff` is reached only on immediate permit self-validation failure before transport invocation. Transport exceptions/cancellation and returns without gate entry become `NEEDS_REVIEW`; a detached gate after a definitive seal is refused by the closed/terminal claim. The unresolved issue is post-entry physical I/O control (Finding 1).
- **Proof binding:** copied, reused, wrong-claim and altered-issuer proof objects are refused. The global mint path still permits an arbitrary registered issuer (Finding 3).
- **Expiry:** expiry is checked while the durable handoff marker is committed and rechecked after that transaction (`repositories/external_effects.py:349-382`; `application/external_effects.py:443-467`). It is not enforced at actual physical submission after `enter` returns (Finding 1).
- **Reservation fixture ordering:** fixed. The integration fixture flushes invocation/reservation rows before inserting reservation amount rows (`backend/tests/integration/test_external_effects_postgres.py:256-294`).
- **Policy and lock order:** OSINT_POLICY remains default-deny without a guard; consume locks the policy guard before envelope/state/invocation/reservation/claim checks. The PostgreSQL revocation harness uses two independently created sessions and a test-only advisory-lock guard with in-memory revoked state; it does not exercise the actual 0003 policy service. PostgreSQL was not run.
- **Per-source model rules, source refs and attempt binding:** unchanged in this delta and remain as reviewed on 642fae96: model submissions receive per-source LLM checks even without a Bridge turn; source-required bindings reject missing/malformed/mismatched refs; family attempt subject/operation/provider/input digest and model lineage checks remain. No production adapters are wired by this core.
- **Migration 0004:** remains additive with `down_revision = "0003_osint_open_policy"`, restrictive FKs/checks/indexes, downgrade data guard, and defaults/index drift preflight (`backend/migrations/versions/0004_external_effect_claims.py:22-23,76-211,223-237`). Candidate `env.py` imports the Retrohunt models. The current parent integration worktree HEAD `de8b7451` still does not contain migration 0003 (`7ef241f2` is available only in the sibling policy worktree) and its `env.py` still lacks the frozen-baseline `external_effect_*` exclusion. Therefore 0001→0004 has not been applied or validated. This is a pending integration gate, not a defect in the 0004 revision itself.
- **PostgreSQL concurrency barrier:** candidate 0637 still has an unbounded `asyncio.Barrier` rendezvous (`backend/tests/integration/test_external_effects_postgres.py:149-158,365-379`). Parent follow-up `de8b7451` adds timeout/abort handling, but that follow-up and the PG test remain unvalidated against a disposable database. Revocation tests use separate DB sessions, but a fake guard. No PostgreSQL test is reported as PASS.
- **Acquisition:** the store refuses acquisition sends until dispatch lineage exists; no silent fallback was found in the prior repository trace.
- **Secrets and production scope:** raw request headers/body/target remain repr-hidden and are not persisted/logged by this core. Credential URL filtering remains incomplete as above. No shared family wiring or production `send_one` call path is added; no family path should be claimed protected or READY until it is wired and tested through an operation-owning gate.

## Checks actually run

- Candidate unit suite from an isolated archive: `PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=src pytest tests/test_external_effects.py -q --tb=short -p no:cacheprovider` — **46 passed**. The prescribed `uv run --no-sync` invocation could not use the read-only uv cache; with a temporary cache its fresh no-sync environment had no pytest module, so the already-installed global pytest executable was used. No dependency installation or network access occurred.
- Ruff check on all 7 changed files — **PASS**.
- Ruff format check on all 7 changed files — **PASS; already formatted**.
- `git diff --check 642fae96..0637bdea` — **PASS**.
- Typecheck — **not independently run**; parent reports PASS.
- PostgreSQL concurrency, revocation, crash/recovery and migration chain — **NOT_RUN**. Candidate contains the unbounded barrier; parent follow-up bounds it, but the combined chain still lacks integrated 0003 and the frozen-baseline exclusion. No database URL was used.

## Disposition

Do not approve this candidate while the three MAJOR findings remain. Fix the physical-operation gate, complete credential alias rejection, and restrict proof issuance; then run the PostgreSQL barrier/revocation/recovery checks and apply 0001→0004 on a disposable database after integrating 0003 and the baseline exclusion. Keep all production family READY claims blocked until each adapter uses the coordinator-controlled one-shot operation.
