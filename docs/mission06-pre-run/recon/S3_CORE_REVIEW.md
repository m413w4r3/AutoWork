# M06 S3 Core Adversarial Review

**Verdict: REVISE**  
Candidate: `c5f27331a2424ab816bbdf807345a5080bbe2c82`  
Findings: **1 BLOCKER, 8 MAJOR, 3 MINOR**  
Scope: read-only review of the fixed candidate and required S3 contracts. No code was changed. No provider or database was contacted.

## Findings

### BLOCKER — Migration graph cannot be applied in this checkout

`backend/migrations/versions/0004_external_effect_claims.py:22-25` correctly declares `down_revision = "0003_osint_open_policy"`, but this checkout contains no 0003 revision. The available versions are 0001, 0002 and this 0004. Alembic cannot resolve or apply the candidate chain here, so schema and PostgreSQL behavior remain unverified.

**Scenario:** an operator applies `upgrade head` from this tree and Alembic cannot construct the revision graph.  
**Required correction:** integrate the actual accepted 0003 revision before applying or validating 0004; do not create a substitute 0003 in this S3 lot.  
**Required proof:** Alembic history resolves as 0001 → 0002 → 0003 → 0004, then run the migration against a disposable PostgreSQL database, including fresh upgrade and guarded downgrade cases.

### MAJOR — A transport can assert a false “not submitted” proof

`backend/src/cti_app/application/external_effects.py:157-163,348-360` treats any `FrameworkProvenPreHandoffFailure` raised by the injected transport as proof that no request was handed off. The exception is publicly constructible and carries no framework-only proof token. The unit test at `backend/tests/test_external_effects.py:317-324` accepts that assertion without measuring whether bytes were handed off.

**Failure scenario:** a transport sends the request, then raises this exception. The claim is persisted as `DEFINITIVELY_NOT_SUBMITTED`; a later explicit occurrence can send the same effect again. This contradicts D6’s rule that an adapter/model assertion is not proof (`D6_S3_C_MIGRATION.md:215-225`).  
**Required correction:** once `transport.send` has been entered, treat exceptions as ambiguous unless a trusted transport framework supplies unforgeable evidence that its client handoff never occurred. Keep ordinary validation failures before transport as typed no-send refusals.  
**Required test:** a fake that records handoff and then raises the exception must leave the claim in `NEEDS_REVIEW`; a framework-proven pre-handoff failure must demonstrate zero handoffs.

### MAJOR — OSINT policy authorization cannot meet the required lock order

`backend/src/cti_app/infrastructure/database/repositories/external_effects.py:128-159` locks envelope, stop state, invocation and reservation before `_check_authority` calls the injected policy guard at `:672-688`. The accepted S3 gate requires pinned policy → envelope → invocation → reservation → claim, and the revocation writer to use the same order (`S3_IMPLEMENTATION_AND_GATES.md:32-44`). No concrete 0003 guard or revocation writer is present here.

**Failure scenario:** if the guard locks the policy row, it does so after the envelope lock; a revoker that locks policy then envelope can deadlock. If the guard reads policy without locking it, revocation can commit after that read but before consume commits, allowing a revoked policy to authorize a send.  
**Required correction:** make policy-event locking part of the transaction protocol and acquire it before the envelope; require revocation to use the identical order. Keep the existing default-deny behavior when the guard is absent.  
**Required test:** two independent PostgreSQL sessions exercise revoke-first (zero send) and consume-first (one authorized send), with synchronization around the competing commits.

### MAJOR — Per-source LLM restrictions are skipped without a Bridge turn ID

The envelope LLM check classifies a request as a model submission by provider or `bridge_turn_id` (`repositories/external_effects.py:655-660`), but the per-source `external_llm_allowed` checks run only when `authority.bridge_turn_id is not None` (`:839-843,860-869`).

**Failure scenario:** an invocation-bound `openai` request has no Bridge turn ID, the envelope permits external LLM use, and a referenced sample has `external_llm_allowed = false`. The envelope check passes and the source-level denial is skipped.  
**Required correction:** use one server-owned model-submission classification for both envelope and source checks, or require persisted model-turn/run lineage for every model provider.  
**Required test:** a PostgreSQL-backed invocation with an LLM provider and a restricted sample reference is refused with zero claim/send; the allowed-source counterpart succeeds.

### MAJOR — Source restrictions trust optional, caller-provided references

`PreparedExternalRequest.evidence_refs` defaults to an empty tuple (`domain/external_effects.py:97-110,142-155`). `_check_source_restrictions` derives every sample/document lookup only from those references and silently skips malformed UUIDs (`repositories/external_effects.py:777-820`). It does not establish that the references cover source IDs or content in the canonical/wire request.

**Failure scenario:** a prepared request includes a restricted sample/document identifier or its contents but omits the corresponding evidence reference; no source row is locked or checked. A recognized reference kind with a non-UUID ID is also ignored.  
**Required correction:** have the family normalizer derive required source references from trusted persisted inputs and reject missing, malformed or mismatched references before consume. Do not let an empty caller-supplied tuple mean “no source restrictions apply” for a source-dependent family.  
**Required test:** restricted sample/document requests with omitted, malformed and mismatched references all refuse with zero claim/send.

### MAJOR — Family attempt IDs are not bound to the prepared operation

The Retrohunt path checks only that the supplied row belongs to the investigation and is `reserved`, then marks it `submitting` (`repositories/external_effects.py:746-764`). It does not compare the row’s subject, operation key, rule/corpus hashes or parameters with the authority and prepared request. The model path checks that a turn points to the supplied running run and that both are eligible (`:873-897`), but does not bind the conversation subject/provider/input digest to this investigation and request.

**Failure scenario:** a caller supplies an unrelated reserved Retrohunt slot or a running model turn/run for another subject. The claim may be consumed against that row while the request describes different work; the model path does not verify the conversation’s subject against the investigation subject.  
**Required correction:** validate the family attempt’s investigation/subject, operation identity, provider and prepared-input digest against the request before changing attempt state. Reject family attempt IDs on unrelated families.  
**Required test:** wrong subject, operation key, rule/corpus digest, model provider or input digest refuses with no committed claim and no send; matching lineage transitions atomically.

### MAJOR — Common API-key query credentials pass persistence validation

`backend/src/cti_app/domain/external_effects.py:464-495` checks some sensitive query names, but the set omits common aliases such as `x_api_key` and `client_secret`. A URL stored under the innocuous key `url` is checked as a string, not rejected by its field name (`:431-461`).

**Failure scenario:** `canonical_request={"url":"https://host/path?X-Api-Key=secret"}` passes validation. The value is then persisted in `canonical_request` and appears in the default `PreparedExternalRequest` representation.  
**Required correction:** normalize and reject credential-bearing query aliases (including `X-Api-Key`, `client_secret` and equivalent provider forms), or disallow full credential-bearing URLs from persisted references and store only a safe immutable reference.  
**Required test:** credential query variants fail before consume and do not appear in persisted payloads or representations.

### MAJOR — The PostgreSQL “revocation-first” test is not a revocation race

`backend/tests/integration/test_external_effects_postgres.py:299-337` names the case `test_revocation_first_guard_denies_with_zero_send`, but `_DenyPolicyGuard.authorize` always returns `False` (`:77-84`). The test creates no policy event, performs no revocation, and uses one consume call; it does not exercise a second session or either commit ordering.

**Failure scenario:** the test passes even if policy revocation is never read, is read before revocation, or has a broken lock order. It is a false PASS for a required gate.  
**Required correction:** replace or supplement it with a real persisted policy event and a two-session revocation/consume race.  
**Required test:** assert both serial orders, policy/claim state, and physical fake-send counts.

### MAJOR — The “crash before consume” test is vacuous

`backend/tests/integration/test_external_effects_postgres.py:341-355` seeds a case, counts claims, and asserts zero. It never invokes consume, injects a crash, exercises recovery, or checks invocation/reservation state.

**Failure scenario:** this test passes for any implementation because the fixture starts with no claims. It provides no evidence that a crash after reservation and before consume cannot dispatch.  
**Required correction:** exercise the reservation/RUNNING state and inject failure at the pre-consume boundary, then run the recovery path.  
**Required test:** assert no claim, no permit, zero sends, and the expected recovered invocation/reservation state.

### MINOR — Migration preflight accepts some schema drift

`backend/migrations/versions/0004_external_effect_claims.py:71-88` compares column names, types and nullability but not server defaults. At `:159-169`, it filters actual indexes to expected names and therefore ignores unexpected indexes.

**Failure scenario:** two pre-existing tables with an altered JSON default or an extra index can pass preflight, after which the migration skips creation and Alembic records 0004 on a noncanonical shape.  
**Required correction:** compare defaults and the complete index set as part of the canonical shape check.  
**Required test:** preflight fixtures with missing/altered defaults and extra indexes must fail before DDL and leave the schema unchanged.

### MINOR — Permit expiry is not represented or tested

`ConsumedEffectPermit` has no creation/expiry time (`domain/external_effects.py:245-260`); `_PermitRegistry` validates issuer identity and one-use state only (`application/external_effects.py:211-270`). The unit test covers forged/copied/misbound/reused permits (`tests/test_external_effects.py:275-302`) but has no expired case.

The coordinator burns the permit synchronously before calling transport, which limits the current in-process exposure. Still, the required expired-permit refusal has no implementation oracle.  
**Required correction:** define and implement an expiry boundary (or document an equivalent bound tied to the consume authorization cut) before a family transport claims readiness.  
**Required test:** an expired permit and a delayed handoff are refused before fake I/O.

### MINOR — The concurrency test does not force the competing interleaving

`backend/tests/integration/test_external_effects_postgres.py:247-295` does create one session per `consume` through the session factory and starts calls with `asyncio.gather`; it asserts one send. It has no barrier at the consume/claim boundary, so the database race is scheduler-dependent.

**Required correction:** synchronize both sessions immediately before the uniqueness/consume step.  
**Required test:** deterministically release both contenders together and assert one committed claim and one transport call.

## Required checklist

- **`send_one` order — verified in code:** preparation validation precedes store consume; the SQL store exits its transaction before returning `ConsumeCommitted`; permit issuance follows that return; the coordinator calls `transport.send` once; completion persists after the call. SQL locks are not held across transport (`application/external_effects.py:284-382`, `repositories/external_effects.py:128-239`). Exceptions after transport entry become `NEEDS_REVIEW`, except for the unsafe public pre-handoff exception above.
- **Permit binding — partial:** issuer identity, exact issued object, one-use burn, investigation/invocation, effect key, family/provider/scope, request digest, envelope version and approval are checked (`application/external_effects.py:239-270`). Canonical/wire digests are recomputed before consume and handoff (`domain/external_effects.py:350-365`). Forged, copied, misbound, byte-mutated and reused cases have assertions. Expiry is not implemented. There is no concrete raw transport in this change to prove absent-permit denial.
- **Claim uniqueness/reuse — structurally present:** unique `(investigation_id, effect_key)` and conflict reread are implemented (`models/external_effects.py:35-38`, `repositories/external_effects.py:139-238`). Explicit occurrence changes the key. Existing CONSUMED/NEEDS_REVIEW claims return no new permit; COMPLETED reuse requires a family policy and writes a `REUSED` use with zero budget delta (`application/external_effects.py:384-437`, repository `:426-537`). PostgreSQL uniqueness has one two-session test, but it is not barrier-controlled and was not run.
- **Consume checks — partial:** exact envelope version/approval ID, provider allowlist, TLP RED, `do_not_submit`, envelope LLM flag, stop state, investigation status, RESERVED operation key/amounts, invocation RUNNING state, invocation arguments digest, invocation tool/schema binding and configured identity/normalizer versions are checked. The SQL store recomputes effect and occurrence digests. OSINT_POLICY denies before DB I/O when the policy guard is absent (`repositories/external_effects.py:101-126`; unit test `tests/test_external_effects.py:438-453`). Source-reference and per-source LLM gaps are above. No concrete 0003 revocation/approval writer is available here to verify.
- **Lock order — partial:** consume locks envelope → working state → invocation → reservation/ordered amounts → claim → family attempt → model turn/run (`repositories/external_effects.py:541-631,720-765,873-898`). Outcome transition writers lock parents in that same order (`:900-983`). Existing stop-state and envelope repositories lock their own row; no reverse multi-row order was found in those writers. The injected OSINT guard is called after envelope/reservation locks, contrary to the required policy-first order; the 0003 revocation writer is absent.
- **Migration 0004 — partially verified:** additive; correct declared parent; no edit to 0001/0002; model registration added in `migrations/env.py`; claim/use FKs are restrictive; PKs, checks, uniqueness and lookup indexes are present; downgrade refuses while claim/use rows exist; no credential/header/body columns are added. Preflight has the gaps above. The chain cannot be applied in this checkout because 0003 is absent.
- **Tests — partial evidence:** the 19 unit cases contain real assertions for digest stability/mutation, ordering, send count, permits, ambiguity, reuse, redaction and OSINT default deny. The pre-handoff test endorses the unsafe exception contract. PostgreSQL concurrency uses independent sessions but lacks a barrier; the revocation test is static deny; the pre-consume crash test does not exercise a crash. PostgreSQL tests were not run.
- **Acquisition — fail-closed:** the store refuses terminal-ledger acquisition attempts and acquisition-family sends with typed `acquisition_dispatch_attempt_required` until dispatch lineage exists (`repositories/external_effects.py:731-744`). The transaction rolls back the tentative claim on refusal; there is no fallback send.
- **Secrets — partial:** raw wire method/target/headers/body are `repr=False` and not persisted; only the application digest is stored. The URL query alias gap above permits credentials into canonical JSON and its representation.
- **Production scope — verified:** this commit changes only the new core, migration registration, and its tests; it does not alter shared dispatch, composition, or provider transports. No production source outside the new core calls `send_one`. Therefore no current family send path is protected by this change. `S3_IMPLEMENTATION_AND_GATES.md` explicitly keeps all family READY gates blocked; do not claim a protected/READY production path until each complete family slice requires and enforces permits at every physical request.

## Checks run

- Requested `uv run --python 3.12 pytest tests/test_external_effects.py -q --tb=short` could not provision its environment: the configured uv cache was read-only. An offline retry using `/tmp` could not obtain `numpy==2.5.4` because it was absent from cache and network access is disabled. The generated partial `backend/.venv` was removed.
- Installed `pytest` fallback: `PYTHONDONTWRITEBYTECODE=1 pytest tests/test_external_effects.py -q --tb=short -p no:cacheprovider` — **19 passed**.
- Installed Ruff on all eight changed files: `ruff check --no-cache` — **passed**; `ruff format --check --no-cache` — **8 files already formatted**.
- `git show --check` for the fixed candidate — **passed**.
- PostgreSQL integration tests — **NOT_RUN**. No disposable `TEST_POSTGRES_ADMIN_DSN` was provided or identified, and this checkout lacks the required 0003 parent revision. No database was contacted.

## Blockers and next gate

Do not approve this candidate. Correct the MAJOR findings, restore the 0003 migration chain, then run deterministic PostgreSQL tests against a disposable database for claim concurrency, policy revocation in both orders, source restrictions, family-attempt binding, migration preflight and recovery. Keep all production family READY claims blocked until the actual transport slices are wired and their direct-entry/no-permit tests pass.
