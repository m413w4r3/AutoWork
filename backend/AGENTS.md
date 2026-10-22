# Backend — FastAPI / Python 3.12 / uv / Dramatiq

Code lives under `src/cti_app/`.

- `domain/`: pure entities and invariants; no I/O.
- `application/`: services, orchestration, and ports.
- `infrastructure/`: PostgreSQL, MinIO, Redis, HTTP adapters.
- `workers/`: Dramatiq jobs.

## Rules

- Control enums are typed `StrEnum` values in `domain/`.
  Do not encode control state as free-form strings or metadata keys.
- Business decisions never belong in `infrastructure/`.
- Tests never contact external APIs.
- Integration tests use `make test-integration`.
- `0001_baseline` is frozen at the pre-C2 schema already represented by
  installations stamped `0001_baseline`. Do not edit it to add later schema.
- `0002_c2_experiments` adds C2 experiment storage. Fresh installs must run
  `alembic upgrade head` and receive the exact ORM schema plus C2 objects.
- Schema changes from `0002` onward are additive Alembic revisions. Preserve
  historical revision behavior and add explicit preflight/compatibility logic
  when an already-stamped installation may have received a manual change.
- The C2 revision accepts a prior manual installation only after exhaustive
  validation of its columns, constraints, indexes, foreign keys, function and
  trigger. Partial or non-conforming objects must fail before schema changes.
- Keep the `0002` downgrade guard: it must refuse to remove C2 storage while
  experiment or head rows exist. Never downgrade C2 on the live database.
- Preserve downgrade and guard protections for every versioned schema.

## Validation

Run the narrowest test first:

    make test-backend \
      PYTEST_ARGS="tests/path/test_file.py::test_name -q --tb=short"

For a PostgreSQL integration test, use:

    make test-integration \
      INTEGRATION_TEST_PATH=tests/integration/path/test_file.py \
      INTEGRATION_PYTEST_ARGS="-q --tb=short"

For backend-only changes, use:

    make lint-backend
    make typecheck-backend

Do not run frontend checks for a backend-only change.

Use the full backend suite only when targeted tests are insufficient.
