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

Paseo is the outer coding orchestrator when enabled. **Prefer GPT-6 Luna
high sub-sessions for bounded, independent tasks**, even mechanical file
edits, reproducible checks, focused comparisons and scoped source mapping:
these are useful places to keep noisy intermediate context away from the
parent. Use **Luna xhigh** for high-risk code, difficult contradictions,
G2/TLP, migrations, provenance and independent review.

Concurrent agents: start at up to **6** independent workers; consider
**8** only after measuring rate limits, time, cost, worktree and test
contention. A ceiling is not a task quota. Keep a single parent integrating
changes. Don't create overlapping write owners or concurrently mutate live
state, and do not nest unlimited trees of Codex/Claude agents inside Paseo.

Configure actual provider/model IDs in **Paseo Agent Profiles on the host**.
Repository `.codex/config.toml` separately enables up to 6 native Codex
helpers defaulting to `gpt-6-luna` high. This is a fallback, not proof
that Paseo loaded the settings. Claude-native subagents run Claude models;
for Luna, start a Paseo Codex worker.

| Profile | Preferred model/effort | Access | When |
|---|---|---|---|
| coordinator | GPT-6 Luna high/xhigh | scoped integration worktree | planning, parent decisions |
| scout | GPT-6 Luna high | actually read-only | trace code/evidence, map APIs |
| mechanical | GPT-6 Luna high | isolated write worktree | small repeatable changes |
| implementer | GPT-6 Luna high | isolated write worktree | bounded implementation |
| tester | GPT-6 Luna high | disposable test environment | one oracle, concise results |
| critical-reviewer | GPT-6 Luna xhigh | actually read-only | security, G2, migrations, provenance |

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

## Fresh sessions and context pressure

A child starts with its own narrow context; its output does not copy its
transcript into the parent. **Start a new session proactively**, not just
after forced compaction: after one completed meaningful lot, switching to a
different subsystem, after a log-heavy investigation, or once unrelated
history begins to crowd out the current task. If context usage is visible,
~50–60% occupancy is a cue to evaluate a handoff, never a hard threshold or
permission to omit evidence. At any earlier natural boundary, prefer a
fresh session if continuation would require remembering substantial obsolete
history.

Use `autowork-context-handoff`: commit/check current worktree, record task
ID, exact base/output SHA, relevant contracts, paths:lines, actual oracle
results, blockers, permissions and next step. The receiving **new** session
re-verifies the SHA and reads only relevant instructions/evidence pointers.
Do not merely ask the same bloated thread to "forget". If the runtime can't
launch sessions, finish with a handoff; don't claim a handoff occurred.

Track Codex/Claude/Paseo costs independently of Bridge, VT and AutoWork
worker costs; report unknown values as UNKNOWN. Delegate safely, never
disclose private CTI evidence to an unapproved provider.

## Ralph loops: two distinct mechanisms

- **Micro repair**: only an offline deterministic oracle; fail ->
  minimal patch -> narrow test -> diff review. Maximum 3 attempts per oracle.
  After 2 without improvement, independent Luna xhigh diagnosis.
- **Long mission**: one persisted task registry; pick one unblocked lot,
  launch a fresh Luna high session, implement and verify its oracle,
  record DONE/FAIL/BLOCKED/UNKNOWN with output SHA, and start another
  fresh session for the next independent lot. No unbounded self-scored loop.

Never loop ambiguous `needs_review`, live CTI effects, provider uploads,
publication, G2/G3 approvals, destructive database work, or non-deterministic
judgments. Read `autowork-ralph-gate` only when a gated loop applies.

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
