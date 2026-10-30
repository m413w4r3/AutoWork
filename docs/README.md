# AutoWork — documentation authority index

Updated for coding agents, 2026-10-23. This index is **navigation and
precedence**, not an assertion that every historical statement or currently
deployed environment was revalidated. Review actual code, schemas, runtime
capabilities, migration state and tests before asserting operational behavior.

## Precedence and classification

An `AGENTS.md` is an instruction for scoped coding work, not a database schema,
G2 approval or CTI evidence. Enforced permissions and server-side guards
cannot be relaxed by any document. For architectural decisions, use accepted
ADRs and current versioned contracts/migrations, reconciled with current code
and tests. When these disagree, **stop and report the conflict**; do not
silently pick the most convenient text. Operator decisions and versioned
observations are canonical only in their authorized application stores.

- `ACTIVE_AUTHORITATIVE`: current scope-specific decision or agent instruction;
  still subject to actual tested implementation and security controls.
- `ACTIVE_REFERENCE`: useful current-oriented explanation/playbook; check
  endpoints, versions and live status against code before acting.
- `HISTORICAL`: dated run, proposal, deprecated workflow or prior configuration;
  never normative for current mutations.
- `OBSOLETE_CONFIRMED`: legacy instruction, duplicate, or superseded plan
  with no current runtime dependency found during a focused reference audit;
  removed only with a Git-history recovery pointer.
- `EXTERNAL_REFERENCE`: provider documentation external to this repository,
  version-specific and non-normative for AutoWork domain behavior.
- `UNREVIEWED`: other material not explicitly assessed here; **not** an
  authorization, normative contract or reason to bypass current code.

## Active authoritative decisions and instructions

| Scope | Document | Status |
|---|---|---|
| Repo-wide coding policy | [`../AGENTS.md`](../AGENTS.md) | ACTIVE_AUTHORITATIVE |
| Backend incl. schema policy | [`../backend/AGENTS.md`](../backend/AGENTS.md) | ACTIVE_AUTHORITATIVE |
| Frontend | [`../frontend/AGENTS.md`](../frontend/AGENTS.md) | ACTIVE_AUTHORITATIVE |
| Discovery code ownership | [`../backend/src/cti_app/application/discovery/AGENTS.md`](../backend/src/cti_app/application/discovery/AGENTS.md) | ACTIVE_AUTHORITATIVE (scoped) |
| ORM module ownership | [`../backend/src/cti_app/infrastructure/database/models/AGENTS.md`](../backend/src/cti_app/infrastructure/database/models/AGENTS.md) | ACTIVE_AUTHORITATIVE (scoped) |
| Repository module ownership | [`../backend/src/cti_app/infrastructure/database/repositories/AGENTS.md`](../backend/src/cti_app/infrastructure/database/repositories/AGENTS.md) | ACTIVE_AUTHORITATIVE (scoped) |
| Frozen baseline / additive C2 migration | [`adr/ADR-0007-frozen-baseline-and-c2-migration.md`](adr/ADR-0007-frozen-baseline-and-c2-migration.md) | ACTIVE_AUTHORITATIVE |
| Exact DB DDL | [`../backend/migrations/versions/0001_baseline.py`](../backend/migrations/versions/0001_baseline.py) and [`0002_c2_experiments.py`](../backend/migrations/versions/0002_c2_experiments.py) | VERSIONED_CODE; inspect before any change |

`0001_baseline` is frozen. `0002_c2_experiments` is additive and handles only
the explicitly validated pre-existing states described in ADR-0007. Never
assume all installations are empty; never run live migration/downgrade without
separate approval and state/backup preflight.

## Active references (read when the task requires them)

| Topic | Entry points | Status |
|---|---|---|
| Production and publication | [`workflow.md`](workflow.md), [`production.md`](production.md), [`model_conversations.md`](model_conversations.md) | ACTIVE_REFERENCE |
| CTI contracts, governance and API | [`investigation_contracts.md`](investigation_contracts.md), [`investigation_governance.md`](investigation_governance.md), [`investigation_api.md`](investigation_api.md) | ACTIVE_REFERENCE |
| Bridge advisor consultation preflight | [`advisor_preflight_catalog.md`](advisor_preflight_catalog.md) | ACTIVE_REFERENCE; reconcile with advisor code and G2/TLP guards |
| CTI tools and loop | [`investigation_agent_loop.md`](investigation_agent_loop.md), [`investigation_operations.md`](investigation_operations.md), [`investigation_local_tools.md`](investigation_local_tools.md), [`investigation_rule_tools.md`](investigation_rule_tools.md), [`investigation_vt_tools.md`](investigation_vt_tools.md) | ACTIVE_REFERENCE; verify versions |
| Operator-led workflows | [`operations/operator-playbook-medium.md`](operations/operator-playbook-medium.md) | ACTIVE_REFERENCE; verify deployment |
| Coding orchestration | [`operations/paseo-agent-orchestration.md`](operations/paseo-agent-orchestration.md) | ACTIVE_REFERENCE (recommended setup, not verified host config) |
| Source collection | [`source_collection_and_evidence.md`](source_collection_and_evidence.md) | ACTIVE_REFERENCE; older workflow statements may be historical |
| Other accepted ADRs | [`adr/`](adr/) | ACTIVE_REFERENCE unless expressly superseded; dated decisions must be interpreted in sequence |

## Historical documents and intentional deletions

The 2026-10-23 coding-agent cleanup deleted these non-authoritative files
(after checking known link consumers) so agents cannot apply their obsolete
instructions. They remain recoverable from Git history at
[the pre-cleanup commit](https://github.com/m413w4r3/AutoWork/tree/1e825f84f08e806df495fc17c937ab19ba696b4c):

- `docs/agent/CONTRACT.md`: obsolete M2/M3 migration directions.
- `docs/operations/operator-sessions-config.md`: Nimbalyst-specific launcher.
- `docs/operations/investigation-supervised-first-run.md`: obsolete initial reset.
- `docs/operations/operator-led-investigation-design.md`: early design snapshot.
- `docs/brief_workflow.md`: superseded AW-008 workflow.
- `.agents/skills/caveman/` and `skills-lock.json`: obsolete style skill.

**Retained historical evidence**: `operations/run12-editorial-closure-2026-10-22.md`
and dated run journals/incidents/ADRs. They document earlier states, not
instructions for current mutations. Do not delete CTI evidence/incident
records solely because they are historical. Dated design documents are
historical by default unless superseded or explicitly accepted.

## Pre-Mission-00 removal sweep (2026-10-23)

**Scope:** 98 tracked `docs/` files on
[`2b3df6d286cc364a17e1d5072f3b1248bd836394`](https://github.com/m413w4r3/AutoWork/tree/2b3df6d286cc364a17e1d5072f3b1248bd836394).
Ten files were deleted after checking known in-repo text references.
Any previously cited paths in retained journals were replaced with
immutable Git-history links or active source/ADR pointers. This is **not**
proof that no external/private consumer exists.

| Removed path (relative to `docs/`) | Why removed |
|---|---|
| `Architecture.txt` | Obsolete technology outline duplicating root README/accepted ADRs |
| `retex_01.md` | Orphaned Pandoc version output, not a retrospective |
| `design/09-invariant-thresholds.example.md` | Unused `CHOOSE_ME` example superseded by the frozen thresholds decision |
| `design/2026-10-05-plan-corrections-rendu-cti.md` | Stale executable work plan superseded by later run records and current code |
| `design/2026-10-12-baseline-collapse-live-db-note.md` | Legacy mutable-baseline/restamp playbook superseded by ADR-0007 |
| `design/2026-10-12-multibatch-composition-deployment-note.md` | Legacy migration/deployment instructions superseded by ADR-0007 |
| `design/2026-10-12-dead-code-audit.md` | Completed one-off code-removal candidate list (Git history remains) |
| `design/2026-10-12-dead-code-removals.md` | Completed removal log/candidate rationale (Git history remains) |
| `design/2026-10-12-deliverable-inspection-audit.md` | Exploratory feasibility plan replaced by `design/2026-10-12-pdf-inspection-api.md` and code |
| `design/extraction-wire-format-recovery.md` | Superseded actionable recovery plan; current parser code + retained dated audit remain |

See [the design index](design/README.md) for retained contracts vs historical
audit evidence. **Do not delete** run journals, SHA-bearing observations,
ADR decisions or migration files merely for age. Obsolete instructions in
retained dated journals are still historical **data**, not steps to execute.

Other docs may remain `UNREVIEWED`: this pass checked the candidate
documents and known in-repo text references, not every file's runtime
consumer or every private archive. A focused review after Mission 00 may
identify further safe deletions, but must preserve the evidence needed to
understand Run12.

## Agent skills

Six narrowly triggered, on-demand workflows live in `.agents/skills/`:
`autowork-context-handoff`, `autowork-ralph-gate`,
`autowork-run-audit`, `autowork-bridge-dossier-review`,
`autowork-cti-evidence-gate`, `autowork-typst-parity`.
Claude Code discovers symlinks to these same skills from `.claude/skills/`
when supported by its installed version; no duplicated skill bodies.
Follow the [Paseo coding orchestration guide](operations/paseo-agent-orchestration.md)
for profiles, Luna high/xhigh delegation and fresh context.

## Curated engineering skills

Seven pinned, adapted Matt Pocock skills are listed in
[the local skills index](../.agents/skills/README.md): `code-review`,
`diagnosing-bugs`, `to-tickets`, `implement-spec`, `retro`,
`domain-modeling` and `writing-for-agents`. They supplement, not
supersede, AutoWork's CTI governance and accepted ADRs. They only become
available in an agent session that loads the corresponding project files.

## External references

- [Codex custom subagents](https://learn.chatgpt.com/docs/agent-configuration/subagents) — EXTERNAL_REFERENCE.
- [Claude Code memory and AGENTS.md](https://code.claude.com/docs/en/memory) — EXTERNAL_REFERENCE.
- [Claude Code subagents](https://code.claude.com/docs/en/sub-agents) — EXTERNAL_REFERENCE.
- [Paseo orchestration](https://paseo.sh/docs/orchestration) — EXTERNAL_REFERENCE.

## Safety note for future missions

The local Mission 00 audit (`RUN12_LIVE_AUDIT.md`,
`BRIDGE_INPUT_COMPLETENESS.csv`, `LIMITS_REGISTER.csv`, etc.) is **not**
present in the reviewed `main` tree at the stated base SHA. It may exist in
private archives or worktrees. Treat its status as UNKNOWN, not PASS. Do not
launch mutating phases B/L/A/T as a consequence of this docs cleanup.
