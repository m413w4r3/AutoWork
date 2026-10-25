# M05-F brief — bounded fix: missing evidence ref must be a typed refusal

## Role and workspace

You are the sole implementer of lot **M05-F**. Work in your own Paseo worktree
branched from local `main` at the SHA given by the parent. Commit atomically
on your branch; do not merge, rebase or push `main`. Do not touch E2E drivers,
parsers, the v4 cutover, S3, budget/transport policy, or frontend.

Read first: root `AGENTS.md`, `backend/AGENTS.md`,
`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` (TARGET_A / security),
`docs/mission05-phase3/_work/briefs/M05A_BRIEF.md` (byte-parity invariants).

## Defect (reproduced by M05-C, parent-confirmed by source reading)

`InvestigationAdvisorTool.execute` calls `_load_evidence(...)` around
`investigation_advisor_tools.py:413`. `_load_evidence` raises `ValueError`
("Evidence reference is absent or belongs to another case") when a supplied
UUID does not exist. Execution then reaches the handler
`except (KeyError, TypeError, ValueError)` at `:1002`, which evaluates
`tuple(item.id for item in evidence)` even though `evidence` was never
assigned. Python raises `UnboundLocalError`, which is caught by the generic
handler and surfaced as `tool_execution_failed`, not as a typed pre-transport
refusal. A request with one valid observation plus one nonexistent UUID is
therefore misclassified; the fake transport records no send, but the operator
gets an internal failure instead of a definitive input rejection.

## Required outcome

- A supplied evidence reference that is absent (or belongs to another
  investigation) must produce a **typed pre-transport refusal**:
  `advisor_status == "invalid_input"`, `error_code == "advisor_input_invalid"`,
  a stable input reason (prefer `evidence_refs_invalid` or the existing
  `advisor_input_rejected` if that is the established contract),
  `confirmed_non_execution == True`, `provider_usage == {"status":
  "not_submitted"}`, and **zero transport calls**.
- No valid-path prompt byte, SHA, refusal, alias, cost or dossier field may
  change. The anti-slop advisor prompt SHA must stay
  `54c9f454f0e80c95e518279c6a3205a776714db84964039a1fdaa0c7ebecafab`.
- The fix must not merely catch `UnboundLocalError`. Prefer a minimal
  initialization/guard so every exception before `evidence` binding is safe,
  and check whether the same pattern exists in the other handlers around
  `:1031`, `:1056`, `:1073`.

## Mandatory sequence

1. **RED**: add a test in `backend/tests/test_investigation_advisor_tools.py`
   (reuse the existing harness of a nearby test) that calls the tool with
   `evidence_refs=[<valid observation UUID>, <random missing UUID>]` and
   asserts the typed refusal above and `transport.calls == []`. Prove it fails
   at the base commit with `UnboundLocalError`/`tool_execution_failed`.
2. **Minimal patch** in `investigation_advisor_tools.py`. Keep the change
   local to exception safety/refusal typing; no refactor of the send path.
3. **GREEN**: the new test passes; full advisor suites pass; bytes/SHA parity
   unchanged.
4. **Deletion/no-regression check**: the existing invalid-measurement,
   disclosure-restriction, refusal and valid-send tests keep their exact
   statuses; the golden corpus still passes.

## Environment and commands

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
export UV_CACHE_DIR=/tmp/autowork-m05f-uv
cd backend
uv run --no-sync --python 3.12 pytest tests/test_investigation_advisor_tools.py -q --tb=short -p no:cacheprovider
uv run --no-sync --python 3.12 pytest \
  tests/test_advisor_prompt_golden.py tests/test_advisor_consultation.py \
  tests/test_advisor_wire.py tests/test_advisor_completeness.py \
  tests/test_advisor_anti_slop_eval.py tests/test_operator_tool_invocation.py \
  -q --tb=short -p no:cacheprovider
uv run --no-sync --python 3.12 ruff check src/cti_app/application tests/test_investigation_advisor_tools.py
uv run --no-sync --python 3.12 ruff format --check src/cti_app/application tests/test_investigation_advisor_tools.py
uv run --no-sync --python 3.12 mypy src/cti_app/application/investigation_advisor_tools.py
```

No network, no provider/Bridge/CTI call, no live DB, no secret read. Do not
weaken a test or skip the RED. Do not change v3/v4 parsing semantics.

## Deliverable (≤60 lines)

- Base/commit SHAs, files changed, exact RED failure (exception/line) and
  GREEN results with counts.
- The typed refusal observed (fields/values) and the zero-send proof.
- Byte/SHA parity evidence and anti-slop SHA.
- Any other handler with the same unbound-local risk (fixed or reported).
- Residual risks and next permissible action. Never claim an unrun test.
