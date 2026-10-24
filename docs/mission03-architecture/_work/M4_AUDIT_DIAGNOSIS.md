# M4-C — Audit and context seam diagnosis

**Code SHA:** aa0cef3ee07525ca77d6b1358252ebf5735a3dde
**Branch:** mission03/architecture-design
**Scope:** audit export, context projection and paging, state/persistence interfaces, and their API callers. This is a read-only diagnosis; this file is the only artifact written.

Path notation below: application/ and infrastructure/ source paths are relative to backend/src/cti_app; test paths are relative to backend/tests; docs/ paths are relative to the repository root.

## Verified interface inventory

| Module / interface | Verified owner and caller obligations |
|---|---|
| Context page proof — application/context_pagination.py:18-57, 82-160, 215-253, 465-579, 598-748 | Owns strict page/omission value types, position cursor parsing, scope checks, canonical page SHA-256, source references, and construction-time manifest consistency. A page digest covers the returned projection plus section, investigation, cursor/pivot, direction, limit, total, has_more, prior digest, and omission counts. It does not authenticate a cursor or prove a database snapshot. |
| Entity persistence — application/investigation_persistence.py:38-155 and infrastructure/database/repositories/investigation.py:159-706 | Protocols expose each entity’s read/list/page/append or version-update rule. In-memory and SQL adapters implement them. SQL owns seek predicates, order, count and limit+1 fetch; callers do not write query predicates. Finding history is append-only with explicit supersedes. |
| Working state — application/investigation_state.py:21-86 and repositories/investigation.py:1264-1411 | One get/save_if_version interface has in-memory and PostgreSQL adapters. Equal state saves are idempotent; changed state requires the expected version and an exact +1. SQL locks the row and checks JSON state against indexed columns. |
| Context projection — application/research_context_builder.py:1052-1429, 1661-1923, 2236-2518 | ResearchContextBuilder owns source collection, projection, bounded/unbounded manifests, and source-section pages. A caller supplies state, article identity, repositories, tool context and budget; it does not assemble evidence summaries itself. Missing page support falls back to list materialization and is explicitly marked source_bounded=false. |
| Operator context API — application/operator_tool_invocation.py:350-698; api/investigation.py:1162-1245, 1410-1458 | The service owns working-state CAS checks, cursor scope/limit validation, calls to context projection, page proofs, and routing between source-paged and materialized sections. Routes enforce operator/reader access and translate failures. Cursor continuation requires expected working-state version and SHA; legacy numeric offsets remain accepted. |
| Audit read adapter — infrastructure/database/investigation_audit_reader.py:48-164, 167-209 | The CLI-facing wrapper captures canonical PostgreSQL rows in a REPEATABLE READ READ ONLY transaction, then projects hash-verified advisor-call blob metadata. The lower-level session function does not establish that transaction itself. |
| Audit projection and files — application/investigation_audit_export.py:182-215, 1668-1777, 1846-2108, 2199-2386, 2387-2567, 2568-2659 | AuditSourceSnapshot is an explicit input seam. One event list feeds RUN_EVENTS, RUN_INDEX, RUN_TRACE and RUN_MANIFEST. The export includes references, versions, hashes and explicit UNKNOWN/unavailable entries; it does not make the export files canonical evidence. Deterministic IDs/bytes, per-directory locking, atomic writes and checkpoint resume are inside the exporter. |
| Audit caller — backend/scripts/export_investigation_audit.py:40-76 | The production caller is a CLI. It constructs PostgreSQL and MinIO adapters, calls the snapshot wrapper, and writes the bundle. No investigation audit-export HTTP route or export method in InvestigationService was found. InvestigationService.run builds Bridge context; reconcile_invocation appends an audit decision, which is a separate meaning of audit (application/investigation_service.py:523-640, 1784-1909). |
| Model-run references — infrastructure/database/repositories/model_runs.py:24-31; investigation_audit_reader.py:116-136 | SqlAlchemyModelRunRepository exposes get by ID. The audit reader follows the investigation’s pivot conversation turns, de-duplicates their model_run_ids and calls get for each. It does not independently scan unrelated model runs. |

## Leak and recomposition table

| Fact / paths | Verified behavior | Diagnosis |
|---|---|---|
| Finding membership and order: research_context_builder.py:1101-1110, 1325-1337, 2334-2364, 3628-3668; repositories/investigation.py:536-616 | Full build pages all finding versions, then _finding_context sorts by finding UUID. The operator section named findings requests latest_only=True, whose SQL and in-memory adapters remove superseded versions; the section source is ordered by created_at/id before projection. | Same section name has different membership/count between Bridge context and operator paging. The projected finding order is UUID order, not the keyset source order, so page concatenation is not globally ordered by the source tuple. Whether full context should contain history is an unresolved caller contract; the divergence itself is verified. |
| Evidence index: research_context_builder.py:1091-1100, 1325-1331, 2301-2333, 3185-3214 | Full build reverses the descending observation page, then _evidence_index sorts by UUID and applies count/byte caps. The paged section seeks by observed_at/id descending and maps each source row in page order. | Same section name uses different order and selection policy. IDs may be the same while ordering and which items fit a byte budget differ. No cross-path equality oracle was found. |
| Context identity and cursor provenance: operator_tool_invocation.py:350-434, 485-578, 580-640; research_context_builder.py:2236-2456; investigation_contracts.py:341-401; operator_context_summary.py:192-194, 228-250 | Full context and its summary expose SHA-256 of canonical context bytes. Source-paged sections pass state.sha256 to the page builder and return that value as context_sha256; materialized sections pass the full context digest. Both page contracts separately expose working_state_sha256 and context_sha256. | Verified field-provenance divergence. Tests intentionally bind keyset cursors to state.sha256, but the v1 field has no description distinguishing that from the full context digest. Treating the two values as interchangeable is a caller hazard; intended semantics are unresolved. |
| Cursor integrity: context_pagination.py:82-160, 215-253; operator_tool_invocation.py:454-509; test_operator_tool_invocation.py:1631-1798 | Cursor is explicitly an unauthenticated position, scoped to section/investigation/direction and optional SHA. The service checks the supplied state CAS. Page SHA binds the returned page metadata and items. A test deliberately re-encodes a different in-scope pivot and receives a valid page proof. | This is position integrity for returned page construction, not proof that the client followed the previous page or that the underlying rows stayed frozen. A client can move within the authorized section and skip rows; it cannot use that cursor to broaden the section/investigation scope. |
| Omission categories: context_pagination.py:18-24, 27-57; research_context_builder.py:1432-1483, 1733-1836, 2440-2456; operator_tool_invocation.py:137-244 | Counts and reasons are validated. PAGINATION covers rows outside a source page, NOT_SELECTED covers source rows excluded by projection, BUDGET covers later context reduction, and SOURCE_NOT_PAGINATED labels materialized inputs. In-memory paging and list fallback report source_bounded=false with a reason. SENSITIVITY and TOMBSTONE are enum values with no use in these projection call sites. | Omission accounting is visible for the main source paths; a page’s PAGINATION count means absent from this page, including records returned on another page. It is not a count of permanently lost canonical rows. |
| Latest tool results: research_context_builder.py:92-103, 3524-3579; investigation_contracts.py:212-245 | The context retains the latest 16 invocations in latest_tool_results. That field has no dedicated truncated flag/count. tool_coverage is a separate, larger projection with its own truncation flag and page path. | No evidence of canonical-record loss; this is a less observable cap on one projection. The broader projection does not itself tell a caller which 16-result subset was omitted. |
| Export artifact derivation: investigation_audit_export.py:1668-1777, 1846-2108, 2199-2386, 2387-2567, 2568-2578 | Events are projected once. Index, trace and manifest all derive from that event list and its serialized bytes; manifest checks file hashes/counts. Missing relations are recorded as unavailable_resources or UNKNOWN gaps. | No incompatible recomposition was found among the four export files. They share one derived event source. The export is a current-state snapshot, not a transition journal; invocation/state transition history absent from canonical append-only rows cannot be reconstructed (900-988, 1002-1055, 1624-1665). |
| Invocation ↔ observation links: investigation_audit_export.py:913-915, 1058-1084; domain/investigation.py:431-441, 529-540; investigation_service.py:2037-2070 | Export emits ToolInvocation.response_refs and EvidenceObservation.invocation_ref from separate stored fields. The service’s observation/result path writes these links, but the projection does not cross-check reciprocity. | No mismatched persisted example was inspected. The exporter can represent inconsistent directions without flagging them; actual database consistency remains UNKNOWN pending a focused oracle. |

## SQL-versus-fake divergence

| Surface | What tests/adapters establish | What remains unproved |
|---|---|---|
| In-memory keyset pages | _memory_keyset_page loads and sorts the whole collection, then returns a page and marks source_bounded=false (investigation_persistence.py:201-246, 274-292, 355-373, 403-425). Context builder tests instrument list calls and assert this flag/reason (test_research_context_builder.py:3645-3899). | The fake is not evidence that a production database read is bounded. |
| SQL context pages | SQL uses tuple seek on (timestamp, UUID), stable matching ORDER BY, separate total count, and limit+1 (repositories/investigation.py:145-156, 188-241, 377-430, 546-616). The offline test compiles the predicate and uses SessionSpy to assert the shared predicate and limit+1 (test_mission01_l2_context_pagination.py:197-272). | No PostgreSQL query execution, concurrent page walk, transaction-isolation, query plan or performance evidence comes from SessionSpy. |
| State CAS | In-memory lock and SQL row lock/update implement the same visible version rule (investigation_state.py:65-86; repositories/investigation.py:1356-1411). | SQL locking/concurrency behavior was not exercised by this lot’s test command. |
| Audit export | test_investigation_audit_export.py builds AuditSourceSnapshot values with SimpleNamespace fixtures and tmp_path files (lines 14-90 onward). It tests deterministic projection, file coherence, resume and metadata behavior. The reader module itself states PostgreSQL runtime is UNKNOWN (investigation_audit_reader.py:1-4). No test reference to load_investigation_audit_snapshot or read_investigation_audit_snapshot was found. Mission 02 also marks real-PostgreSQL audit export UNKNOWN/TODO (docs/mission02/FINAL_AUDIT.md:15, 102-111). | The known audit-export-from-real-PostgreSQL gap remains open. The test suite proves projection from supplied values, not that the SQL snapshot loader returns a complete/correct AuditSourceSnapshot. |
| Existing PostgreSQL context test | test_operator_context_summary_postgres.py appends an observation while the working-state version/SHA remain unchanged, then reads the summary and evidence page (lines 108-144). | This demonstrates that working-state CAS is not a frozen source-row snapshot. It does not test a multi-page cursor across such a mutation or prove what clients should expect. |

**Tests run in this lot:** from backend/:

    PYTHONDONTWRITEBYTECODE=1 .venv/bin/pytest -p no:cacheprovider -q tests/test_investigation_audit_export.py tests/test_mission01_l2_context_pagination.py tests/test_operator_tool_invocation.py::test_context_section_keyset_cursor_is_opaque_chained_and_offset_compatible tests/test_research_context_builder.py::test_context_build_pages_large_sources_with_fixed_memory_budget_and_manifest --tb=short
**Result:** 40 passed in 3.47s. These tests use synthetic data, memory adapters or SessionSpy. No PostgreSQL, Docker, MinIO, provider, CTI network or integration test was run.

## Candidate classification and replacement burden

For every replacement claim, require all of: a reproduced observable problem; exact affected callers; currently dispersed invariants; a named single owner; caller obligations that disappear; concrete code paths deleted; a new oracle at the external interface; and migration/regression cost. No replacement design is made in this lot.

### Audit snapshot reader + event/file exporter — KEEP

- **Observable problem / callers:** No projection inconsistency was found within RUN_EVENTS/RUN_INDEX/RUN_TRACE/RUN_MANIFEST. The visible production caller is the CLI at backend/scripts/export_investigation_audit.py:40-76; unit tests call the pure projection directly.
- **Dispersed invariants / possible owner:** Repeatable-read SQL loading is in investigation_audit_reader.py; event/projection/file integrity is already in investigation_audit_export.py. These are different concerns joined by AuditSourceSnapshot.
- **Obligations/code deletion:** A facade could move PostgreSQL/MinIO construction out of the single CLI caller, but no duplicate invariant code would disappear. A wrapper would be shallow. No existing source path is selected for deletion.
- **Oracle / cost:** A disposable-PostgreSQL export test is the narrow missing proof. It can seed canonical rows and use a test blob store; no schema change is evidenced. Regression cost is checking source inclusion, explicit UNKNOWNs, all four file digests/counts, and resume.

### PostgreSQL audit reader validation — NARROW_FIX (oracle only)

- **Observable problem / callers:** Runtime output is UNKNOWN, not a demonstrated bad export. The CLI is the affected caller.
- **Dispersed invariants / owner:** The read adapter already owns a read-only repeatable-read snapshot and selected blob hash verification; the pure exporter owns serialization. Keep those owners.
- **Obligations/code deletion:** The test should call the public snapshot wrapper and feed its result through the exporter. It deletes no production code and removes no caller obligation.
- **Oracle / cost:** Add a real disposable-PostgreSQL test with representative invocation, observation, finding/version, state, opinion, decision, conversation-turn and model-run records; assert references and output hashes. Cost is test setup only; runtime/schema regression risk is UNKNOWN until executed.

### Full versus paged context projections — NARROW_FIX

- **Observable problem / callers:** Verified membership/order differences for findings and evidence_index affect Bridge full context and operator section readers.
- **Dispersed invariants / possible owner:** Selection lives in ResearchContextBuilder.build, read_context_section_page, _finding_context/_evidence_index, and repository latest_only/page semantics. ResearchContextBuilder already owns most projection depth; a replacement is not established.
- **Obligations/code deletion:** A caller should not have to infer whether “findings” means current rows or all revisions, or which order/budget policy applies. A narrow repair would align the two existing paths; no code is deleted in this diagnosis. A replacement would have to remove the duplicated selection path in build versus read_context_section_page, not add another wrapper.
- **Oracle / cost:** Fixture with superseded versions and equal-timestamp rows; compare full-context membership/order/counts with all paged findings plus a separate history section. Compare full and paged evidence_index order and byte selection. A behavior change alters context payloads, hashes and potentially Bridge input; replay/regression cost is material but unmeasured.

### Operator page identity and source snapshot semantics — BLOCKED_DESIGN

- **Observable problem / callers:** The page field context_sha256 represents state SHA on source-keyset pages and full-context bytes SHA on materialized pages. Source rows can change while the working-state CAS remains unchanged; the existing PostgreSQL test demonstrates such an append.
- **Dispersed invariants / possible owner:** State CAS is owned by OperatorToolInvocationService/InvestigationStateRepository; source pagination is owned by repositories and ResearchContextBuilder; the full context hash is owned by get_context. No one owner currently defines a frozen row-set identity for section cursors.
- **Obligations/code deletion:** A replacement cannot be justified until the contract decides whether a page continuation means “current rows under this state” or “the same source set that produced the summary.” No current code deletion follows from the evidence; changing v1 cursor/page identity could invalidate clients’ cursors.
- **Oracle / cost:** PostgreSQL test that obtains page one, appends a row without changing state, then follows the cursor; assert the chosen snapshot/staleness contract, total and no duplicate/lost row. Also assert the meaning of context_sha256 across summary and page types. API-v1 compatibility and cursor replay cost are UNKNOWN.

### Shared page proof and entity repository interfaces — KEEP

- **Observable problem / callers:** Generic page hashing/manifest checks are already shared; SQL and memory adapters conform to per-entity Protocols. The fake’s materialization is accurately declared rather than hidden.
- **Dispersed invariants / possible owner:** Generic proof belongs in context_pagination; entity ordering/seek belongs in each SQL repository; context row projection belongs in ResearchContextBuilder; state CAS belongs in the state repository. This split follows current ownership.
- **Obligations/code deletion:** Merging all of these into a new module would not remove current query or projection code and would combine distinct entity invariants. No deletion evidence supports a replacement.
- **Oracle / cost:** Keep a real PostgreSQL adapter test for stable ordering, limits, same-timestamp UUID ties and finding latest_only parity. No migration is indicated; SQL runtime cost remains UNKNOWN.

### End-to-end audit service in InvestigationService or an HTTP route — DEFER

- **Observable problem / callers:** No API export caller or missing runtime workflow was specified. The actual export entry point is the CLI; InvestigationService’s audit methods handle investigation decisions, not export files.
- **Dispersed invariants / possible owner:** Snapshot read, projection and file checkpoint have separate owners and one CLI composition. New facade ownership is not demonstrated by multiple callers or varying adapters.
- **Obligations/code deletion:** Moving CLI composition would delete only wiring from one caller; absent a cross-layer invariant to centralize, that is a wrapper. No production code path should be deleted on current evidence.
- **Oracle / cost:** Real-PostgreSQL audit export oracle first. API/auth/version contract and operational deployment costs are unspecified; defer any route/service replacement.

**REPLACE_CANDIDATE:** none currently meets the deletion test. The context differences have a concrete narrow correction path; the audit issue is an oracle gap; the remaining questions require a contract decision or measurement.

## Missing oracles

1. **PostgreSQL audit export:** execute the reader wrapper and the pure exporter against seeded disposable PostgreSQL, with a test artifact store; assert selected canonical references, source versions, explicit unavailable/UNKNOWN fields, hashes/counts for each file, and resume behavior. Do not infer this from synthetic-snapshot tests.
2. **Finding semantics:** establish one expected definition of “findings” across full context and paginated endpoint, while keeping finding history separately addressable; test superseding chains, equal timestamps, order, totals and continuations.
3. **Evidence index semantics:** decide whether full and paged projections must share order/budget; test a fixture where UUID order differs from observed-time order and the byte limit excludes rows.
4. **Cursor source identity:** test a continuation while canonical rows change but state version/SHA do not; assert whether it becomes stale or remains a live query. Confirm context_sha256 semantics for source and materialized page types.
5. **Stored link consistency:** if response_refs and invocation_ref are intended to be reciprocal, construct a conflicting canonical pair and assert export refuses or reports the discrepancy. No live mismatch is claimed.
6. **Latest-result cap visibility:** assert/document the intentional latest-16 projection and whether a separate count/flag is required; tool_coverage is not identical to latest_tool_results.
7. **SQL adapter parity:** existing SessionSpy evidence is insufficient for database execution, concurrency, query plans and performance. Use only disposable PostgreSQL for this oracle; it was not run here.

## UNKNOWNs

- PostgreSQL audit snapshot completeness and transaction behavior have not been exercised; the module documents this directly.
- Real SQL page behavior under concurrent inserts and count/page query snapshots; no EXPLAIN or performance evidence.
- Whether finding history in full context and UUID order in evidence_index are intended product semantics or bugs.
- Whether operator page context_sha256 is meant to identify working state, full context bytes, or a stable source set.
- Whether the invocation/observation link fields are guaranteed reciprocal by all historical writers.
- Runtime scale of the audit reader’s all-record lists and per-turn model-run get calls.
- Whether a missing latest_tool_results truncation signal is an accepted “latest” projection contract.
- No live PostgreSQL, MinIO, provider or CTI source was accessed.
