# Mission 05 — architecture implementation and deletion test

Status: **M05-A INTEGRATED on local main at `b8a10165`.** Only what the
integrated commit and executed tests prove is recorded here.

**Closure pass (2026-10-25, `4111acf3`):** no architecture change. The closure
diff `a30a7c22..4111acf3` touches only three integration test files; the
renderer, preparation interface, byte corpus and deletion test below are
unchanged.

## Modules before / after

| Aspect | Before (`55fba67d`) | After (M05-A `2e7733be`) |
|---|---|---|
| Renderer implementation | `InvestigationAdvisorTool._prompt`, a static method inside the 6,892-line `investigation_advisor_tools.py` | `render_advisor_prompt()` in `advisor_prompt.py` (228 lines, pure) |
| Preparation dependency | `advisor_consultation.prepare` imported `InvestigationAdvisorTool` **inside** `_prepare_projection` (`advisor_consultation.py:196-202`) | module-level import of `render_advisor_prompt` only |
| Legacy path | `_fit_advisor_prompt` → `InvestigationAdvisorTool._prompt` | `_fit_advisor_prompt` → `render_advisor_prompt` (`investigation_advisor_tools.py:4501`) |
| Legacy compatibility | — | `_prompt = staticmethod(render_advisor_prompt)` alias (`:3583`), one engine, no duplicate body |
| Rendering helpers | `_advisor_task_description`, `_humanize_advisor_label`, `_advisor_wire_output_contract` in the tool module | moved to `advisor_prompt.py`; old definitions removed |
| Public preparation interface | `prepare(projection) -> PreparedConsultation \| PreparationRefusal` | unchanged |

## Direct imports and dependency direction

- Before: preparation → `investigation_advisor_tools.py`, which has 13 direct
  application-module imports and orchestration/transport/DB dependencies.
- After: `advisor_consultation.py` → `advisor_prompt.py` → `advisor_wire.py`
  only. The renderer module imports no tool, settings, storage, transport,
  budget or policy module.
- AST/source checks in `test_advisor_prompt_golden.py` assert:
  - `advisor_consultation` contains no `InvestigationAdvisorTool` name;
  - neither `advisor_consultation` nor `advisor_prompt` imports a module ending
    in `investigation_advisor_tools`.

## Caller obligations and interface size

| Question | Before | After |
|---|---|---|
| Public entries a caller must know for rendering | private `InvestigationAdvisorTool._prompt` (reachable only through the orchestration class) | `render_advisor_prompt(consultation_type, data)` |
| Entries for preparation | `prepare(projection)` + the hidden tool dependency | `prepare(projection)` only |
| Objects/invariants the caller must compose | dossier, aliases, refs, bytes/SHA if using the tool path directly | unchanged for `prepare`: it absorbs dossier, aliases, refs, completeness, bytes and SHA |
| Renderer duplication | one body, but only reachable through the legacy class | one body shared by both paths |
| Tests through the public interface | tool tests called the private method | golden + parity tests call `render_advisor_prompt` and `prepare` |
| Interface learned invariants | private-class reachability, orchestration construction | 2 parameters, pure `str` result, no I/O, no policy decision |

## Byte parity and golden corpus

- Golden corpus: **15 prompt outputs** covering the seven consultation types,
  multi-unit and legacy `review_text`, exact opinion refs/version/SHA, aliases,
  Unicode + CRLF and LF, JSON-like CTI text, Base64/hex fragments, restricted
  projection flags, preparation success and refusal. Two cases assert the full
  expected text; all assert UTF-8 SHA-256 and byte length.
- Token formula `max(1, (len(bytes)+3)//4)` is checked against each byte length.
- `prepare()` bytes equal the legacy `_fit_advisor_prompt` bytes on the same
  final `prompt_data`, including the archived SHA, length and ref catalogue.
- Anti-slop advisor prompt SHA-256 remains
  `54c9f454f0e80c95e518279c6a3205a776714db84964039a1fdaa0c7ebecafab`
  (baseline and candidate).
- Refusal parity: `evidence_preflight_insufficient_evidence` keeps its
  prompt digest `e0e12d91…` and 1207 bytes.
- Partially: `AdvisorPreflightStatus` has no `PARTIAL` value; partial is a
  downstream response-parser status, so no partial-preparation case exists to
  capture. This is recorded, not hidden.

## Deletion test

- `advisor_consultation` and the renderer import the legacy tool nowhere.
- With `InvestigationAdvisorTool._prompt` monkeypatched to raise,
  `prepare()` and `render_advisor_prompt()` still succeed.
- The legacy private method is now a one-line delegation alias; deleting its
  body did not change any rendered byte, and the golden corpus remains valid.

## Executed evidence

| Command (candidate `2e7733be`) | Result |
|---|---|
| golden + `test_advisor_consultation.py` + `test_advisor_wire.py` + `test_advisor_completeness.py` + `test_advisor_anti_slop_eval.py` + `test_operator_tool_invocation.py` | PASS, 114 passed, 1 skipped (archived Run12 corpus absent) |
| `test_investigation_advisor_tools.py` | PASS, 125 passed |
| ruff check changed source/tests | PASS |
| mypy `advisor_prompt.py` + `advisor_consultation.py` | PASS |
| golden replay at baseline `55fba67d` (earlier 13-case corpus copy, no shim) | 4 passed, 2 failed — the failures are exactly the inversion tests |

## Independent review

- Reviewer: separate Paseo Luna xhigh session, read-only, candidate
  `2e7733be2c0a636b68cd82fb813d5e118f108380`.
- Verdict: **APPROVED** — 2 MINOR findings, no blocker:
  1. the baseline shim replay produced three architecture failures (the
     reviewer's enumeration had two); the third is
     `test_prompt_renderer_and_preparation_do_not_import_the_legacy_tool`,
     which correctly detects the baseline dependency;
  2. the AST check covers `ast.ImportFrom` but not `ast.Import`; the reviewer
     manually verified both forms are absent in the candidate.
- Reviewer's independent results: baseline shim replay 4 passed + 3 expected
  architecture failures; candidate golden 7 passed; full advisor set 114
  passed/1 skipped (worktree), 240 passed on integrated `main`; ruff and mypy
  clean; scope limited to the renderer/preparation files and tests.

## Residual risks

- The renderer remains a template assembled from `advisor_wire` helpers; future
  wire-version changes still require the same byte-corpus discipline.
- `prepare()` remains dormant (`PREP_ONLY`); no production cutover was made and
  `ADVISOR_CUTOVER_BLOCKED_BY_S3` is unchanged.
