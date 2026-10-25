# M05-A independent review brief

You are an independent **read-only reviewer**. You did not implement M05-A. Do
not edit files, commit, merge or push. Do not contact any external provider or
CTI source. Read `docs/mission05-phase3/_work/REVIEW_PROTOCOL.md` and the
implementer brief `M05A_BRIEF.md` first.

## Target

- Base: `55fba67d5882c1ae6c4abff4e74c53a24e57649f` (docs bookkeeping commit
  `0db6a904` is irrelevant to code).
- Candidate: the M05-A branch HEAD in
  `/home/nill/.paseo/worktrees/0y1qsxa1/m05a-advisor-deep`
  (`git -C <dir> rev-parse HEAD`).
- Work only by reading/running; you may run tests in that worktree with
  `PYTHONDONTWRITEBYTECODE=1 -p no:cacheprovider`.

## Mandatory independent checks

1. **Scope**: `git diff --stat 55fba67d..<candidate>` touches only
   `advisor_consultation.py`, one new pure renderer module, the renderer
   helpers in `investigation_advisor_tools.py`, and advisor tests/fixtures.
   Any parser, send path, budget/reservation, S3, integration/E2E or frontend
   change is a `BLOCKER`.
2. **One renderer**: the new pure module contains the real body; both
   `InvestigationAdvisorTool` (`_fit_advisor_prompt`/send path) and
   `prepare()` call it. `advisor_consultation.py` and the new module do not
   import `investigation_advisor_tools` (verify by AST/source, not grep alone).
   A cosmetic three-line wrapper around the old method is `REVISE`.
3. **Golden genuineness**: copy `backend/tests/test_advisor_prompt_golden.py`
   into a temporary baseline worktree at `55fba67d` and run it. Expected at
   baseline: the golden/parity/refusal tests PASS and exactly the two RED
   tests (`test_prepare_has_no_legacy_tool_reference`,
   `test_prepare_works_when_legacy_private_renderer_raises`) FAIL for the
   inversion. Then run the same file at the candidate: all must PASS.
   Report exact counts. Do not accept rewritten golden values unless a
   contract change was explicitly justified and reviewed (none is in scope).
4. **Byte parity**: run the advisor suites at the candidate and compare with
   the recorded baseline: `test_advisor_consultation.py`,
   `test_advisor_wire.py`, `test_advisor_completeness.py`,
   `test_advisor_anti_slop_eval.py`, `test_operator_tool_invocation.py`
   (baseline 108 passed) and `test_investigation_advisor_tools.py` (baseline
   125 passed). The anti-slop advisor prompt SHA must remain
   `54c9f454f0e80c95e518279c6a3205a776714db84964039a1fdaa0c7ebecafab`.
5. **No egress / purity**: the renderer does not read settings/env/dotenv,
   does file or network I/O, decide budget/authorization or submit. Existing
   no-I/O test must still pass; look for new imports that violate purity.
6. **Deletion test**: with the legacy private alias monkeypatched to raise,
   `prepare()` and the public renderer still work; no test depends on the old
   private engine. An internal alias is acceptable only if it delegates.
7. **Interface depth**: assess with codebase-design vocabulary (interface,
   implementation, depth, seam, locality, leverage, deletion test). Report:
   public entries a caller must know, objects/invariants the caller must
   compose, duplication removed, tests through the public interface, and
   whether the interface grew gratuitously. Line count alone is not proof.
8. **Honesty**: every claimed command was actually run; skipped/blocked tests
   are labelled. Any byte/SHA change without versioned justification is a
   `BLOCKER`.

## Commands

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export UV_CACHE_DIR=/tmp/m05a-review-uv
cd <candidate-worktree>/backend
uv run --no-sync --python 3.12 pytest <tests> -q --tb=short -p no:cacheprovider
```

Baseline worktree (create your own or reuse `/tmp/m05-baseline`):
`git worktree add --detach /tmp/m05a-baseline-review 55fba67d`.

## Deliverable (≤60 lines)

```text
LOT: M05-A
BASE: <sha>  CANDIDATE: <sha>
VERDICT: APPROVED | REVISE
Findings: path:line, severity, fact vs inference
Commands actually run + results
Byte/parity evidence + anti-slop SHA
Depth assessment + residual risks
Required corrections (if REVISE)
```
