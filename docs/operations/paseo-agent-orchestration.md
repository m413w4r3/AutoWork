# Paseo orchestration for AutoWork coding work

Status: ACTIVE_REFERENCE — recommended setup, **not** a record of installed
Paseo version, effective provider permissions or enabled plugins.

## Boundary

Paseo orchestrates **code work** and worktrees. AutoWork's `operator_led`
and `bridge_led` are **application investigation modes**, not Paseo sessions.
Do not make the CTI backend depend on Paseo. No session transcript, plugin
report or workspace is primary CTI evidence.

## Host preflight before launching a coding mission

1. Verify the installed Paseo version, providers/models, profile selections,
   delegation tool names, workspace/worktree isolation and Git state. Do not
   assume `/paseo*`, CLI or MCP commands exist on the particular host.
2. Verify effective shell, file, network and secrets permissions. A textual
   READ-ONLY mandate does not sandbox a child. Paseo CLI/tool access plus a
   shell may allow further actions; isolate credentials and filesystem.
3. Verify base SHA, allowed paths, one task registry and explicit acceptance
   oracles. Check availability of Mission 00 live-audit artifacts before the
   separate V2 mutating phases; if absent, report BLOCKED/UNKNOWN.
4. Do not install plugins, enable access to live data or invoke real CTI
   providers without separate human authorization.

## Delegation policy

Paseo is the top-level coding orchestrator when enabled. Create independent
scout/reviewer sessions for questions that would pollute the parent context.
Use isolated worktrees for parallel coding tasks and a single parent for
integration. One child = one bounded question; no overlapping edits.
Avoid unconstrained nested Codex/Claude delegation inside each Paseo worker.

Suggested host-side profiles, **not executable project configuration**:

| Profile | Typical reasoning | Access | Use |
|---|---|---|---|
| lead | high, xhigh only when warranted | scoped coding worktree | coordinate, reconcile, verify |
| scout | low/medium | technically read-only | locate paths, symbols and contradictory evidence |
| implementer | medium/high | isolated writable worktree | small patch + focused oracle |
| critical-reviewer | high/xhigh where provider supports it | technically read-only | DB migration, G2, provenance, auth |
| tester | low/medium | isolated disposable environment | targeted tests and failures |

Use the *actual* provider/model IDs and effort levels advertised by Paseo.
A model or effort override does not grant permissions.

## Child task contract

TASK_ID: one question and why it matters
BASE: full commit SHA and versioned contract
SCOPE: exact 3–8 relevant paths, or specific evidence IDs/sections; expand
       deliberately if necessary, never silently suppress required evidence
ACCESS: read-only scout/reviewer; worktree-only implementer; never live mutation
INPUT: pointers + SHA + excerpts sufficient to resolve the question, no secrets
ORACLE: deterministic test, invariant, or explicit human review
STOP: blocking condition, scope mismatch, two no-improvement repairs, or
      three total repair attempts per deterministic oracle
OUTPUT: facts vs interpretation; paths:lines; patch/result SHA; actual tests;
        coverage gaps, cost observed or UNKNOWN; next recommendation

Parent retains task registry, reviews every patch, checks source links
and tests, and requests any necessary authorization. Do not import entire
child transcripts into the parent. Read targeted ranges on demand.

## Context and budgets

- Start from known paths or `scripts/ctx/ctx.py` as a **locator** only.
  Verify actual code and DB contracts. Navigation optimizes reading, not
  completeness of Bridge evidence.
- Pass immutable pointers, versions and hashes. Keep one compact registry:
  `TASK_ID / owner / deps / base SHA / scope / oracle / state / output SHA /
   measured cost or UNKNOWN / next`.
- On context compaction or fresh Ralph iteration, reload the registry, task
  specification, relevant AGENTS/ADR and exact evidence pointers; never use
  model memory or previous success claims as an oracle.
- Track Paseo/Codex/Claude coding costs separately from Bridge, VT and
  AutoWork workers. Never report estimated bytes/token conversion as
  observed provider billing.

## Ralph loops

Only repair against deterministic oracles, e.g. `focused failing test ->
small fix -> focused rerun -> diff inspection`. Maximum three attempts;
after two attempts without improvement, obtain independent diagnosis.
No automatic retries of ambiguous external effects (`needs_review`), no
live-provider experimentation, no automatic G3 or release.

## Exit gate

Report `DONE / FAIL / BLOCKED / UNKNOWN` per task with SHA, changed files,
tests actually executed, skipped tests, risks and approvals still necessary.
Passing offline tests does not establish live provider or CTI correctness.

## Where to configure

- Codex project defaults and optional fallback native workers:
  `.codex/config.toml`, `.codex/agents/`.
- Claude Code project safety settings and bounded native reviewers:
  `CLAUDE.md`, `.claude/settings.json`, `.claude/agents/`.
- Paseo **Agent Profiles** and daemon tool access: configured on the host,
  outside this repo. Do not pretend a repository TOML file is loaded by Paseo.
