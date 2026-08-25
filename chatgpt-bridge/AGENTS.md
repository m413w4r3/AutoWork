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
  classes; manages `CleanupWorker`, `ConversationSweeper`; auth via `require_key`.

### bridge/config.py
- Environment variables and defaults (HOST, PORT, API_KEY, WS_TOKEN, timeouts, etc.).

### bridge/contracts.py
- Pure DTOs: `BridgeRunRequest`, `ResponseRequest`, `ChatRequest`, etc.
- No logic.

### bridge/transport.py
- WebSocket multiplexing, request/response pairing by ID.
- Single reader loop pattern.

### bridge/registry.py
- SQLite runs registry: claim, state transitions, lifecycle queries.
- **Fresh schema only**: no ALTER TABLE, no legacy migration, no backfill.
- If reset required, delete manually: `bridge-runs.sqlite3`, `.sqlite3-wal`, `.sqlite3-shm`.
- Browser/auth state in `bridge_data` is always preserved.

### bridge/lifecycle.py
- `CleanupWorker`: retry loop, terminal identity error handling.
- `ConversationSweeper`: periodic sweep, state transitions.
- **TERMINAL_IDENTITY_ERROR_CODES**: defined here, consumed explicitly.
- Enforces the deletion rules in "Destructive actions — fail closed" below.

### bridge/ui.py
- State machine, controls application, UI cache.
- Private state: `_probe_cache` confined here.

### bridge/generation.py
- Parsing, final-only generation, timeouts, metadata.
- Private state: `_live_progress` confined here.
- See "Generation invariants" below.

### bridge/routes_*.py
Route instances are injected by BridgeApplication.

- **routes_conversations.py**: archive, release, lifecycle, cleanup start/complete/fail.
- **routes_openai.py**: responses, chat completions, models.
- **routes_bridge.py**: native runs, recovery, UI, capabilities, metrics. Depends on OpenAIRoutes (one-way).

### extension/
- DOM selectors, browser actions, content script, service worker.
- Never in `server.py` or the lifecycle state machine.

---

## Responsibility boundary

The bridge owns external conversations, fresh/continue behavior,
`external_locator`, `ConversationPolicy` application, and conversation
cleanup. It never decides whether an AutoWork business artifact is valid.

## Destructive actions — fail closed

Deletion requires **all of**:

- the conversation is registered locally;
- policy is `DELETE_ON_SUCCESS`;
- `release_outcome` is `SUCCESS`;
- lifecycle status permits deletion;
- `external_locator` is verified after opening the page — never a heuristic
  match by title, position, DOM index, or visual similarity.

**Fail-closed**: `locator_mismatch` or `locator_invalid` → `CLEANUP_FAILED`
(terminal). No retry, no heuristic replacement search, no override via
endpoint. The conversation has already been released; the client must handle
the blocked cleanup via manual intervention or application-level escalation.

Control data comes only from the trusted client: prompt text and ChatGPT
response text can never change policy, lifecycle state, or trigger deletion.

## Generation invariants

- **done snapshot** = authoritative final output, never incomplete.
- **heartbeat** = liveness signal only, never carries user content.
- **idle timeout** ≠ **total timeout**: idle detects stalls; total caps duration.
- **No timeout resubmits** the prompt.
- **conversation_id + external_locator** remain strict (no inference, no recovery search).
- **needs_review** never triggers implicit replay.

## Validation

Run the narrowest existing bridge test covering the modified behavior; use
the full gates below only when the change affects lifecycle, cleanup, or
browser integration.

Run the Python bridge gate (90+ passed — includes route/transport/generation
tests previously duplicated under `backend/tests/test_chatgpt_bridge.py`):

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
