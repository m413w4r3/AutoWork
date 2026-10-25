# Mission 05 — final audit and handoff

Status: **PHASE 3 OFFLINE CLOSED — CORRECTIVE CLOSURE APPLIED.** Local `main`
is ahead of the published baseline; **no new push, force-push, merge, release
or deployment was performed by this closure**.

The earlier snapshot at `a30a7c22` is preserved under "Published baseline". The
corrective closure (C1–C4, M05-REV) is recorded below. No earlier verdict is
deleted; each one is shown with its evolution.

## Published baseline (`a30a7c22`) — history

The original mission closed with this table. Its "Remote push: NOT PERFORMED"
row was true when written but is now **obsolete**: the owner published the
mission on `origin/main` at `a30a7c22`, and CI run **#40210449466** on that SHA
completed successfully.

| Item | Value |
|---|---|
| Baseline `origin/main` at mission start | `55fba67d5882c1ae6c4abff4e74c53a24e57649f` |
| Final code SHA under the original gates | `25de7308` (M05-D integration `70ffb9df` + test-only isolation fix) |
| Mission code commits | M05-A `b8a10165`; lint fix `7e51912a`; M05-F `12671c6a`; M05-B `8da3300c`; M05-C `222942e2`; M05-D `70ffb9df`; isolation fix `25de7308` |
| Mission docs commits | `0db6a904` … `507ed250`, plus the close commit `a30a7c22` |
| Remote push | **PERFORMED BY OWNER AFTER CLOSE** — `origin/main == a30a7c22`; the earlier "NOT PERFORMED" row is obsolete |
| Force-push / history rewrite | none |
| CI on `a30a7c22` | **SUCCESS** — run #40210449466, 5/5 jobs |

Original verdicts at that close (kept as history):

```text
DEEP_ADVISOR_PREPARATION_PASS|REVISE       : DEEP_ADVISOR_PREPARATION_PASS
E2E_NO_SAMPLE_PASS|PARTIAL|BLOCKED         : E2E_NO_SAMPLE_PARTIAL
E2E_SYNTHETIC_SAMPLE_PASS|PARTIAL|BLOCKED  : E2E_SYNTHETIC_SAMPLE_PASS
PHASE3_OFFLINE_PASS|PARTIAL                : PHASE3_OFFLINE_PARTIAL
S3_BLOCKED_DESIGN                          : S3_BLOCKED_DESIGN (unchanged)
ADVISOR_CUTOVER_BLOCKED_BY_S3              : ADVISOR_CUTOVER_BLOCKED_BY_S3 (unchanged)
MAIN_LOCAL_REVIEWED|NOT_READY              : MAIN_LOCAL_REVIEWED
READY_FOR_MAIN_PUSH_REVIEW|NO_GO           : READY_FOR_MAIN_PUSH_REVIEW
```

Original reasons for the PARTIAL verdicts: real P10 `propose()` requires origin
Samples; authoritative V4 reparse requires a V4 archive while the production
writer remains v3; in-flight crash recovery was NOT_DEMONSTRATED.

## Corrective closure (C1–C4) — current state

### Git

| Item | Value |
|---|---|
| Published baseline `origin/main` | `a30a7c22fa2967235543ae794ee4350800e2acf8` |
| Local `main` (closure code SHA) | `4111acf3` |
| Closure commits | `dd5636e1` C1; `5f62805a` C2; `ca78f1e4` C2 audit correction; `7b896f20` proven-no-send scenario; `4424fb4d` P09 assertion; `4111acf3` review corrections |
| Distance from `origin/main` | 6 commits ahead; worktree clean |
| New push / force-push / merge | **NONE** — no push performed by this closure |
| CI on the new commits | **NOT_RUN** — they are unpushed |
| CI on the published baseline | **SUCCESS** — #40210449466 on `a30a7c22`, 5/5 jobs |

The closure diff `a30a7c22..4111acf3` touches **only three integration test
files** (+1241/−24): `test_investigation_e2e_no_sample_postgres.py` (modified),
`test_invariant_proposals_postgres.py` (new), and
`test_inflight_job_recovery_postgres.py` (new). **No application code changed.**

### Closure lots

| Lot | Objective | Agent | Result SHA | Status |
|---|---|---|---|---|
| M05-C1 | Correct the E2E-0 contract; real P10 absence + real P10 where preconditions hold | Luna Implementer xhigh | `9d54d6a1` → integrated `dd5636e1` | INTEGRATED |
| M05-C2 | In-flight crash recovery oracle | Luna Implementer xhigh | `b5421ff6` + `f7c58900` → integrated `5f62805a`, `ca78f1e4`; parent `7b896f20` | INTEGRATED |
| M05-C3 | Parent integration and full gates | parent | `4111acf3` | GATES GREEN |
| M05-C4 | Documentation closure | parent | this docs commit | DONE |
| M05-REV | Independent closure review | Luna xhigh, separate workspace | candidate `ca78f1e4` **REVISE** (2 MAJOR, 1 MINOR) → corrections `4424fb4d`/`4111acf3` → **APPROVED** on `4111acf3` | APPROVED |

### C1 — corrected E2E-0 contract

Two V3 requirements were physically incompatible with the application
invariants as written. They are corrected at the **acceptance-criteria** level,
not by relabelling:

1. **P10 in a no-sample investigation.** `ProposalConversationService.propose()`
   requires a bound goodware baseline and at least one origin Sample. E2E-0
   cannot satisfy them. The driver now calls the **real** service and asserts
   the typed first-precondition failure
   `ProposalContractError("The investigation has no goodware baseline binding")`,
   with durable proof of no subject Samples and no binding, zero adapter calls,
   no P10 turn and no P09 call. The refused-P10 terminal gate is retained.
   **Coverage elsewhere:** a new focused PostgreSQL test proves the real P10
   with preconditions satisfied (persisted inert Sample, imported and bound
   goodware index, real snapshot, one local fake send, a real P09 invariant,
   replay of the same turn with one send).
2. **Authoritative V4 reparse.** V3 requires a v4-admissible advisor response
   through the fake transport, not an authoritative V4 reparse of the
   consultation archive. The production writer remains v2/v3-authoritative and
   `ADVISOR_CUTOVER_BLOCKED_BY_S3` is unchanged. The driver keeps the synthetic
   offline V4 parser + `advisor.reparse` proof and marks the authoritative
   archive reparse `NOT_APPLICABLE` while the cutover is blocked. Offline V4
   coverage is retained.

### C2 — in-flight recovery oracle

A new disposable-PostgreSQL oracle proves a worker lost **during** execution,
not only terminal-job redelivery:

- **Ambiguous submission (PASS):** the worker dies after the durable submission
  claim. Durable state before recovery: job `RUNNING` with a stale heartbeat,
  turn `RUNNING`, ModelRun `RUNNING`/`EXTERNAL_STATE_UNKNOWN` with a stable
  bridge request id, bridge reservation `RESERVED`, zero committed. A **fresh**
  engine/session factory/services/dispatcher on the same database calls the real
  `JobService.recover_abandoned`, requeues the same attempt, resumes, seals the
  turn as `orphaned_running_turn`, keeps `NEEDS_REVIEW`, commits the reservation
  once, keeps provider sends at **1**, and preserves the input/authorized/
  evidence hashes, prompt template id/version and requested model exactly.
- **Proven no-send (PASS):** the worker dies after the `RUNNING` turn and
  pre-submission ModelRun are committed but before the submission claim. The
  durable run is `NOT_SUBMITTED`, attempt 0, with no bridge request id, and the
  provider fake saw **zero** sends. Recovery preserves that proof, seals to
  `NEEDS_REVIEW`, keeps sends at zero and invents no retry authorization.
- **Framework pre-submission-proof tool-invocation branch:** `NOT_EXERCISED`.
  This is recorded, not claimed.

This test does **not** implement S3 and does not change `S3_BLOCKED_DESIGN`.

### Final gates (actually executed at `4111acf3`)

| Gate | Result |
|---|---|
| Four targeted oracles (E2E-0, E2E-1, P10, in-flight) at `ca78f1e4` | **PASS** — 4 passed in 8.43 s |
| Revised P10 + both recovery scenarios at `4111acf3` | **PASS** — 3 passed in 3.37 s |
| `make lint-backend` at `4111acf3` | **PASS** — 854 files already formatted |
| `make typecheck-backend` at `4111acf3` | **PASS** — no issues in 827 source files |
| `make test-backend` at `ca78f1e4` | **PASS** — 4912 passed, 2 skipped, 31 deselected in 133.04 s; later commits are integration-test-only |
| `make test-integration` at `4111acf3` | **PASS** — 351 passed in 151.44 s |
| `make test-workflow` at `ca78f1e4` | **PASS** — 22 passed, 15 deselected in 62.31 s, JUnit guard OK |
| `make test-workflow-full` at `ca78f1e4` | **PASS** — 32 passed, 5 deselected in 115.37 s, JUnit guard OK |
| `make test-ci-scripts` at `ca78f1e4` | **PASS** — 5 tests |
| Frontend gates | **NOT_RUN** — no frontend path touched |
| GitHub CI on `a30a7c22` | **SUCCESS** — #40210449466, 5/5 jobs |
| GitHub CI on the new local commits | **NOT_RUN** — unpushed |

The first full integration run at `ca78f1e4` was **RED** (1 failed / 349
passed): `test_agentic_correction_loop_postgres` resolved the snap `typst`
wrapper instead of the pinned binary. An isolated rerun with the pinned Typst
on `PATH` passed (3.74 s), and the full rerun passed. This is recorded as an
environmental RED → GREEN, not a silent repair.

### Independent review (M05-REV)

Luna xhigh, separate workspace, read-only. On candidate `ca78f1e4` the verdict
was **REVISE** with two MAJOR findings — `InvariantRegistryService` used default
`Settings()` and could read `backend/.env`; and the recovery test checked hash
lengths/presence instead of exact pre/post equality — plus one MINOR (documentation
inventory). Both MAJORs were corrected in `4424fb4d`/`4111acf3`; the delta was
re-reviewed and returned **APPROVED** on `4111acf3`, with no BLOCKER or MAJOR
remaining. The reviewer did not run PostgreSQL itself; it reviewed the source
and the parent-reported 351-pass integration run. Residual: the proven-no-send
class is demonstrated against a local fake provider boundary and the test
recovery path, not a live transport.

## Final verdicts (closure)

Previous verdict at `a30a7c22` → closure verdict at `4111acf3`:

```text
DEEP_ADVISOR_PREPARATION_PASS|REVISE       : DEEP_ADVISOR_PREPARATION_PASS (unchanged)
E2E_NO_SAMPLE_PASS|PARTIAL|BLOCKED         : E2E_NO_SAMPLE_PARTIAL → E2E_NO_SAMPLE_PASS
E2E_SYNTHETIC_SAMPLE_PASS|PARTIAL|BLOCKED  : E2E_SYNTHETIC_SAMPLE_PASS (unchanged, replayed)
IN_FLIGHT_RECOVERY_PASS|PARTIAL            : NOT_DEMONSTRATED → IN_FLIGHT_RECOVERY_PASS
PHASE3_OFFLINE_PASS|PARTIAL                : PHASE3_OFFLINE_PARTIAL → PHASE3_OFFLINE_PASS
S3_BLOCKED_DESIGN                          : S3_BLOCKED_DESIGN (unchanged)
ADVISOR_CUTOVER_BLOCKED_BY_S3              : ADVISOR_CUTOVER_BLOCKED_BY_S3 (unchanged)
MAIN_LOCAL_REVIEWED|NOT_READY              : MAIN_LOCAL_REVIEWED
READY_FOR_MAIN_PUSH_REVIEW|NO_GO           : READY_FOR_MAIN_PUSH_REVIEW
```

`E2E_NO_SAMPLE_PASS` is justified only by the corrected contract and its
coverage elsewhere, both documented in `E2E_0_NO_SAMPLE.md`. `PHASE3_OFFLINE_PASS`
requires both E2E PASS plus the deep Advisor preparation; all applicable
offline gates are green.

## Missing proofs / non-claims

- **Authoritative V4 reparse of the consultation archive** remains
  NOT_DEMONSTRATED and is excluded by the corrected E2E-0 contract while
  `ADVISOR_CUTOVER_BLOCKED_BY_S3` holds. Offline V4 parse/reparse is proven.
- **The missing-Sample snapshot guard** is later than the missing-baseline
  guard and is not directly exercised in E2E-0; the driver asserts
  `subject_samples == []` and the real P10 test satisfies the guard. This is a
  residual coverage note, not a missing requirement.
- **The framework pre-submission-proof tool-invocation branch** is
  NOT_EXERCISED by the in-flight oracle.
- **MinIO/S3:** not exercised; `S3_BLOCKED_DESIGN` unchanged. This test does not
  implement S3.
- **Real provider/Bridge/VT/Shodan/CTI:** never called. All transports are local
  fakes; the new tests assert provider send counts and construct no network
  client.
- **Real VirusTotal transport and MinIO/S3 in E2E-1:** still NOT_DEMONSTRATED.
- **Malware:** never downloaded, executed, scanned or uploaded.
- **CI on the new commits:** NOT_RUN because they are unpushed.
- **Token/cost figures:** UNKNOWN (not measured).

## Next permissible action

The owner must authorize a push of the six local commits before any remote CI
runs on them. The push is a new outward-facing action and was **not** performed
here. After authorization: push `main` normally (no force-push), let CI run,
then review the new CI result. No release, repin, deployment or live migration
is authorized by this handoff.
