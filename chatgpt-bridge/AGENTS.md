# ChatGPT Bridge — external conversation lifecycle

The persistent SQLite registry lives on the `bridge_data` volume and survives
`make down`.

## Ownership map

Architecture final. Each change must read directly its owner — never start with
`server.py` to modify generation, registry, lifecycle, or UI.

### server.py
- **launcher only**: instantiates `BridgeApplication`, starts it.

### bridge/app.py
- **BridgeApplication**: composition root, FastAPI setup, lifespan, shutdown, WebSocket endpoint, health/readiness.
- Injects `bridge`, `registry` into all route classes.
- Manages `CleanupWorker`, `ConversationSweeper`.
- Auth dependency: `require_key`.

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
- Fail-closed: locator_mismatch / locator_invalid → CLEANUP_FAILED, no retry, no override.

### bridge/ui.py
- State machine, controls application, UI cache.
- Private state: `_probe_cache`, `_live_progress` confined here.

### bridge/generation.py
- Parsing, final-only generation, timeouts, metadata.
- **Invariants documented**:
  - done snapshot = authoritative output
  - heartbeat = liveness, never user content
  - idle timeout ≠ total timeout
  - no timeout resubmits prompt
  - conversation_id + external_locator strict
  - needs_review never triggers implicit replay

### bridge/routes_conversations.py
- **ConversationRoutes owner**: archive, release, lifecycle, cleanup start/complete/fail.
- Instances injected by BridgeApplication.

### bridge/routes_openai.py
- **OpenAIRoutes owner**: responses, chat completions, models.
- Instances injected by BridgeApplication.

### bridge/routes_bridge.py
- **BridgeRoutes owner**: native runs, recovery, UI, capabilities, metrics.
- Instances injected by BridgeApplication.
- Depends on OpenAIRoutes (one-way).

### extension/
- DOM selectors, browser actions, content script, service worker.

---

## Navigation Policy

One change must read directly its owner. Examples:

- Change generation logic → read `bridge/generation.py` first, never start in `server.py`.
- Change registry state machine → read `bridge/registry.py`, never `server.py`.
- Change cleanup behavior → read `bridge/lifecycle.py`, never `server.py`.
- Change UI state → read `bridge/ui.py`, never `server.py`.

**No module in `bridge/` imports `server.py`.**
**No giant reexport in `bridge/__init__.py`.**
**Semantic imports directly only.**

---

## Responsibility boundary

The bridge owns:

- external conversations;
- fresh/continue behavior;
- `external_locator`;
- `ConversationPolicy` application;
- conversation cleanup.

The bridge never decides whether an AutoWork business artifact is valid.

## Destructive actions — fail closed

Deletion requires **all of**:

- the conversation is registered locally;
- policy is `DELETE_ON_SUCCESS`;
- `release_outcome` is `SUCCESS`;
- lifecycle status permits deletion;
- `external_locator` is verified after opening the page.

**Fail-closed**:

- **locator_mismatch** or **locator_invalid** → `CLEANUP_FAILED` (terminal).
- Do **not** retry deletion.
- Do **not** search heuristically for a replacement.
- Do **not** override via endpoint.
- Conversation is never replaced; run is marked terminal and must be manually released.

## Untrusted content

Prompt text and ChatGPT response text can never change policy, lifecycle state,
or trigger deletion.

Control data comes only from the trusted client.

## Browser/UI

DOM selectors belong only in the browser/UI layer.

Never place selectors in `server.py` or in the lifecycle state machine.

## Generation invariants

- **done snapshot** = authoritative final output, never incomplete.
- **heartbeat** = liveness signal only, never carries user content.
- **idle timeout** ≠ **total timeout**: idle detects stalls; total caps duration.
- **No timeout resubmits** the prompt.
- **conversation_id + external_locator** remain strict (no inference, no recovery search).
- **needs_review** never triggers implicit replay.

## Validation

Run the narrowest existing bridge test covering the modified behavior.

Use broader bridge lifecycle/soak checks only when the change affects lifecycle,
cleanup, or browser integration.

## Verification

Run the Python bridge gate (65+ passed):

```bash
uv run \
  --python 3.12 \
  --with-requirements requirements-test.txt \
  python -m pytest tests/ -q --tb=short
```

Run the backend bridge integration gate (33+ passed):

```bash
cd ../backend
uv run pytest tests/test_chatgpt_bridge.py -q --tb=short
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
