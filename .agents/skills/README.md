# Eight curated engineering skills (AutoWork)

These are local **adapted copies** of selected skills from
[mattpocock/skills](https://github.com/mattpocock/skills), pinned at
[`5beb35b040e3de97e54fa9e386d7e42d15c295c2`](https://github.com/mattpocock/skills/tree/5beb35b040e3de97e54fa9e386d7e42d15c295c2).
Copyright (c) 2026 Matt Pocock. License: [MIT](UPSTREAM-MIT-LICENSE).
They are not automatically updated; review any upstream changes before
repinning. Installing the full upstream plugin in the same agent may create
duplicate skill names.

| Skill | Use |
|---|---|
| `code-review` | Independent standards/spec review, optional CTI safety axis |
| `diagnosing-bugs` | Minimal red-capable offline reproduction, bounded fix |
| `to-tickets` | Spec/mission to dependency DAG, explicit publication gate |
| `implement-spec` | Per-ticket fresh Luna worktrees and parent-controlled integration |
| `retro` | Improve CI, instructions and agent workflow from real evidence |
| `domain-modeling` | CTI terminology/glossary and proposed ADRs |
| `improve-codebase-architecture` | Read-only deep-module preflight, before/after candidates, no automatic refactor |
| `writing-for-agents` | Precise skills and lean AGENTS/CLAUDE references |

Already installed AutoWork skills such as `autowork-ralph-gate` and
`autowork-context-handoff` remain canonical for repair and handoff.
Upstream optional skills `to-spec`, `tdd` and
`setup-matt-pocock-skills` are not mandatory for these adaptations.

Codex reads `.agents/skills/` in supporting versions. Claude Code's
`.claude/skills/` directories point to the same files; no duplicate
content. Paseo launches whichever provider's process loads these paths:
skills do **not** create Paseo profiles. Start a fresh session after
checking out the branch; verify the installed skill inventory and model.

Only run actions allowed by `AGENTS.md` and actual sandbox permissions.
No remote plugin installation, shell hooks or unapproved ticket writes
have been introduced by this change.
