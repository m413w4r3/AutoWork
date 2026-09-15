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
- `0001_baseline` is the complete schema intended for an empty database.
- `alembic upgrade head` on an empty database must always create the target
  schema.
- Through AW-002 to AW-013, until the new baseline is explicitly declared
  stable, `0001_baseline` is intentionally mutable. Schema-target changes
  should update that baseline instead of adding migrations whose only purpose
  is transforming an older AutoWork database.
- The mutable baseline must describe the target schema directly. Do not add
  legacy database detection, compatibility backfills, repair branches, or
  old-database upgrade support.
- Preserve downgrade and guard protections that belong to the current
  single-baseline schema, not tests whose sole purpose is compatibility with
  deleted revisions.

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
