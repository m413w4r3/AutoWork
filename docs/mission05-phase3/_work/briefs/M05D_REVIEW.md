# M05-D independent review brief — adversarial validation

You are an independent **read-only reviewer**. You did not implement M05-D,
M05-B or M05-C. Do not edit, commit, merge or push; no external service.

Read `docs/mission05-phase3/_work/REVIEW_PROTOCOL.md`,
`M05D_BRIEF.md`, `E2E_0_NO_SAMPLE.md`, `E2E_1_SYNTHETIC_SAMPLE.md`.

## Target

- Base: local main `37690820` (contains A/F/B/C).
- Candidate: the M05-D branch HEAD in its worktree (exact SHA from parent).

## Mandatory checks

1. **Independence of the adversarial tests**: each check must fail if the
   invariant is removed; a test that merely mirrors the implementation or a
   tautological assertion is `REVISE`. Read the new module and look for
   `assert True`, `or len(...)`, prefix fallbacks, broad `except`, skips or
   environment-gated passes.
2. **Both journeys in one fresh session**: run the B no-sample and C
   inert-sample drivers sequentially on fresh disposable databases; both pass;
   distinct DB/state; B's P10/v4 labels remain honest.
3. **Context edge cases**: stale cursor 409, invalid legacy offset 422,
   full-vs-paged canonical SHA, `(observed_at, UUID)` NULLs-last order, current
   finding vs history. Verify the HTTP status/assertion is real.
4. **Stop/replay**: no duplicate fake send, no duplicated chronology, no extra
   consumption, stable `NEEDS_REVIEW` where the external outcome is uncertain.
5. **Veto matrix**: every veto typed refusal and `fake_send_count == 0`; no
   secret/signed-URL echo. The allowed path may send exactly once.
6. **Egress guard**: the guard actually makes external socket/HTTP calls fail
   and does not exempt the code under test; loopback PG/local tools only.
7. **PDF/audit provenance**: real Typst/Poppler V5/V6, exact evidence
   reference form, reviewer/approver lineage hidden from the rendered names,
   chronology exactly once/in order.
8. **Global gates**: verify the candidate's reported results by rerunning at
   least `make lint-backend`, `make typecheck-backend`, `make test-backend` and
   the two E2E drivers; confirm `make test-integration`, `test-workflow`,
   `test-workflow-full`, `test-ci-scripts` results are not claimed if unrun.
   Any SKIPPED/BLOCKED is not PASS.
9. **Scope/non-claims**: only the new adversarial test file (plus declared
   additive helper); no A/B/C/application changes; S3/L22 and Advisor cutover
   remain blocked and were not tested/implemented.

## Verdict

`APPROVED` or `REVISE` (≤60 lines) with path:line findings, severity, commands
actually run, gate table, and residual risks. A partial result is labelled
partial; never converted to PASS.
