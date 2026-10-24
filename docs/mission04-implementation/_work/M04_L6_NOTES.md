# Mission 04 — L6 Suricata reparse

**Base:** `df9e091076cbfd3147690f184c5562d626d8ae66`
**Scope:** Suricata reparse admission and local context validation, plus the operator composition oracle. No writer, Bridge transport, source evidence reader, worker mode, database, blob adapter, provider, or CTI behavior was changed.

## Implementation

- Added `suricata_draft` only to the two reparse admission sets in `investigation_advisor_tools.py`.
- Suricata now requires a persisted `reparse_context`, exact equality between its `transmitted_refs` and normalized source references, and non-empty, complete `network_facts` whose source observation references match those transmitted. Missing or unusable context returns `advisor_reparse_context_unavailable`; reference conflicts return `advisor_reparse_context_refs_invalid`, before `record_json`.
- The composition test verifies the `advisor.reparse` definition, capability, and availability for `operator_mode=True`. The default composition has no registration.

## RED and GREEN evidence

- **RED before the production change:** focused run reported the five Suricata reparse cases failing with `advisor_reparse_original_not_found`; this confirmed both admissions blocked the valid case and the intended context refusals. The composition assertion initially queried schema version `1`; correcting it to `ADVISOR_REPARSE_SCHEMA_VERSION` made the composition oracle pass on the existing operator-mode registration.
- **Focused GREEN:** 7 passed, including the success, absent/incomplete context, conflicting refs, and composition cases.
- **Reparse/composition regression GREEN:** 18 passed across `test_advisor_output_reparse.py` and `test_investigation_composition.py`.
- **Ruff:** `ruff check` passed; `ruff format --check` reported all 4 touched Python files already formatted.
- **Targeted mypy:** passed with no issues in the 4 touched Python files.
- The `make test-backend` wrapper did not reach pytest: `uv sync` could not create a temporary file in the read-only shared cache. The tests and checks above ran directly with `uv run --no-sync` and a writable `UV_CACHE_DIR` under `/tmp`.

Commands run from `backend/` (with the temporary cache override shown):

```sh
UV_CACHE_DIR=/tmp/autowork-m04-l6-uv-cache uv run --project . --no-sync python -m pytest tests/test_advisor_output_reparse.py::test_reparse_accepts_verified_suricata_context_without_bridge tests/test_advisor_output_reparse.py::test_reparse_refuses_suricata_without_persisted_context tests/test_advisor_output_reparse.py::test_reparse_refuses_suricata_with_incomplete_network_facts tests/test_advisor_output_reparse.py::test_reparse_refuses_suricata_context_with_conflicting_references tests/test_investigation_composition.py::test_composition_announces_local_tools_and_only_configured_capabilities tests/test_investigation_composition.py::test_operator_inventory_capabilities_match_the_dispatcher -q --tb=short
UV_CACHE_DIR=/tmp/autowork-m04-l6-uv-cache uv run --project . --no-sync python -m pytest tests/test_advisor_output_reparse.py tests/test_investigation_composition.py -q --tb=short
UV_CACHE_DIR=/tmp/autowork-m04-l6-uv-cache uv run --project . --no-sync ruff check src/cti_app/application/investigation_advisor_tools.py src/cti_app/application/investigation_composition.py tests/test_advisor_output_reparse.py tests/test_investigation_composition.py
UV_CACHE_DIR=/tmp/autowork-m04-l6-uv-cache uv run --project . --no-sync ruff format --check src/cti_app/application/investigation_advisor_tools.py src/cti_app/application/investigation_composition.py tests/test_advisor_output_reparse.py tests/test_investigation_composition.py
UV_CACHE_DIR=/tmp/autowork-m04-l6-uv-cache uv run --project . --no-sync mypy src/cti_app/application/investigation_advisor_tools.py src/cti_app/application/investigation_composition.py tests/test_advisor_output_reparse.py tests/test_investigation_composition.py
```

## Verified guarantees and limits

- The successful reparse reads the original advisor observation and its archived output blob only. The fakes assert there are no other observation or artifact reads and only the `model-outputs` address lookup occurs.
- The reparse tool has no Bridge transport dependency; the appended payload records `bridge_turns_consumed=0`. The recorder spy sees exactly one append with the complete input refs unchanged, the parser version, and a parser-versioned idempotency key. Parsed `fact_refs` resolve to the stored `network_facts` IDs.
- Missing context, empty or malformed network facts, and conflicting references fail before the recorder is called; no partial observation is produced.
- `advisor.reparse` is available in the `operator_mode=True` composition. The standard runner calls `composition_factory.build(uow, investigation_id)` at `investigation_jobs.py:73-76`; the factory defaults `operator_mode=False` at `investigation_composition.py:1773-1791`, so it does not expose the tool. L6 does not change that worker behavior. Source behavior was inspected; deployment wiring was not verified.
- The in-memory recorder and artifact fakes prove this application seam only; they do not prove PostgreSQL or MinIO adapter behavior.

## Rollback

Revert the L6 production, test, and note changes. No schema or persisted data change is involved.
