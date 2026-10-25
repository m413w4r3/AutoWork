# M05-D brief — adversarial replay, cross-review and regression gates

## Role and workspace

You are the sole implementer of lot **M05-D**, the adversarial validation lot.
Work in a Paseo worktree branched from local `main` **after M05-B and M05-C
are integrated** (the parent gives exact SHAs). Do not modify the A/B/C
application or test files except to add a new adversarial test module. Do not
merge, rebase or push `main`.

Read first: root `AGENTS.md`, `backend/AGENTS.md`,
`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` §"E2E-0/E2E-1" and
`docs/mission05-phase3/_work/REVIEW_PROTOCOL.md`.

## Objective

Independently attack the two integrated E2E journeys and the surrounding
invariants, in a fresh disposable environment, and produce evidence that
either confirms or refutes the claimed gates. An adversarial test must fail
when an invariant is removed; do not write a test that merely mirrors the
implementation.

## Required adversarial checks

1. **Replay both journeys**: run the M05-B no-sample driver and the M05-C
   inert-sample driver sequentially in one fresh disposable PostgreSQL
   environment and confirm both pass. They must use distinct databases and not
   leak sample/observation state into each other.
2. **Context edge cases**: stale cursor → 409; invalid legacy offset → 422;
   full-vs-paged canonical SHA equality; evidence order `(observed_at, UUID)`
   with NULLs last; current finding vs distinct history.
3. **Stop/replay**: stop or interrupt an in-flight investigation at a
   deterministic seam, replay it, and prove no duplicate external send, no
   duplicated canonical chronology, no extra consumption, and a stable
   `NEEDS_REVIEW` diagnostic where the external outcome is uncertain.
4. **Refusal isolation**: for the sampled and non-sampled paths, every veto
   (TLP RED, `do_not_submit`, `external_llm_allowed=false`, source/owner
   restriction, AutoWork credential, signed URL/revocation, completeness/refs
   conflict) yields a typed refusal, `fake_send_count == 0`, and no external
   client construction/call.
5. **PDF/audit provenance**: V5 preserved, V6 augmented with exact
   reviewer/approver in lineage metadata (not automatically visible as names),
   canonical chronology exactly once and in order in the real PDF, audit export
   consistent with PostgreSQL rows.
6. **Egress guard**: run the two journeys under a test-level guard that makes
   any external socket/HTTP call fail (loopback PostgreSQL and local tools
   excepted), proving the fakes are the only external seam.

Any real defect found: document a RED reproduction, apply only a bounded fix
in the owning file with the parent's approval, then re-run. Do not widen scope
to S3 or advisor cutover.

## Global regression gates (run and report exactly)

```sh
cd /home/nill/perso/work/AutoWork   # or your integrated worktree
make lint-backend
make typecheck-backend
make test-backend
make test-integration
make test-workflow
make test-workflow-full
make test-ci-scripts
```

Set `TYPST_BINARY=/home/nill/.local/share/autowork-tools/typst` and `TMPDIR=/tmp`.
If a gate cannot run, report `BLOCKED` with the exact reason; never turn a
skipped/unrun gate into PASS. Frontend gates are required only if the diff
touches frontend files; otherwise record `NOT_APPLICABLE` with evidence.

## Deliverable

- New adversarial test module path + commit SHA; exact commands/results
  (counts and measured durations only).
- Per-check evidence with `path:line`, including the fake-send counters and
  the egress guard.
- Any defect found, its RED/GREEN evidence, and the owning file.
- Global gate table with PASS/FAIL/SKIPPED/BLOCKED/NOT_RUN.
- A separate cross-review of M05-B and M05-C by an independent reviewer who
  did not implement them (the parent launches it after your candidate exists).
- `S3_BLOCKED_DESIGN` and `ADVISOR_CUTOVER_BLOCKED_BY_S3` must remain
  unchanged; state explicitly that no S3 or cutover work was performed.
