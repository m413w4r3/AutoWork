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

## Applying this on Paseo 0.12.x / Codex 0.163.x

This is a **host setup checklist**, not executable repo configuration.
Paseo may have enabled orchestration skills but **zero agent profiles**.
Codex also distinguishes `~/.codex/config.toml` (user defaults) from the
project `.codex/config.toml` (only after the corresponding checkout/PR is
present). Do not assume work in a draft GitHub PR already changed a local
checkout.

### What is a profile, and who orchestrates?

A Paseo **Agent Profile** is a convenient saved bundle of launch settings:
provider, model, mode, thinking level, available features, and a `When to use`
note that the orchestrator can inspect. A profile is **not** an autonomous
agent, a new provider, a mandatory setup step or an execution policy. A
main/parent coding session becomes an *orchestrator* by using Paseo tools
or the installed orchestration skills and delegating actual work.

Profiles can be created manually in Paseo Desktop at
Settings -> host -> Agents -> Agent profiles -> New profile, or omitted:
when no matching profile is available, the Paseo orchestration skill can
discover configured providers/models and launch workers with explicit
settings. Prefer creating **three small worker profiles** rather than
duplicating every conceivable parent/worker role.

**User's preferred architecture (2026-10-23):** the coordinating session
uses either Claude Sonnet 5.5 medium/high (if the installed Claude provider
offers that model) or the existing `deepseek-claude` custom provider
(`deepseek-flash[1m]`, maximum offered thinking/effort). These are
**alternative parent sessions**, not Luna leads. The parent is responsible
for splitting tasks, dependencies, context freshness, permission boundaries,
integrating and checking outputs. GPT-6 Luna high/xhigh is preferred for
specialized **workers and independent review**.

| Optional profile | Provider ID | Selected model | Thinking | When to use |
|---|---|---|---|---|
| Luna focused | `codex` | `gpt-6-luna` | high | Independent small task, code tracing, mechanical change or focused offline test, fresh context |
| Luna implementer | `codex` | `gpt-6-luna` | high | Single bounded implementation, isolated writable worktree, verifiable oracle |
| Luna critical review | `codex` | `gpt-6-luna` | xhigh | Independent read-only review of G2/G3, TLP, migrations, provenance or security |

Optional **parent presets**, only if the user wants one-click launching:

| Parent preset | Provider ID | Model | Thinking |
|---|---|---|---|
| Orchestrator Sonnet | `claude` | Sonnet 5.5 *only if listed as available* | medium or high |
| Orchestrator DeepSeek | `deepseek-claude` | `deepseek-flash[1m]` | maximum *supported* by that provider |

The `deepseek-claude` provider already exists in the user's private
`~/.paseo/config.json`. **Do not duplicate it** as a new provider alias and
do not put credentials in the repo. The config's environment variable
`CLAUDE_CODE_EFFORT_LEVEL=max` does not by itself prove that Paseo or the
upstream model implements max reasoning; verify selectable options and
effective session metadata. Likewise do not invent a Sonnet model ID.


Choose compatible mode and effective permissions in the UI. The profile
notes are selection guidance, **not a sandbox**. Test that a spawned worker's
effective model and thinking appear in its actual session metadata.

Before delegation: use the bundled Paseo skills (`/paseo`,
`/paseo-handoff`, `/paseo-advisor`, `/paseo-committee`) instead of
reimplementing their CLI/MCP. Use AutoWork skills for **domain gates** and
SHA-aware handoff format; avoid two skills trying to create the same session.
When changing provider/tool profiles, new settings affect new launches, not
already running agents.

Choose compatible mode and effective permissions in the UI. Profile notes
are selection hints, not a sandbox, and they never start agents by themselves.
A new agent must actually be launched, and its runtime model/effort verified.

### Concurrency is a system budget

Six Codex-native threads per session and six Paseo agents do **not** mean
36 independent child sessions are desirable. Start with **2–4 Paseo workers**
plus one coordinating parent and independent review; raise to six **total**
active coding workers after measurement and no worktree/CI contention. When
Paseo is delegating, do not let its child agents launch additional fleets.
The Codex six-thread setting is mainly useful for standalone Codex work.

### Security and actual host configuration

- Review *effective* Codex sandbox/approval mode **before** launch.
  Codex subagents inherit parent permissions: a no-edit instruction does not
  establish read-only tool access. Give review workers an isolated workspace
  and an actually restricted permission mode when the host supports one.
- Paseo browser tools, when enabled, control workspace browser tabs that may
  hold login cookies. Keep disabled during sensitive CTI work. Turn them on
  temporarily for deliberately scoped frontend verification if needed.
- Only enable Paseo tool access to a provider after reviewing its permissions.
  Use provider-specific `paseoTools` restrictions if supported by the host.
- Relay access is optional and end-to-end encrypted; do not turn it off
  automatically if used for remote access. Bound daemon listen to loopback
  when operating locally. Verify CORS and pairing, never copy private keys.
- If provider configs include inline API tokens, keep them outside Git,
  restrict permissions on the local file and rotate any token actually
  disclosed. This repo must never track host `~/.paseo/config.json`.
- Keep personal Claude Code's historical command approvals **outside Git**.
  Project `.claude/settings.json` adds scoped denies, but cannot magically
  undo already permitted capabilities through other tools or agents.
  Use `/permissions` to verify the effective merged rules.
- If the user's tools are installed but project config is "No such file",
  verify `pwd`, `git branch --show-current` and the checkout; the draft
  PR's project files do not appear in `main` until checked out/merged.
- Do not force a numeric compaction token limit just to trigger freshness.
  Use explicit new sessions at bounded task transitions and SHA-based
  handoffs. Record actual token ledger measurements or UNKNOWN.

### Validation before calling this configuration ready

1. Spawn one Luna focused session and inspect effective model/effort/mode.
2. Spawn one read-only Luna critical reviewer with constrained tools; confirm
   it cannot write and that it reports missing permissions as BLOCKED.
3. Spawn two independent agents into different worktrees; verify no shared
   dirty checkout, no simultaneous live DB accesses and no runaway children.
4. Test `/paseo-handoff` to a **new** Luna high session, verifying exact
   base/output SHAs and a reproducible offline oracle.
5. Compare the same task under a solo Luna high agent, two Luna high
   subagents and independent xhigh review. Record defects, total cost,
   latency, attempts and session churn. Do not assume 6 is optimal.
