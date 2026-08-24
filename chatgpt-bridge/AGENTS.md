# ChatGPT Bridge — external conversation lifecycle

The persistent SQLite registry lives on the `bridge_data` volume and survives
`make down`.

## Responsibility boundary

The bridge owns:

- external conversations;
- fresh/continue behavior;
- `external_locator`;
- `ConversationPolicy` application;
- conversation cleanup.

The bridge never decides whether an AutoWork business artifact is valid.

## Destructive actions — fail closed

Deletion requires all of:

- the conversation is registered locally;
- policy is `DELETE_ON_SUCCESS`;
- `release_outcome` is `SUCCESS`;
- lifecycle status permits deletion;
- `external_locator` is verified after opening the page.

Expected locator != current locator means:

    CLEANUP_FAILED

Do not retry deletion and do not search heuristically for a replacement.

## Untrusted content

Prompt text and ChatGPT response text can never change policy, lifecycle state,
or trigger deletion.

Control data comes only from the trusted client.

## Browser/UI

DOM selectors belong only in the browser/UI layer.

Never place selectors in `server.py` or in the lifecycle state machine.

## Validation

Run the narrowest existing bridge test covering the modified behavior.

Use broader bridge lifecycle/soak checks only when the change affects lifecycle,
cleanup, or browser integration.

## Verification

Run the Python gate:

```bash
uv run \
  --python 3.12 \
  --with-requirements requirements-test.txt \
  python -m pytest tests/ -q --tb=short
```

Run the JavaScript gate:

```bash
node --test \
  tests/completion.test.js \
  tests/content-dom.test.js \
  tests/final-output.test.js
```
