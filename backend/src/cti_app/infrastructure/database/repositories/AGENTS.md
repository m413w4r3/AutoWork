# Repositories — bounded contexts as modules

Each `.py` file in this directory represents one repository bounded context.
These instructions preserve the architectural split during refactoring.

## Organization

- One module = one bounded context (e.g., `discovery.py`, `collection.py`).
- Import domain entities and ports directly from `domain/` and `application/`.
- Do not add repositories or mappers to `__init__.py` — no giant reexport surface.
- Each module owns its own serializers, mappers, and row converters.

## Shared infrastructure primitives

- There is no `_shared.py` by default. Generic infrastructure helpers (coercions,
  datetime serialization, UUID handling) remain with their canonical owner.
- Create a shared helper only when an infrastructure-generic need emerges and is
  reused across multiple contexts. Do not create `_shared.py` for hypothetical
  or single-use abstractions.
- A business-specific helper (domain payload builder, value serializer, enum
  mapping) stays in its canonical repository module, even if the signature
  looks generic. Never promote a business helper to `_shared.py` merely because
  it has two callers.
- Never duplicate a helper to avoid an import. If you need a shared primitive
  that does not exist and ownership is unclear, stop and decide ownership
  explicitly rather than copy it.

## Ownership and cross-module references

- **Discovery** owns the `CandidateTopic` codec and payload builders.
- **discovery_cumulative** imports the `CandidateTopic` payload codec
  from `repositories.discovery`.
- A business serializer shared across contexts remains owned by its canonical
  context and is imported by others (e.g., discovery → discovery_cumulative).

## Structural changes

- Moves, splits, and refactors must not alter SQL DDL, row locking, transaction
  boundaries, or serialization/deserialization logic.
- Tests are the contract: if a move changes test behavior, the move changes
  application semantics and must be reconsidered.
- Narrowest tests first: validate structure changes with targeted integration
  tests before running the full suite.

## Alembic and migrations

- The database is assumed empty for migrations. Alembic is never a backfill tool.
- ORM/DB drifts inventoried by `test_migrations` are noted but not opportunistically
  repaired during refactoring.
