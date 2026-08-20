---
name: repo-context
description: Locate AutoWork code when the relevant files or symbols are unknown.
---

# Repository context

Use:

    uv run scripts/ctx/ctx.py query "<task>" -k 8

Read returned ranges before broader exploration.

Useful options:

    --path backend/
    --path frontend/
    --path chatgpt-bridge/
    --scores

If semantic embeddings are unavailable and an existing index is usable:

    uv run scripts/ctx/ctx.py query "<task>" --lexical-only --no-refresh

Treat results as hints. Verify real source before editing.
