# M02-S7 dead-code inventory

Base SHA: `e4abba495d9f5fdc296e2ba80b65442855625474`.

`rg` was used only to locate references. Candidate decisions below also check
the active call path, tests, API/CLI entrypoints, workers, and string-based
dynamic references. No migrations or archive formats are changed.

## Accepted removals

### `_source_figure_candidates` — removed

- **Location:** `backend/src/cti_app/application/production_editorial_enrichment.py:4179`
- **Caller evidence:** Repository-wide search found only the definition. It is
  private and is not imported, registered, called by a worker or CLI, or named
  in a dynamic lookup. The active figure path builds a prompt catalog in
  `build_editorial_figure_catalog` (called by the enrichment service) and
  creates selected domain candidates during proposal validation.
- **Deletion oracle:** The active figure catalog, parse, and selection behavior
  is covered by `backend/tests/test_production_editorial_enrichment_application.py`,
  including `test_figure_proposal_selects_local_asset_with_the_model_written_caption`
  and `test_two_catalog_figures_allow_only_the_relevant_one_to_be_selected`.
  Those paths do not call this helper.
- **Risk:** No internal runtime caller was found. An out-of-repository caller
  importing a private helper cannot be ruled out.

### `OperatorToolInvocationService.get_context_summary` — accepted for removal

- **Location:** `backend/src/cti_app/application/operator_tool_invocation.py:438`
- **Caller evidence:** Repository-wide search found only the method definition.
  No API, test, worker, CLI, registry, or dynamic lookup calls it. The active
  `/operator/context?view=summary` route instead calls `get_context` and
  `build_operator_context_summary` directly, adding invalidation facts and
  `since_fingerprint` support that this method does not provide.
- **Deletion oracle:**
  `backend/tests/test_operator_tool_invocation.py::test_operator_http_routes_expose_bounded_context_and_idempotent_invocation`
  exercises the active summary route, including CAS and delta fields.
- **Risk:** The service class is exported from its module, so an unobserved
  external Python consumer could call this method. No in-repository or
  documented extension point does so.

## Rejected candidates

### Observation summary lookups in `operator_tool_invocation.py` — retain

- **Locations:** `_framework_pre_submission_proof` near line 813,
  `_validate_recommendation_trace` near line 1183, and `_result` near line 1299.
- **Caller evidence:** All three are on active invocation paths. They have
  distinct purposes: establish framework pre-submission proof, validate a
  persisted advisor recommendation against observations, and build bounded
  response observation data on both fresh execution and idempotent replay.
- **Deletion oracle:** Removing any lookup changes a separate proof or response
  behavior covered by operator invocation tests.
- **Risk:** Removal would weaken evidence checks or change the API response.

### `load_audit_events` — retain

- **Location:** `backend/src/cti_app/application/investigation_audit_export.py:2655`
- **Caller evidence:** The helper is used directly by offline tests rather than
  the export CLI. It validates an existing compressed event checkpoint using
  the same schema, identity, stable-order, digest, and snapshot checks used by
  `--resume` through `_read_gzip_events`.
- **Deletion oracle:** The helper provides a public read/validation entrypoint
  for archived checkpoints; its test-only direct usage is not coupled to dead
  implementation.
- **Risk:** Removing it would reduce deterministic archive/replay capability
  and could break downstream readers, despite no in-repository production
  caller.

### Editorial resource-search path — retain

- **Locations:** `production_editorial_enrichment.py` around lines 6689–6720
  and 7229 onward.
- **Caller evidence:** The path is guarded by the configured resource-search
  setting and access policy, is part of the input identity, and is consumed by
  production enrichment and revision flows. Tests exercise disabled and
  enabled states.
- **Deletion oracle:** Not applicable; live configuration and production
  callers disprove deadness.
- **Risk:** Removing it would alter configured behavior and artifact identity.
