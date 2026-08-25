# AutoWork / CTI Bulletin

Monorepo.

Before modifying files under `backend/`, `frontend/`, or `chatgpt-bridge/`,
read that area's `AGENTS.md` once. Do not read instructions for unaffected
areas.

## Architecture invariants

- Canonical state lives in PostgreSQL, versioned files, and evidence packs.
- Materialized workspaces, LLM conversations, Redis files, and external
  service responses are never sources of truth.
- Document bodies live in MinIO. PostgreSQL stores metadata and SHA-256
  references only.
- Destructive actions require a verified external identity.
  Never rely on title, position, DOM index, or visual similarity.

## Code navigation

Use the locator before broad repository exploration:

    uv run scripts/ctx/ctx.py query "<task description>" -k 8

Read the returned ranges first, adding only small surrounding context when
needed.

If results are insufficient:
1. Reformulate the query once using known domain identifiers.
2. Then use targeted `rg -n` inside the most likely subtree.

The locator automatically falls back to its stdlib-only lexical mode when
dense dependencies, credentials, or the embedding service are unavailable.
The lexical index is refreshed automatically when sources changed.

Do not start with repository-wide `grep`, `find`, `tree`, or bulk file reads.
The index is a locator, not a source of truth: inspect real code before edits.

## Commands

    make up | down | status | logs
    make test | test-integration
    make lint | typecheck | format

Use the narrowest relevant test/check first. Run repository-wide checks only
when the change crosses components or before final validation when justified.

## Subagents

Subagents are optional, not the default.

You may spawn `scout` only when `ctx.py` plus one targeted search still leaves
the implementation location unclear.

You may spawn `tester` only when validation can run independently from the
main task.

Do not spawn subagents for routine navigation or small changes.
Use at most one helper at a time unless the user explicitly requests
parallel work.

## Never inspect

`.git`, `.mypy_cache`, `.pytest_cache`, `.ruff_cache`, `node_modules`, `var`,
`chatGPT_Answers`, `.secrets`, `.llms_key`, `.env`, `.roo`, `.roomodes`.

## Output

Keep the final response concise.

Report:
- tests/checks actually run;
- failures or blockers, if any.

Do not reproduce code that was just written and do not print the full diff
unless explicitly requested.
