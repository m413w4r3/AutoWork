# Mission 06-R FINAL, Lot C: E2E OSINT audit

Base: `66d5976638e2c9c60d44ffc8a4b0fa3f5dcb24c0` (`main`). Scope is offline
PostgreSQL scenarios and current persistence/audit code. No real provider was
contacted. Verdicts distinguish tested coverage from runtime possibility.

## Journey coverage

| Step | Proving node | Authority | Persisted rows / assertions | Verdict |
|---|---|---|---|---|
| 1. New investigation inherits a pinned policy with no individual G2 | `test_osint_open_policy_postgres.py::test_concurrent_start_pins_one_policy_envelope_without_human_decision` (213–242) | OSINT_POLICY | Policy event/version, `osint_open_inheritances`, `investigation_envelopes`; no `analyst_decisions`; concurrent starts reuse one version/event | PASS |
| 2. Seed report then a second VT pivot | `test_investigation_vertical_postgres.py::test_real_postgres_http_bridge_vertical_investigation_and_v6_publication` (196–218); OSINT read control: `test_virustotal_external_effects_postgres.py::test_osint_open_inherited_investigation_uses_vt_coordinator_and_hides_credentials` (520–607) | Vertical: HUMAN_G2. Read control: OSINT_POLICY | Vertical runs `file_report` then `intelligence_search`; OSINT test runs two hash `file_report`s, not a chained pivot. Invocation/claim/evidence rows are checked separately | PARTIAL |
| 3. Acquire a candidate | Vertical node above (220–238); `test_investigation_e2e_synthetic_sample_postgres.py::test_investigation_e2e_synthetic_sample_postgres` (457–491) | HUMAN_G2 in both | Fake hash-checked download, budget, acquisition attempt and candidate association are exercised. No combined OSINT_POLICY download scenario; S3-VT-DOWNLOAD is not integrated at this base | PARTIAL |
| 4. Verify SHA-256 and quarantine bytes | Vertical node (303–325, 376); synthetic sample node (493–540) | HUMAN_G2 | Candidate association, successful `sample_acquisition_attempts`, sample state, blob SHA/bucket and bytes are asserted | PARTIAL |
| 5. Analyze candidate before G3 | Vertical node (344–371, then G3 at 391–405); synthetic sample node (542–579) | HUMAN_G2 | Vertical records `sample.analyze` while qualification is still candidate, but allows success, partial, or failure. Synthetic node runs fake local measurement before its explicit G3, not the `sample.analyze` tool | PARTIAL |
| 6. Bridge/advisor consult with cited dossier and a reason for the turn | Vertical node scripts Bridge (145–238); synthetic sample node invokes advisor with refs (623–715) | HUMAN_G2 in both | Vertical checks persisted findings/refs and replay count, not the requested hypothesis-change/discriminating-observation rationale in the submitted dossier. Synthetic advisor uses a separate fixture after G3. No continuous OSINT_POLICY Bridge/advisor test; S3-BRIDGE is not integrated | BLOCKED |
| 7. Persist result, limits, next decision | Vertical node (327–375); audit node below | HUMAN_G2 | `investigation_finding_versions` carries statement, assessment, observation refs, scope and limitations; `analyst_decisions` records pivot approval. The tests do not prove a durable hypothesis-transition reason linked to that pivot | PARTIAL |
| 8. Rebuild timeline and evidence links from audit | `test_investigation_audit_export_postgres.py::test_postgres_audit_snapshot_projects_scoped_rows_and_resumes_without_reread` (808–1327) | Synthetic canonical-row fixture; no approval journey | Verifies a scoped read-only snapshot, IDs/refs and bundle integrity/resume. Projection intentionally hashes or omits source text; it does not load claims/uses or acquisition attempts | PARTIAL |
| 9. Replay without another emission | Vertical node (373–389); audit node above verifies bundle resume | Vertical: HUMAN_G2 | Re-dispatch of the succeeded job leaves VT download and scripted Bridge submission counts unchanged | PARTIAL |

J2 is a browser scenario server, not a pytest node: `tests/workflow_emulator/investigation_browser_server.py:1,788`.
The workflow-emulator W13/W16 tests cover other resumptions/revocations, not this OSINT_OPEN journey.

## Audit-chain reconstruction from canonical rows

| Chain link | Canonical rows / fields | Reconstruction result and exact gap |
|---|---|---|
| Hypothesis | `investigation_working_states.state` JSONB (`hypotheses`, `agenda`, `recent_actions`); version/turn index. `investigation_experiments` has `hypothesis_id`, versioned `payload`, `claim_ids`, payload SHA. | Current state and versioned experiments are recoverable. The working-state row is keyed by investigation, so it is a current snapshot, not transition history. No persisted hypothesis transition reason; exporter sets `decision_reason` UNKNOWN (`investigation_audit_export.py:1015–1029`). |
| Pivot justification | `tool_invocations.purpose`, `normalized_parameters`; optionally `analyst_decisions.reason`, `target_id`, `correlation_id`, `details`. | Raw purpose/reason can be read from canonical rows. There is no enforced foreign key/required edge from a hypothesis transition to the justification and following invocation. Audit projection emits only purpose/reason hashes (`:925–929`, `:1163–1173`), so the audit bundle alone cannot recover their text. |
| Operation and response | `tool_invocations.id`, `operation_key`, tool/schema, status, `response_refs`; `external_effect_claim.invocation_id`, family, canonical request/ref hashes, `evidence_refs`, reservation amounts, envelope version; `external_effect_use.claim_id`, `invocation_id`, `observation_refs`, `budget_delta`. | Explicit invocation/claim/use IDs support operation and effect reconstruction when populated. The DB does not require each response ref to resolve to an observation. The audit reader/projector has no external-effect claim/use rows or events (`investigation_audit_reader.py:60–80,140–165`; `investigation_audit_export.py:1699–1747`). |
| Cost | Invocation `reserved_budget`/`consumed_budget`; `investigation_budget_reservations` and `_amounts` by `operation_key`; claim `reservation_amounts`; use `budget_delta`; `model_runs.usage`. | Budget-unit admission/consumption can be reconstructed. No monetary price/charge is persisted; audit reports cost UNKNOWN (`investigation_audit_export.py:961–964,1584–1589`). Requested invocation amount and transition history are also UNKNOWN (`:932–941`). |
| Evidence | `evidence_observations`: `id`, `blob_id`, SHA-256, source/version, object, limitations, `invocation_ref`; finding observation IDs; claim/use evidence/observation refs. | Response→observation→blob/finding is linkable by IDs and hashes when refs are written. Audit events retain IDs/blob refs, not content (`:1065–1084`, `:1208–1232`). |
| Sample acquisition | `sample_acquisition_attempts`: investigation/requested hash, outcome, sample ID, time; `samples`: blob, expected hash, state; `sample_associations` and qualification-change reason/actor; claim `acquisition_attempt_id`. | DB rows and quarantine blob descriptor can establish successful hash-verified acquisition and candidate status. The audit snapshot does not read acquisition attempts or sample rows, and has no acquisition event; association projection alone is insufficient to prove bytes were acquired. |
| Interpretation | Versioned finding statement/assessment, observation and claim refs, scope, limitations, supersedes; optional versioned opinion content. | Canonical rows preserve interpretation and its evidence. The audit projection exports statement/scope hashes and limitations counts, not their text (`:1215–1231`); fetch authorized canonical rows/blob refs to read them. |
| Next decision / Bridge | `analyst_decisions` stores decision type/target/reason/correlation/details; latest working-state JSON; opinion versions. Turns link to model runs through `model_conversation_turns.model_run_id`; claim can link turn+run. | No historical working-state transition log or explicit Bridge `next_action` event is captured by this audit snapshot. Decision reasons are hashed. `tool_invocations` has no turn/run FK; the exporter says consultation ID and Bridge-run ID are absent, and turn/run linkage is conditional on verified advisor metadata (`:981–985`, `:2410–2444`). `jobs`/`job_events` and claim/use rows are not projected, so audit-bundle replay chronology is incomplete. |

**Audit verdict:** raw canonical rows support most operation→evidence→finding joins, plus unit-budget and sample provenance checks. They do not provide a complete hypothesis→justification→pivot→interpretation→next-decision history. The current audit bundle preserves useful IDs/hashes and resumes deterministically, but omits effect/acquisition rows and source text; it cannot independently establish the full required chain. This finding is about current schema/projection coverage, not a proposal for a new logging framework.

## Proposed lot verdicts

| Gate | Verdict | Basis |
|---|---|---|
| OSINT_OPEN_NO_REPEAT_G2 | PASS | New-start concurrency test pins one OSINT_POLICY envelope/event with zero human decisions. |
| CANDIDATE_LOCAL_ANALYSIS | PARTIAL | Candidate analysis is attempted before G3 in HUMAN_G2 vertical coverage; success is not required and OSINT_POLICY coverage is absent. |
| E2E_OSINT_CONTINUOUS | BLOCKED | No single PG scenario proves the full journey under OSINT_POLICY; S3-VT-DOWNLOAD and S3-BRIDGE remain unintegrated. |
| AUDIT_DECISION_CHAIN | PARTIAL | Explicit refs cover much of operation→evidence→finding, but hypothesis-transition rationale, the enforced pivot edge, and some export rows are missing. |

## Execution and parent-run nodes

No test node executed. `uv run` could not create a temporary file under the
read-only default uv cache; system `pytest` collection then failed because
`pefile` is absent (`ModuleNotFoundError`). No provider was contacted and no
G3 approval was submitted by this audit.

Recommended PG nodes for a configured parent environment:

1. `tests/integration/test_osint_open_policy_postgres.py::test_concurrent_start_pins_one_policy_envelope_without_human_decision`
2. `tests/integration/test_virustotal_external_effects_postgres.py::test_osint_open_inherited_investigation_uses_vt_coordinator_and_hides_credentials`
3. `tests/integration/test_virustotal_external_effects_postgres.py::test_osint_open_vt_read_accepts_investigation_observation_without_association`
4. `tests/integration/test_investigation_audit_export_postgres.py::test_postgres_audit_snapshot_projects_scoped_rows_and_resumes_without_reread`

The vertical and synthetic-sample nodes are useful HUMAN_G2 controls, but both
explicitly perform G3 later in their fixture; they do not upgrade the OSINT
verdicts above.
