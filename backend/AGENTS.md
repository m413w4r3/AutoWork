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
- Integration tests use `TEST_POSTGRES_ADMIN_DSN`.
- The database is assumed empty for migrations:
  no backfills, legacy columns, or compatibility modes.
- `alembic upgrade head` on an empty database must create the complete target
  schema.

## Validation

Run the narrowest test first:

    cd backend && uv run pytest tests/path/test_file.py::test_name -q --tb=short

For backend-only changes, prefer:

    cd backend && uv run ruff check .
    cd backend && uv run mypy src tests

Do not run frontend checks for a backend-only change.

Use the full backend suite only when targeted tests are insufficient.
