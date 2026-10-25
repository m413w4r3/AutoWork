# Mission 05 / Phase 3 — compact lot registry

Date opened: 2026-10-25. Owner prompt:
`.prompts/PROMPT_PASEO_AUTOWORK_MISSION05_PHASE3_V3_EXECUTION_ARCHITECTURE_E2E.md`.
`.prompts/` is gitignored, so it is absent from Paseo worktrees; recon agents
therefore worked from the checked-in `ARCHITECTURE_DECISIONS.md` plus their
briefs. The parent applied the V3 requirements when writing those briefs.
`BLOCKED_V3_PROMPT_SOURCE` in an agent report is a worktree-visibility note,
not a mission blocker.
Baseline: `main@55fba67d5882c1ae6c4abff4e74c53a24e57649f`
(clean worktree, `origin/main` identical at preflight).

Hard rules: local `main` only; no push/PR/release; no live provider, Bridge,
VT, Shodan, CTI source, live PostgreSQL/MinIO, migration or deployment. S3
stays `S3_BLOCKED_DESIGN`; Advisor cutover stays
`ADVISOR_CUTOVER_BLOCKED_BY_S3`. These rules governed the original mission and
the closure. The owner published `a30a7c22` to `origin/main` after the original
close; the closure pass performed no new push.

## DAG

```text
M05-A (Advisor deep) ──────────────┐
M05-R0 (E2E-0 recon) ──> M05-B ────┼──> M05-D (adversarial, cross-review)
M05-R1 (E2E-1 recon) ──> M05-C ────┘
```

B starts after R0's map and A's interface freeze; C after B's integration and
R1's map; D after B+C. A's application files are never touched by B/C/D.

## Registry

| Lot | Objective | Agent (Paseo) | Base SHA | Result SHA | Status | Tests actually run | Review | Risks / deps |
|---|---|---|---|---|---|---|---|---|
| M05-DOCS | Open registry, DAG, briefs, decisions | parent | 55fba67d | (this commit) | INTEGRATED | docs only | n/a | none |
| M05-A | TARGET_A deep Advisor preparation, one pure renderer, byte parity | A1 Luna Implementer xhigh | 55fba67d | 2e7733be → integrated `b8a10165` | INTEGRATED | candidate: 114 passed/1 skipped; advisor tools 125; integrated main: 240 passed; ruff+mypy clean | R_A Luna xhigh independent: **APPROVED** (2 MINOR, no blocker) | Golden + RED replayed at baseline; anti-slop SHA unchanged; no S3/cutover |
| M05-R0 | Map E2E-0 seams/gaps, no edits | R0 Luna Audit xhigh | 55fba67d | 0db6a904 (read-only) | DONE — report in `_work/R0_E2E0_MAP.md` | no repo test run in agent sandbox (loopback denied); parent baseline run PASS | n/a | Found existing vertical test acquires a sample; context/advisor/audit/PDF/restart absent from one journey |
| M05-R1 | Map E2E-1 sample/storage/D7 gaps, no edits | R1 Luna Audit xhigh | 55fba67d | 0db6a904 (read-only) | DONE — report in `_work/R1_E2E1_MAP.md` | no repo test run in agent sandbox (loopback denied); parent acquisition run PASS 6/6 | n/a | Filesystem blob store is the existing isolated test store; MinIO test-only isolatable but unproven; D7 matrix exists at unit level only |
| M05-B | E2E-0 continuous no-sample scenario | B1 Luna Implementer xhigh (full-access, loopback only) | 2e7733be (A branch) | 337f983f → revised `80076e6a` → integrated `8da3300c` | INTEGRATED (partial) | parent 337f983f 1/1; revised 80076e6a 1/1; integrated main 1 passed in 4.55 s | R_B **APPROVED** on 80076e6a as an honest partial (not E2E_NO_SAMPLE_PASS) | `E2E_NO_SAMPLE_PARTIAL`: real P10 and authoritative v4 reparse NOT_DEMONSTRATED | **V3 contradiction**: real P10 service requires origin Samples, impossible in E2E-0 no-sample; v4 reparse needs a v4-parsed archive while the authoritative path archives v2/v3. Driver labels both NOT_DEMONSTRATED. |
| M05-C | E2E-1 inert synthetic sample scenario | C1 Luna Implementer xhigh (full-access, loopback only) | 73ea620f (main with A) | 2748bdeb → revised `38d9312d` → integrated `222942e2` | INTEGRATED | 2748bdeb parent 1/1; revised 38d9312d parent 1/1 (4.25 s); integrated main 1/1 (5.17 s) | R_C **REVISE** on 2748bdeb; **APPROVED** on 38d9312d (4 findings resolved) | `E2E_SYNTHETIC_SAMPLE_PASS`; real VT transport and MinIO/S3 NOT_DEMONSTRATED |
| M05-F | Fix Advisor missing-ref UnboundLocalError -> typed refusal | F1 Luna Implementer xhigh | 7c549ec3 | a1b356c6 → integrated `12671c6a` | INTEGRATED | integrated main: 241 advisor tests passed; anti-slop SHA unchanged | R_F Luna xhigh **APPROVED** (2 MINOR, no blocker) | Unblocked C | Unblocks M05-C; no byte change |
| M05-D | Adversarial replay, regression gates, cross-review | D1 Luna Implementer xhigh (full-access, loopback only) | 37690820 | 59f8c85c → 85af3430 → 1667418c → integrated `70ffb9df` | INTEGRATED | focused module 3 passed (parent 5.06/5.35 s; reviewer 5.32 s); B/C drivers on D tree 2 passed (7.06 s); integrated main: integration 348 passed, backend 4912 passed | R_D xhigh independent: REVISE → REVISE → **APPROVED** on 1667418c (all findings resolved, one new ordering assertion added) | Terminal completed-job redelivery proven; in-flight crash recovery NOT_DEMONSTRATED; depends B+C |
| M05-IDFIX | Reserve hardcoded edition codes in the integration allocator | parent (root-cause fix after D integration) | 70ffb9df | `25de7308` | INTEGRATED | first full integration run 1 failed/347 passed (`CT` collision); after fix 348 passed in 115.78 s | n/a — test-only isolation fix, no review lot | Exposed by D's added allocation pressure; nine hardcoded codes now reserved per the allocator contract |

Status vocabulary: `PLANNED / RUNNING / REVIEW / APPROVED / BLOCKED /
INTEGRATED`. A lot is integrable only with relevant tests green and an
independent `APPROVED` review; `REVISE` means correction plus a new review.

## Closure corrective pass (C1–C4, M05-REV)

Base `a30a7c22` (published). Worktrees were used only for the independent Luna
agents; integration is on local `main`. No new push was performed.

| Lot | Objective | Agent | Base | Result SHA | Status | Tests actually run | Review |
|---|---|---|---|---|---|---|---|
| M05-C1 | Correct the E2E-0 contract; real P10 absence + real P10 with satisfied preconditions | Luna Implementer xhigh, wks_660f81c51758d0fa | a30a7c22 | `9d54d6a1` → integrated `dd5636e1` | INTEGRATED | targeted 2 passed (4.58 s parent); integrated four oracles 4 passed (8.43 s) | M05-REV xhigh |
| M05-C2 | In-flight crash recovery oracle | Luna Implementer xhigh, wks_5f9ddda089ed2fd6 | a30a7c22 | `b5421ff6` + `f7c58900` → integrated `5f62805a` + `ca78f1e4`; parent `7b896f20` adds the proven-no-send class | INTEGRATED | 1 passed 1.92 s (ambiguous); 2 passed 2.79–3.07 s (both classes); exact hash/version assertions in `4111acf3` | M05-REV xhigh |
| M05-C3 | Parent integration and full gates | parent | a30a7c22 | `4111acf3` | GATES GREEN | lint, typecheck, backend, full integration (351), workflow FAST/FULL, CI scripts | — |
| M05-C4 | Documentation closure | parent | `4111acf3` | (this docs commit) | DONE | docs only | — |
| M05-REV | Independent closure review | Luna xhigh, wks_569a4cf025c30962 | a30a7c22 | candidate `ca78f1e4` **REVISE** (2 MAJOR, 1 MINOR) → corrections `4424fb4d`/`4111acf3` → **APPROVED** on `4111acf3` | APPROVED | independent source review; reviewer did not run PostgreSQL (parent-reported 351 passed) | — |

The closure diff `a30a7c22..4111acf3` changes only three integration test files:
`test_investigation_e2e_no_sample_postgres.py` (modified),
`test_invariant_proposals_postgres.py` (new),
`test_inflight_job_recovery_postgres.py` (new). No application code changed.

## File ownership (conflict prevention)

| Lot | Owned paths |
|---|---|
| M05-A | `backend/src/cti_app/application/advisor_consultation.py`, new pure renderer module under `application/`, renderer helpers in `investigation_advisor_tools.py`, advisor unit tests, new parity test/fixtures |
| M05-B | `backend/tests/integration/test_investigation_vertical_postgres.py` and/or one new `test_investigation_e2e_no_sample_postgres.py`; no application changes without an explicit parent amendment |
| M05-C | one new `test_investigation_e2e_synthetic_sample_postgres.py` (+ inert fixture module); no B file, no A file |
| M05-D | one new adversarial test module, mission docs; may read all |

## Evidence files

Required final set in `docs/mission05-phase3/`: `ARCHITECTURE_DECISIONS.md`,
`ARCHITECTURE_IMPLEMENTATION_AND_DELETION_TEST.md`, `E2E_0_NO_SAMPLE.md`,
`E2E_1_SYNTHETIC_SAMPLE.md`, `TEST_AND_CI_LEDGER.md`,
`SECURITY_REPLAY_AND_S3_GATES.md`, `FINAL_AUDIT_AND_HANDOFF.md`, `_work/`.

## Verdicts (closure pass, code SHA `4111acf3`)

Previous verdicts at `a30a7c22` are retained before the arrow; the arrow shows
the closure evolution. No earlier verdict is deleted.

```text
DEEP_ADVISOR_PREPARATION_PASS|REVISE : DEEP_ADVISOR_PREPARATION_PASS (unchanged; no advisor code changed in the closure)
E2E_NO_SAMPLE_PASS|PARTIAL|BLOCKED    : E2E_NO_SAMPLE_PARTIAL → E2E_NO_SAMPLE_PASS (typed P10 absence with the real service; real P10 proven where preconditions hold; authoritative archive reparse excluded while the cutover is blocked, offline V4 retained)
E2E_SYNTHETIC_SAMPLE_PASS|PARTIAL|BLOCKED : E2E_SYNTHETIC_SAMPLE_PASS (unchanged and replayed at 4111acf3; real VT transport + MinIO/S3 still NOT_DEMONSTRATED)
IN_FLIGHT_RECOVERY_PASS|PARTIAL       : NOT_DEMONSTRATED → IN_FLIGHT_RECOVERY_PASS (ambiguous submission: no second send, NEEDS_REVIEW, exact hash/version preservation; proven no-send: zero sends, proof preserved, no retry authorization; framework tool-invocation proof branch NOT_EXERCISED)
PHASE3_OFFLINE_PASS|PARTIAL           : PHASE3_OFFLINE_PARTIAL → PHASE3_OFFLINE_PASS (both E2E PASS, deep advisor PASS, all applicable closure gates green)
S3_BLOCKED_DESIGN                     : S3_BLOCKED_DESIGN (unchanged)
ADVISOR_CUTOVER_BLOCKED_BY_S3         : ADVISOR_CUTOVER_BLOCKED_BY_S3 (unchanged)
MAIN_LOCAL_REVIEWED|NOT_READY         : MAIN_LOCAL_REVIEWED (closure gates rerun on local main at 4111acf3; see TEST_AND_CI_LEDGER.md)
READY_FOR_MAIN_PUSH_REVIEW|NO_GO      : READY_FOR_MAIN_PUSH_REVIEW (no new push performed; owner authorization required)
```
