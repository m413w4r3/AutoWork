# Mission 04 / Lot L3 — PostgreSQL audit export oracle

## Status

**BLOCKED_ENV.** The integration test and its PostgreSQL assertions are written,
but this sandbox cannot access the Docker daemon needed by the `postgres-test`
profile. No PostgreSQL test assertion was executed, so this lot does not claim a
green reader/export result or a production defect.

Base SHA supplied for the lot: `e120a36f1afa08261fdf0276e0200872973ad8f2`.

## Files and scope

- Added `backend/tests/integration/test_investigation_audit_export_postgres.py`.
- This is test-only. No reader, exporter, CLI, repository, schema, or migration
  code was changed.
- Seeded target and foreign investigations use the actual SQLAlchemy UOW and
  repositories. The target fixture includes versioned tool invocation states,
  observations, finding revisions, opinion revisions, working state, a pivot
  conversation with ordered turns, and linked model runs. The foreign fixture
  has its own invocation, observation, conversation, turn, and model run to
  exercise investigation scope.
- The test independently queries the PostgreSQL rows and compares those IDs,
  versions, states, references, and hashes with the real snapshot reader and
  `build_audit_bundle` output.

## Oracle coverage

The test checks all four outputs: `RUN_EVENTS.jsonl.gz`, `RUN_TRACE.md`,
`RUN_INDEX.json`, and `RUN_MANIFEST.json`. Assertions cover target investigation
scope, event order and type counts, canonical record IDs and versions, finding
and opinion references, state and model-turn links, observation and model
hashes, event IDs, manifest file hashes/byte lengths/counts, and checkpoint
resume equality.

One tool invocation contains a deliberately absent response reference. The
oracle requires that ID to remain an unresolved `tool_response_reference` in
the index with no matching event. One advisor-call observation is backed by a
synthetic blob row whose declared SHA matches the observation, while the test
artifact reader returns bytes with a deterministic extra byte. The export must
mark the read `hash_mismatch`, keep unavailable advisor fields `UNKNOWN`, and
omit the marker body from every output. This fake artifact reader exercises
hash verification only; it is not MinIO evidence.

The test includes three temporary, in-memory sensitivity perturbations of a
copied bundle: remove a canonical observation event, change an event's
investigation scope to the foreign investigation, and alter an observation
SHA-256. Each must raise the corresponding oracle assertion. The original
bundle is checked again afterward. These checks are part of the PostgreSQL
test and were **not executed** because Docker was unavailable.

An SQLAlchemy statement listener is reset after the database snapshot is
captured. It asserts that building and writing from that snapshot, and resuming
from the event checkpoint, execute no SQL statements.

## Validation record

Requested PostgreSQL command:

```text
make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_audit_export_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"
```

The exact command stopped during `uv sync`, before Docker or pytest, because
the default UV cache path was read-only and UV could not create a temporary
file there. Retried the same Make target using a writable cache under `/tmp`:

```text
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_audit_export_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"
```

UV sync completed. Docker then reported permission denied for
`/var/run/docker.sock`; the Docker CLI also emitted a secondary panic while
handling that denied connection. The integration test did not start.

```text
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache uv run --python 3.12 pytest --collect-only -q tests/integration/test_investigation_audit_export_postgres.py
```

Result: **1 test collected, 0 executed**. This checks import/collection only;
it is not evidence of PostgreSQL behavior.

```text
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache uv run --python 3.12 ruff check tests/integration/test_investigation_audit_export_postgres.py
```

Result: passed, all checks clean.

```text
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache uv run --python 3.12 ruff format --check tests/integration/test_investigation_audit_export_postgres.py
```

Result: passed, file already formatted.

Integration counts: **0 passed, 0 failed, 0 skipped**; pytest was not invoked
by the Make target after Docker startup failed. The collect-only command found
one test and ran none.

## Limits and next action

The required real PostgreSQL reader/export oracle, including its sensitivity
perturbations, remains unverified in this sandbox. No SQLite or in-memory
database was used as a substitute. No production defect is established; a
production defect could still surface when the test runs against PostgreSQL.
The next permissible action is to rerun the documented Make command in an
environment where the disposable `postgres-test` Docker profile is accessible.
