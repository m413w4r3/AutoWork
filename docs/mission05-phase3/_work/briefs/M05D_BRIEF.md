# M05-D brief — adversarial replay, cross-review and global gates

## Role and workspace

You are the sole implementer of lot **M05-D**. Work in your own Paseo worktree
branched from local `main` at `36c83da2` (contains M05-A, M05-F, the M05-B
no-sample driver and the M05-C inert-sample driver). Commit atomically on your
branch; never merge, rebase or push `main`.

Read first: root `AGENTS.md`, `backend/AGENTS.md`,
`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` §E2E-0/E2E-1,
`docs/mission05-phase3/E2E_0_NO_SAMPLE.md`,
`docs/mission05-phase3/E2E_1_SYNTHETIC_SAMPLE.md`, and
`docs/mission05-phase3/_work/REVIEW_PROTOCOL.md`.

## Objective

Independently attack the two integrated journeys and the surrounding
invariants, in a fresh disposable environment. Produce a compact adversarial
test module in `backend/tests/integration/test_e2e_phase3_adversarial_postgres.py`
plus a gate report. Do not modify the A/B/C application or test files.

## Required adversarial checks

1. **Both journeys in one fresh session**: run the M05-B no-sample driver and
   the M05-C inert-sample driver sequentially in one pytest invocation on a
   freshly created disposable PostgreSQL container/database set. Confirm both
   pass and that they use distinct databases/state (no leakage).
   The B driver is an honest partial: real P10 and authoritative v4 reparse
   are NOT_DEMONSTRATED and must stay labelled as such.
2. **Context edge cases** (real PG):
   - stale context cursor → HTTP 409;
   - invalid legacy offset → HTTP 422;
   - full-vs-paged canonical SHA equality;
   - evidence order `(observed_at, UUID)` with NULLs last;
   - current finding vs distinct history.
   Reuse the operator context endpoints/services; do not invent a parallel
   framework.
3. **Stop/replay**: stop or interrupt an in-flight investigation at a
   deterministic seam, replay the durable job, and prove no duplicate external
   send, no duplicated canonical chronology, no extra consumption, and a
   stable `NEEDS_REVIEW` where the external outcome is uncertain.
4. **Refusal isolation**: for both journeys, re-run or directly assert the
   veto matrix (TLP RED, `do_not_submit`, `external_llm_allowed=false`,
   source/owner restriction, AutoWork credential, signed URL/revocation,
   missing refs) with typed refusal and `fake_send_count == 0`. Do not weaken
   an existing driver; add independent checks or invoke the real services.
5. **Egress guard**: add a test-level guard that makes any external socket/
   HTTP call fail (loopback PostgreSQL and local tools excepted) and exercise
   at least one Advisor refusal and one allowed fake send under it. Reuse the
   pattern in `backend/tests/workflow_emulator/test_network_guard.py`.
6. **PDF/audit provenance**: re-render V5/V6 with real Typst/Poppler and assert
   the V6 lineage reviewer/approver, hidden actor names, exactly-once canonical
   chronology and the exact evidence reference form in the extracted text
   (no prefix fallback).
7. **S3/cutover**: explicitly state that no S3/L22 work or Advisor cutover was
   performed; `S3_BLOCKED_DESIGN` and `ADVISOR_CUTOVER_BLOCKED_BY_S3` remain.
   Do not test or implement S3 durability.

Any real defect found: document a RED reproduction, apply only a bounded fix
in the owning file with the parent's approval, then re-run. No scope widening.

## Global regression gates (run and report exactly)

```sh
cd <D-worktree>
make lint-backend
make typecheck-backend
make test-backend
make test-integration
make test-workflow
make test-workflow-full
make test-ci-scripts
```

Set `TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst`,
`TMPDIR=/tmp`, and the disposable PG DSN used by the Makefile. If a gate
cannot run, report `BLOCKED` with the exact reason; never turn a skipped or
unrun gate into PASS. Frontend gates are `NOT_APPLICABLE` unless the diff
touches frontend files (it should not).

## Constraints

- No real provider/Bridge/VT/Shodan/CTI/network call; loopback PG and local
  tools only. No live DB/MinIO, no secret read, no deployment, no migration,
  no release. No malware download/execution; inert bytes only.
- Only the new adversarial test module (plus the gate report in your final
  message). If you need a tiny additive helper, report it explicitly and do
  not change existing helper semantics.
- Commit atomically; report base/candidate SHA, exact commands/results,
  PASS/FAIL/SKIPPED/BLOCKED/NOT_RUN, and residual risks.

## Deliverable (≤80 lines)

- Base/commit SHA, files changed, worktree state.
- Per-check evidence with `path:line` and zero-send/egress proofs.
- Any defect and its RED/GREEN evidence.
- Global gate table.
- Explicit S3/cutover non-claims.
