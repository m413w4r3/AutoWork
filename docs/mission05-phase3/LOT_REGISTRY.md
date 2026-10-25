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
`ADVISOR_CUTOVER_BLOCKED_BY_S3`.

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
| M05-A | TARGET_A deep Advisor preparation, one pure renderer, byte parity | A1 Luna Implementer xhigh | 55fba67d | 2e7733be (branch `m05a/advisor-deep`) | REVIEW | candidate: 114 passed/1 skipped; advisor tools 125 passed; ruff+mypy clean; parent independently reran the same | R_A Luna xhigh independent, RUNNING | Golden + 2 RED replayed at baseline; anti-slop SHA unchanged |
| M05-R0 | Map E2E-0 seams/gaps, no edits | R0 Luna Audit xhigh | 55fba67d | 0db6a904 (read-only) | DONE — report in `_work/R0_E2E0_MAP.md` | no repo test run in agent sandbox (loopback denied); parent baseline run PASS | n/a | Found existing vertical test acquires a sample; context/advisor/audit/PDF/restart absent from one journey |
| M05-R1 | Map E2E-1 sample/storage/D7 gaps, no edits | R1 Luna Audit xhigh | 55fba67d | 0db6a904 (read-only) | DONE — report in `_work/R1_E2E1_MAP.md` | no repo test run in agent sandbox (loopback denied); parent acquisition run PASS 6/6 | n/a | Filesystem blob store is the existing isolated test store; MinIO test-only isolatable but unproven; D7 matrix exists at unit level only |
| M05-B | E2E-0 continuous no-sample scenario | B1 Luna Implementer xhigh | A-integrated | — | PLANNED | — | R_B Luna Audit xhigh | Depends R0, A |
| M05-C | E2E-1 inert synthetic sample scenario | C1 Luna Implementer xhigh | B-integrated | — | PLANNED | — | R_C Luna Audit xhigh | Depends B, R1 |
| M05-D | Adversarial replay, regression gates, cross-review | D1 Luna Implementer high + reviewer xhigh | C-integrated | — | PLANNED | — | R_D xhigh, not B/C implementers | Depends B+C |

Status vocabulary: `PLANNED / RUNNING / REVIEW / APPROVED / BLOCKED /
INTEGRATED`. A lot is integrable only with relevant tests green and an
independent `APPROVED` review; `REVISE` means correction plus a new review.

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

## Verdicts (updated at close)

```text
DEEP_ADVISOR_PREPARATION_PASS|REVISE : NOT_RUN
E2E_NO_SAMPLE_PASS|PARTIAL|BLOCKED    : NOT_RUN
E2E_SYNTHETIC_SAMPLE_PASS|PARTIAL|BLOCKED : NOT_RUN
PHASE3_OFFLINE_PASS|PARTIAL           : NOT_RUN
S3_BLOCKED_DESIGN                     : S3_BLOCKED_DESIGN (unchanged)
ADVISOR_CUTOVER_BLOCKED_BY_S3         : ADVISOR_CUTOVER_BLOCKED_BY_S3 (unchanged)
MAIN_LOCAL_REVIEWED|NOT_READY         : NOT_READY
READY_FOR_MAIN_PUSH_REVIEW|NO_GO      : NO_GO
```
