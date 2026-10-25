# M05-A brief — deep Advisor preparation (TARGET_A)

## Role and workspace

You are the sole implementer of lot **M05-A**. Work only in your Paseo
worktree (branched from local `main@55fba67d5882c1ae6c4abff4e74c53a24e57649f`).
Commit atomically on your own branch. **Do not merge, rebase onto, or push
`main`; do not push anything.** The parent integrates after independent review.

Read first: root `AGENTS.md`, `backend/AGENTS.md`,
`docs/mission05-phase3/ARCHITECTURE_DECISIONS.md` (TARGET_A and TARGET_B
sections only), `docs/mission04-closure/L22_ADOPTION_PLAN.md`.

## Objective (single)

Make the Advisor *preparation seam* deep: **one pure prompt renderer** shared
by the historical tool path and by `advisor_consultation.prepare()`, with no
dependency from preparation onto `InvestigationAdvisorTool`. Byte-for-byte
behaviour must be preserved. No v4 cutover, no send-path change, no S3 work.

## Observed starting facts (verify line numbers before editing)

- `backend/src/cti_app/application/advisor_consultation.py:144` `prepare()`;
  the illegitimate private call is at `:196-202`:
  `from ...investigation_advisor_tools import InvestigationAdvisorTool` then
  `InvestigationAdvisorTool._prompt(consultation_type, prompt_data)`.
- Renderer: `backend/src/cti_app/application/investigation_advisor_tools.py:3582`
  `InvestigationAdvisorTool._prompt` (static). Helpers near `:5352`
  (`_advisor_task_description`), `:5364` (`_humanize_advisor_label`),
  `:5382` (`_advisor_wire_output_contract`). Wire helpers already live in
  `advisor_wire.py` (`ADVISOR_WIRE_VERSION`, `advisor_wire_aliases`,
  `render_advisor_wire_input`).
- Real send path renderer call: `_fit_advisor_prompt` at `:4587`, called from
  `:296` and the authoritative submission path `:726`.
- Tests currently calling the private `_prompt`: `test_advisor_anti_slop_eval.py`
  (`:281`, `:373`), `test_advisor_wire.py:398`, `test_advisor_completeness.py:752`.
  `test_advisor_consultation.py` exercises `prepare()`.

## Mandatory sequence

1. **GOLDEN capture before any source edit.** Add a golden corpus test (new
   file, e.g. `backend/tests/test_advisor_prompt_golden.py`) and record the
   *current* outputs. Cover all seven consultation types
   (`plan_investigation`, `pivot_strategy`, `yara_draft`, `review_text`,
   `draft_text`, `suricata_draft`, `figure_plan`), multi-unit `units`, legacy
   `review_text` mapping, aliases and exact source refs/version/SHA, Unicode
   (accents + CJK), CRLF vs LF, CTI text that looks like JSON, Base64/hex
   fragments, restricted evidence/`external_llm_allowed=false`/TLP RED/
   `do_not_submit` projections, a `PreparationRefusal` case, and a `partial`
   completeness case if reachable through `prepare`. Record expected full text
   for small cases and expected UTF-8 SHA-256 + byte length for every case.
   Also assert legacy-path prompt bytes, `len(utf8)`, token formula
   `max(1,(len+3)//4)`, and `prepare()` bytes/SHA agree on the same final
   `prompt_data`.
2. **RED.** Add a test proving the current inversion: preparation imports or
   resolves `InvestigationAdvisorTool` (e.g. AST/source assertion plus a
   monkeypatch that makes the private method raise while calling `prepare`).
   It must fail on baseline for the inversion reason, not for missing fixtures.
3. **Refactor.** Move the real renderer implementation and only the helpers it
   needs out of `InvestigationAdvisorTool` into a pure module under
   `application/` (prefer a dedicated module only if the body is substantive;
   no cosmetic wrapper). `InvestigationAdvisorTool` imports and delegates to
   it; `_fit_advisor_prompt` uses it; `prepare()` uses it and imports nothing
   from `investigation_advisor_tools`. No new I/O, settings, env, HTTP, budget,
   storage, or policy decision inside the renderer. A one-line legacy alias
   `_prompt` may remain, but there must be exactly one renderer engine.
4. **GREEN.** Golden corpus unchanged (bytes/SHA), plus the existing advisor
   test files. Update legacy tests that called `_prompt` to the new public
   renderer where that does not weaken them.
5. **Deletion test.** Prove renderer independence: monkeypatching
   `InvestigationAdvisorTool._prompt` to raise still leaves `prepare()` and the
   new renderer working; an AST/source check proves `advisor_consultation` and
   the new module never import `investigation_advisor_tools`.

## Hard constraints

- **Zero byte changes.** If any user-visible prompt byte, SHA, refusal, alias,
  cost or dossier field changes, STOP and report; do not silently accept a
  difference. A genuine bug needs a separate lot, version bump and review.
- Do not touch: v3/v4 parsers (`advisor_semantic_parser.py`,
  `_parse_advisor_wire_legacy`), the submission/transport path, budget or
  reservation logic, S3, `investigation_service.py`, `investigation_tools.py`,
  E2E/integration files, frontend.
- No network, no provider/Bridge call, no `.env`/secret read, no live DB.
- Keep the public interface small. Do **not** convert every mapping to a
  dataclass. A typed input is allowed only if it demonstrably removes caller
  obligations and preserves all behaviour; otherwise keep `prepare(projection)`
  and report why.
- Do not pass an object named "authorized"/"allowed": preparation does not
  decide egress authorization.

## Validation commands (run from your worktree root)

```sh
export UV_PROJECT_ENVIRONMENT=/home/nill/perso/work/AutoWork/backend/.venv
cd backend
uv run --no-sync --python 3.12 pytest \
  tests/test_advisor_prompt_golden.py tests/test_advisor_consultation.py \
  tests/test_advisor_wire.py tests/test_advisor_completeness.py \
  tests/test_advisor_anti_slop_eval.py tests/test_operator_tool_invocation.py \
  -q --tb=short
uv run --no-sync --python 3.12 pytest tests/test_investigation_advisor_tools.py -q --tb=short
uv run --no-sync --python 3.12 ruff check src/cti_app/application tests/test_advisor_prompt_golden.py
uv run --no-sync --python 3.12 ruff format --check src/cti_app/application tests/test_advisor_prompt_golden.py
uv run --no-sync --python 3.12 mypy src/cti_app/application/advisor_consultation.py <new renderer module>
```

Do not use bare `python`/`pytest`/`ruff`/`mypy`. Never hide a failure.

## Deliverable (final message, ≤80 lines)

- New module + files changed (paths), branch commit SHA, clean status.
- Renderer/interface before vs after: module dependency counts, public
  entrypoints callers must learn, objects callers must compose, duplication
  removed, interface size (params/fields/invariants), test paths through the
  public interface.
- Golden corpus: case count, exact SHA-256 coverage, proof no byte changed.
- RED evidence, deletion-test evidence, exact commands and observed results
  (PASS/FAIL/SKIPPED/BLOCKED, counts; durations only if measured).
- Deviations, residual risks, and the next permissible action. No claims
  beyond tests actually run.
