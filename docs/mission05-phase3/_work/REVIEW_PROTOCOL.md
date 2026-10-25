# Mission 05 — independent review protocol

Every lot is reviewed by a Paseo **Luna xhigh** agent that did not implement it.
Reviewers are read-only: no source edits, no commits, no merges. They run their
own commands against the candidate worktree/commit and return a verdict
`APPROVED` or `REVISE` with evidence.

## Inputs given to a reviewer

- Lot ID, objective and the accepted brief (`_work/briefs/`).
- Base SHA and candidate commit SHA (exact).
- Exact files in scope and the invariants that must not change.
- The tests the implementer claims to have run (to be re-run or spot-checked).
- Explicit prohibition: no external/provider/CTI call, no live DB/MinIO, no
  secret inspection.

## Review axes

1. **Spec conformance** — the lot does what the V3/decisions require, no more
   and no less. Scope creep, silent behaviour change, or a new parallel
   framework is `REVISE`.
2. **Invariants** — canonical evidence, provenance, versions, CAS/idempotency,
   G2/G3/TLP, `external_llm_allowed`, `do_not_submit`, provider boundaries,
   refusals and zero-egress. Any weakening is `REVISE`.
3. **Depth (codebase-design)** — small interface vs rich implementation,
   locality, leverage, deletion test, one source of truth. A cosmetic wrapper,
   a pass-through file, or a public surface larger than needed is `REVISE`.
4. **Byte/behaviour parity** — where the lot claims parity, the reviewer
   independently re-runs the golden/parity oracle, ideally by replaying the
   golden test at the base SHA and at the candidate SHA.
5. **Honesty** — commands/results in the report match the observed output;
   skipped/blocked tests are labelled, never reported as PASS.
6. **Egress and secrets** — source-level and runtime checks that no external
   client is constructed/called, no dotenv/secret read, no live endpoint.

## Verdict format (≤60 lines)

```text
LOT: M05-X
BASE: <sha>   CANDIDATE: <sha>
VERDICT: APPROVED | REVISE
- Findings (path:line, severity: BLOCKER/MAJOR/MINOR, fact vs inference)
- Commands the reviewer actually ran + results
- Invariants checked + evidence
- Residual risks / missing proofs
- Required corrections (if REVISE)
```

A lot with any `BLOCKER` or unresolved `MAJOR` cannot be `APPROVED`. A `REVISE`
requires a corrected candidate and a fresh review (same reviewer is acceptable
for the follow-up, provided it did not implement).
