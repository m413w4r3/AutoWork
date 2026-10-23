@AGENTS.md

Claude Code: use the shared repository instructions above. Before editing
inside a component, read its applicable nested `AGENTS.md` (including deeper
ones). Do not copy them into this file.

When Paseo orchestrates coding, prefer its actual **GPT-6 Luna high/xhigh**
profiles and fresh isolated sessions, including for bounded mechanical tasks.
Claude-native subagents can use Claude models and are a fallback, not an
alternative way to launch Luna. Avoid a second unsupervised agent fleet.
For a long task, proactively hand off to a new Paseo session with exact SHA,
small task state and test oracle instead of repeatedly compacting.

Project `.claude/settings.json` provides defense-in-depth deny rules, not a
complete sandbox. Verify actual permissions before handling sensitive data.
