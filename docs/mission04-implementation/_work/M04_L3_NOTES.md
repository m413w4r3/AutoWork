# Mission 04 / Lot L3 — PostgreSQL audit export oracle

## Status

**PARENT_POSTGRES_GREEN.** The parent ran the disposable `postgres-test`
oracle. Its first run failed in the test fixture because `InvestigationAgenda`
had one question; after adding a second valid question, the same command
completed with **1 passed in 1.25s**. That was a fixture correction, not a
production defect. After review repair attempt 1, the parent reran PostgreSQL
and the exact resource-set assertion failed because the expected set omitted
the synthesis artifact and base publication artifact already present in the
bundle (**1 failed in 1.15s**). Repair attempt 2 added both expected keys from
independently queried PostgreSQL rows; the parent reran the oracle on that
revision and it completed with **1 passed in 1.02s**. The independent R2 review
then approved the lot. The fake artifact reader still does not prove real MinIO
integration.

Base SHA supplied for the lot: `e120a36f1afa08261fdf0276e0200872973ad8f2`.

## Files and scope

- Added `backend/tests/integration/test_investigation_audit_export_postgres.py`.
- This is test-only. No reader, exporter, CLI, repository, schema, or migration
  code was changed.
- Seeded target and foreign investigations use the actual SQLAlchemy UOW and
  repositories. The target fixture includes versioned tool invocation states,
  observations, finding revisions, opinion revisions, a persisted analyst
  decision, working state, a pivot conversation with ordered turns, and linked
  model runs. The foreign fixture has its own invocation, observation,
  conversation, turn, and model run to exercise investigation scope.
- The test independently queries the PostgreSQL rows and compares those IDs,
  versions, states, references, and hashes with the real snapshot reader and
  `build_audit_bundle` output.

## Oracle coverage

The test checks all four outputs: `RUN_EVENTS.jsonl.gz`, `RUN_TRACE.md`,
`RUN_INDEX.json`, and `RUN_MANIFEST.json`. Assertions cover target investigation
scope, stable event order, exact event type/record/version identities and
counts, canonical record IDs and versions, finding and opinion references,
state and model-turn links, observation and model hashes, event IDs, manifest
file hashes/byte lengths/counts, and checkpoint resume equality. Analyst
decisions are append-only records without a version column; the test asserts
their event and manifest record-ref version is explicitly null, and resolves
the decision target to the persisted pivot conversation and its version.

The expected `RUN_INDEX` resource key set is derived from the queried PostgreSQL
rows and compared by equality. Its expected count is computed from that
independent set and used for the manifest counts. `RUN_TRACE` chronology lines
are counted exactly by event type and record ID; opinion version references are
also checked by ID/version, and the analyst decision target is checked in both
the chronology and operator-decision section.

One tool invocation contains a deliberately absent response reference. The
oracle requires that ID to remain an unresolved `tool_response_reference` in
the index with no matching event. One advisor-call observation is backed by a
synthetic blob row whose declared SHA matches the observation, while the test
artifact reader returns bytes with a deterministic extra byte. The export must
mark the read `hash_mismatch`, keep unavailable advisor fields `UNKNOWN`, and
omit the marker body from every output. This fake artifact reader exercises
hash verification only; it is not MinIO evidence.

The test includes temporary sensitivity perturbations of a copied bundle:
remove a canonical observation event, remove the analyst-decision event,
change an opinion event's version, remove the analyst-decision target resource,
remove an opinion-version trace line, change an event's investigation scope to
the foreign investigation, and alter an observation SHA-256. Each must raise
the corresponding oracle assertion. The original bundle is checked again
afterward. These perturbations run only as part of the PostgreSQL test.

An SQLAlchemy statement listener is reset after the database snapshot is
captured. It asserts that building and writing from that snapshot, and resuming
from the event checkpoint, execute no SQL statements.

## Review repair attempt 1

The independent review returned `REVISE` with three medium findings. The
test-only repair adds:

1. One `AnalystDecision` persisted through `uow.analyst_decisions.append`, plus
   independent PostgreSQL, snapshot, event, trace, index-target, and manifest
   record-ref checks for identity, null record version, decision type, target
   type/ID, and target conversation version.
2. Exact `RUN_INDEX` resource-set equality and an independently derived
   expected resource count for manifest assertions. A temporary target-resource
   omission must fail the oracle.
3. Exact event identity counts by type/record/version for all canonical
   `RUN_EVENTS`, including both opinion versions and the analyst decision;
   exact `RUN_TRACE` chronology counts by type and ID, explicit opinion version
   references, plus deterministic version and event omission perturbations.

The manifest contract lists event IDs/types/versions and hashes the full
projected event stream and files; it does not duplicate decision target fields
in `record_refs`. The test checks the target in `RUN_EVENTS`, `RUN_TRACE`, and
`RUN_INDEX`, then checks its event identity/version in `record_refs` and the
manifest hashes that commit to the event payload.

## Review repair attempt 2

The parent rerun after attempt 1 failed only at exact `RUN_INDEX` resource-set
equality. The bundle had two extra canonical resources: `synthesis_artifact`
and `base_publication_artifact`. The test now adds the synthesis key from the
persisted investigation row's `synthesis_artifact_id`, and the publication
key from the approved/published opinion version's persisted JSON content.
Opinion IDs and SHA-256 values also come from those PostgreSQL opinion rows;
the expected set is not read from bundle output. The exporter confirms that
`opinion.*` events project these resources from the persisted opinion details'
`synthesis_artifact_id`/`base_publication_artifact_id` fields. Exact set
equality and all previous assertions and sensitivity perturbations remain.
No production code was changed.

## Validation record

The parent ran the disposable PostgreSQL command before review repair attempt 1:

```text
make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_audit_export_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"
```

The first parent run failed during fixture construction because the agenda had
one question. After the fixture was corrected to contain two valid questions,
the parent reran the command: **1 passed in 1.25s**. This result predates the
three review repairs above.

Before receiving the parent result, the local Make target was also attempted.
The default UV cache path was read-only; retrying with a writable cache allowed
UV sync to complete but the sandbox denied `/var/run/docker.sock` access. That
local failure was environmental and did not execute the test body:

```text
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache make test-integration INTEGRATION_TEST_PATH=tests/integration/test_investigation_audit_export_postgres.py INTEGRATION_PYTEST_ARGS="-q --tb=short"
```

Docker reported permission denied for `/var/run/docker.sock`; the test did not
start. No SQLite or in-memory database was used as a substitute.

After review repair attempt 1, collection was rerun with Python 3.12:

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

After review repair attempt 2, the test was formatted and validated again with
Python 3.12:

```text
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache uv run --python 3.12 pytest --collect-only -q tests/integration/test_investigation_audit_export_postgres.py
```

Result: **1 test collected, 0 executed**.

```text
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache uv run --python 3.12 ruff check tests/integration/test_investigation_audit_export_postgres.py
UV_CACHE_DIR=/tmp/autowork-m04-l3-uv-cache uv run --python 3.12 ruff format --check tests/integration/test_investigation_audit_export_postgres.py
```

Both commands passed. `git diff --check -- backend/tests/integration/test_investigation_audit_export_postgres.py docs/mission04-implementation/_work/M04_L3_NOTES.md` also passed.

Current test SHA-256: `1ebbabd442010283272cf4e3ed9a90817a8c9c440754be792ccb70bc4514921d`.

The current review-repaired revision has **not** been run against PostgreSQL:
the current integration counts are therefore not available. The latest parent
failure was the omitted expected artifact keys described above; it does not
establish a production defect. The parent should rerun the exact
`make test-integration` command above against the disposable `postgres-test`
profile.

## Limits and next action

The latest event, index, and trace sensitivity perturbations have not yet been
executed because they run inside the PostgreSQL-backed test. The next action is
to rerun that test in the parent's environment; then record its pass/failure
counts and any newly observed production defect without widening this test-only
lot.
