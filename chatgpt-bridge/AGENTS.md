# ChatGPT Bridge — external conversation lifecycle

The persistent SQLite registry lives on the `bridge_data` volume and survives
`make down`.

## Ownership map

Architecture final. Each change must read directly its owner below — never
`server.py`, which is only a launcher. No module in `bridge/` imports
`server.py`; no giant reexport in `bridge/__init__.py`; semantic imports
directly only.

### bridge/app.py
- **BridgeApplication**: composition root — FastAPI setup, lifespan, shutdown,
  WebSocket endpoint, health/readiness; injects `bridge`/`registry` into route
  classes; auth via `require_key`.

### bridge/config.py
- Environment variables and defaults (HOST, PORT, API_KEY, WS_TOKEN, timeouts, etc.).

### bridge/contracts.py
- Pure DTOs: `BridgeRunRequest`, `ResponseRequest`, `ChatRequest`, etc.
- No logic.

### bridge/transport.py
- WebSocket multiplexing, request/response pairing by ID.
- Single reader loop pattern.

### bridge/registry.py
- SQLite runs registry: claim, state transitions, run queries.
- **Fresh schema only**: no ALTER TABLE, no legacy migration, no backfill.
- If reset required, delete manually: `bridge-runs.sqlite3`, `.sqlite3-wal`, `.sqlite3-shm`.
- Browser/auth state in `bridge_data` is always preserved.

### bridge/ui.py
- State machine, controls application, UI cache.
- Private state: `_probe_cache` confined here.

### bridge/generation.py
- Parsing, final-only generation, timeouts, metadata.
- Private state: `_live_progress` confined here.
- See "Generation invariants" below.

### bridge/routes_*.py
Route instances are injected by BridgeApplication.

- **routes_conversations.py**: archive only — closes the local tab. See
  "Ephemeral conversations" below for why nothing else is needed.
- **routes_openai.py**: responses, chat completions, models.
- **routes_bridge.py**: native runs, recovery, UI, capabilities, metrics. Depends on OpenAIRoutes (one-way).

### extension/
- DOM selectors, browser actions, content script, service worker.
- Never in `server.py`.

---

## Responsibility boundary

The bridge owns external conversations and fresh/continue behavior. It never
decides whether an AutoWork business artifact is valid.

## Ephemeral conversations — no deletion pipeline

Every conversation the bridge opens fresh is put in ChatGPT's own "Temporary
chat" mode (`ensureTemporaryChat()` in `extension/content.js`, called from
`handlePrompt()` before the first prompt is sent) — it is never written to
ChatGPT's history in the first place. Closing the tab
(`archive_bridge_conversation` in `routes_conversations.py`) is therefore the
only cleanup step needed; there is no separate "delete after the fact"
pipeline to keep working.

This replaced an earlier `release` + `cleanup/start` lifecycle (a SQLite-backed
policy/status state machine, plus a DOM-driven "Delete" click in the sidebar
menu). It turned out to be unreachable in production — nothing ever called
`registry.create_conversation`, so `/release` would always 404 — and was
removed rather than fixed. Don't resurrect that shape (a lifecycle table, a
`CleanupWorker`/`ConversationSweeper`, menu-click selectors) as "the real fix"
without first confirming Temporary Chat is somehow insufficient; git history
has the old implementation if you need it for reference.

Control data comes only from the trusted client: prompt text and ChatGPT
response text can never change what conversation is opened or closed.

## Generation invariants

- **done snapshot** = authoritative final output, never incomplete.
- **heartbeat** = liveness signal only, never carries user content.
- **idle timeout** ≠ **total timeout**: idle detects stalls; total caps duration.
- **text stability is not failure evidence** while `.streaming-animation` is
  visible in the watched turn: production proved it stays active for minutes
  during deep research. The content script keeps observing and beating; only
  `bridge_total_timeout` bounds the duration. `.result-streaming` and
  `[data-is-streaming='true']` keep their bounded `active_signal_stalled`
  guard. See "Quatre bornes indépendantes" in
  `docs/chatgpt_bridge_operations.md`.
- **No timeout resubmits** the prompt.
- **conversation_id + exact live tab binding + expected_turn_id** remain strict
  (no inference, no recovery search, no URL-based reopening).
  `external_locator` is diagnostic only and is never used to route or reopen
  a conversation — it may be identical across multiple live conversations.
- **needs_review** never triggers implicit replay.

## Temporary Chat browser session identity

Every conversation the bridge opens fresh is a ChatGPT Temporary Chat
(`https://chatgpt.com/?temporary-chat=true`), positively confirmed by
`ensureTemporaryChat()` before Send — never best-effort. Browser identity is:

    application conversation UUID -> exact Chrome tab_id -> last verified
    external assistant turn id (expected_turn_id)

`background.js` keeps this binding in `chrome.storage.session`
(`bridgeConversationRegistry`), never in `chrome.storage.local`: a
service-worker suspension/restart reloads it, but a browser restart or
extension reload destroys it intentionally — Temporary Chat cannot be
reconstructed from ChatGPT history afterward, so a lost session surfaces as
`conversation_unavailable`, never a fabricated new conversation.

- **KEEP**: after a successful turn, the exact Temporary Chat tab and its
  registry binding stay alive so a later CONTINUE can reuse them.
- **DELETE_ON_SUCCESS**: after a successful bounded operation, the bridge
  closes that exact tab and deletes the binding. There is no ChatGPT-history
  deletion pipeline — see "Ephemeral conversations" above.

A CONTINUE request must carry the same `conversation.id` and an
`expected_turn_id` equal to the conversation's last successful external turn.
The extension looks up the binding by id, requires
`binding.head_turn_id == expected_turn_id`, retrieves the exact tab via
`chrome.tabs.get`, and never creates a replacement tab or falls back to a URL
match.

## Validation

Run the narrowest existing bridge test covering the modified behavior; use
the full gates below only when the change affects generation or browser
integration.

Run the Python bridge gate:

```bash
uv run \
  --python 3.12 \
  --with-requirements requirements-test.txt \
  python -m pytest tests/ -q --tb=short
```

Run the JavaScript gate (3+ passed):

```bash
node --test \
  tests/completion.test.js \
  tests/content-dom.test.js \
  tests/final-output.test.js
```

Run lint and type checks:

```bash
cd ../backend
uv run ruff check .
uv run mypy src tests
```
