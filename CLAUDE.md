@AGENTS.md

Claude Code: use the shared repository instructions above. Before editing
inside a component, read its applicable nested `AGENTS.md` (including deeper
ones). Do not copy them into this file.

When Paseo is the parent orchestrator, prefer its isolated sessions/worktrees.
Use Claude-native subagents only for bounded work assigned within this session;
do not create a second unsupervised fleet.

Project `.claude/settings.json` provides defense-in-depth deny rules, not a
complete sandbox. Verify actual permissions before handling sensitive data.
