# M06 runtime readiness preflight

**Assessment point:** `505c1cd2c525744bee5d768cccc1a830abce2d2b`  
**Scope:** static, read-only source and contract inspection. No provider, application, database, object store, container, worker or analyst tool was contacted or run. Tests below are source-level examples only; none were executed.

## How to read these classifications

- **READY** means the repository contains a bounded implementation contract and source/test evidence. It does **not** prove a deployment is configured, reachable, authorized or ready for a real case.
- **PARTIAL** means the code path exists, but important runtime, policy, evidence or completeness conditions are unresolved.
- **BLOCKED** means the checked-in default explicitly disables that capability. An override may exist in the deployment; it was not inspected.
- **UNKNOWN** means static source cannot establish the relevant live fact. UNKNOWN is never a pass.

The current deployment’s readiness for every external or local runtime capability below is **UNKNOWN**. Where defaults are disabled, the code-default assessment is **BLOCKED** as well. There are zero verified live READY claims.

## A. VirusTotal

| Capability / claim | Classification | Evidence and limit |
|---|---|---|
| File report (`GET /files/{hash}`; legacy `/file/report`) | READY contract; live UNKNOWN; default BLOCKED | V3 and V2 route construction: `backend/src/cti_app/infrastructure/virustotal.py:943-964,1557-1606`. Capability and route opt-ins default false: `backend/src/cti_app/config.py:215-229`; adapter intersects supplied capability, setting and ready route: `backend/src/cti_app/infrastructure/virustotal.py:1507-1540`. |
| File relationships (`GET /files/{hash}/{relationship}`) | READY contract; live UNKNOWN; default BLOCKED | Relationship is enum-validated and paginated: `backend/src/cti_app/infrastructure/virustotal.py:1020-1036`; proxy V3 only route: `:1596-1606`; opt-in defaults false: `backend/src/cti_app/config.py:215-218`. |
| Intelligence search (`GET /intelligence/search`) | READY contract; live UNKNOWN; default BLOCKED | Query validation, proxy route and pagination: `backend/src/cti_app/infrastructure/virustotal.py:1123-1147,1596-1606`; opt-in defaults false: `backend/src/cti_app/config.py:215-218`. Account permission and actual proxy access are unknown. |
| Behaviour reports (`GET /files/{hash}/behaviours`) | READY contract; live UNKNOWN; default BLOCKED | Capability-gated paginated call: `backend/src/cti_app/infrastructure/virustotal.py:1090-1121`; proxy route: `:1596-1606`; default disabled: `backend/src/cti_app/config.py:218`. |
| Proxy transport and route policy | READY contract; live UNKNOWN; default BLOCKED | Proxy client disables ambient environment proxying and redirects: `backend/src/cti_app/infrastructure/virustotal.py:122-158`. Route activation requires proxy URL/base URL and per-capability opt-in; direct VT additionally needs client, key and legacy URL: `:1464-1495`. Default proxy URL is absent: `backend/src/cti_app/config.py:206-222`. |
| Direct and fallback routes | PARTIAL; live UNKNOWN; default BLOCKED | File report is the only operation with optional fallback steps. V3 proxy fallback and direct V2 each need their own explicit flags; key/base URL presence alone does not enable them: `backend/src/cti_app/infrastructure/virustotal.py:1557-1611`; flags default false: `backend/src/cti_app/config.py:223-228`. Other capabilities remain proxy-only. |
| Download endpoints and byte verification | READY contract; live UNKNOWN; default BLOCKED | Direct `/files/{hash}/download`, then `/files/{hash}/download_url`, with legacy fallback only after configured missing-route evidence: `backend/src/cti_app/infrastructure/virustotal.py:440-458,492-545`; streaming enforces max bytes and expected hash/family: `:492-513`. Download is off by default: `backend/src/cti_app/config.py:237-252`. |
| Signed download redirect handling and allowlist | READY contract; live UNKNOWN | HTTPX redirects are disabled; each redirect is manually followed with a fresh credential-free client: `backend/src/cti_app/infrastructure/virustotal.py:122-133,2041-2048,2069-2075`. Targets must be HTTPS, have no URL credentials, and match exact configured host and port: `:1940-1968,2162-2164`. Defaults cap at 3 redirects, 200 MiB and 120 seconds, and allow the required VT CDN host on port 443: `backend/src/cti_app/config.py:243-252`. Tests `backend/tests/test_virustotal_transport_contracts.py::test_v3_signed_redirect_is_manual_and_cdn_client_has_no_credentials` and `::test_each_redirect_target_requires_https_exact_host_and_approved_port` use deterministic HTTP transports; they do not prove real DNS, proxy, CDN or account behavior. |
| Provider quota / account entitlement | UNKNOWN | The code maps throttling and applies investigation budgets, but no provider account or quota was inspected. Global VT units and acquisition caps default to zero: `backend/src/cti_app/config.py:385-392`; G2 and tool reservation determine whether an invocation can proceed: `backend/src/cti_app/application/investigation_governance.py:872-879`. HTTP 429 handling is not proof of available quota. |
| Investigation budgets | READY contract; live UNKNOWN | G2 governs VT units/downloads and reservations can refuse or require review; global autonomous caps default to zero: `docs/investigation_operations.md:19-50`, `backend/src/cti_app/config.py:385-392`. A valid case envelope and remaining budget must be read for the target case. |
| Raw response evidence archive | READY contract; storage UNKNOWN | Successful raw VT response pages are recorded as evidence with canonical blob/SHA references; errors are not promoted to successful response evidence: `docs/investigation_vt_tools.md:3-7,69-74`. Offline contracts: `backend/tests/test_investigation_vt_tools.py::test_relationship_page_archive_is_untyped_and_summary_remains_typed` and `::test_raw_archive_is_typed_and_summaries_exclude_secrets_and_signed_urls`. These use fakes and cannot establish production DB/MinIO availability. |
| New download versus verified local reuse | READY contract; storage UNKNOWN | Prior or same-subject quarantined samples are reused only after policy and requested SHA-256 byte verification; reuse consumes zero VT/download units: `backend/src/cti_app/application/investigation_acquisition.py:201-243,245-288`. Otherwise new acquisition follows explicit G2/TLP and budget checks; offline tests include `backend/tests/test_investigation_acquisition_tools.py::test_corrupt_subject_sample_bytes_are_not_reused` and `::test_subject_sample_from_another_subject_is_not_reused`. No sample/storage was inspected here. |
| Bounded pagination | READY contract; live UNKNOWN | Page size, page count and result count are bounded; cursor and non-exhaustive status are preserved: `backend/src/cti_app/infrastructure/virustotal.py:1164-1222`; defaults are 40/100 items, 10 pages, 1,000 results: `backend/src/cti_app/config.py:233-236`. Offline tests: `backend/tests/test_virustotal_adapter.py::test_pagination_cursor_and_max_pages`, `::test_max_results_marks_result_non_exhaustive`; application partial-result test `backend/tests/test_investigation_vt_tools.py::test_page_ceiling_returns_partial_with_cursor_and_provider_route`. |
| VT upload/submission | BLOCKED for this scope; authorization absent | No submission is authorized by this preflight. VT capability model contains a submissions flag, but the inspected investigation module documents acquisition outside its read-only tool set: `backend/src/cti_app/application/virustotal.py:202-221`, `docs/investigation_vt_tools.md:112-113`. Do not infer an enabled submission path from the model field. |

## B. Bridge consultation

| Capability / claim | Classification | Evidence and limit |
|---|---|---|
| Transport selection and endpoint | READY contract; live UNKNOWN; default BLOCKED | Responses transport uses `POST /responses` and `GET /responses/{id}`: `backend/src/cti_app/integrations/models.py:90-110`. Runtime composition selects the Bridge turn transport only when endpoint/key and model route are configured: `backend/src/cti_app/infrastructure/investigation_runtime_ports.py:831-865`. Default endpoint is loopback and has no key; `auto` route defaults to ChatGPT Bridge: `backend/src/cti_app/config.py:105-107,140-145`. The default loopback alone is explicitly not enough to count as configured. |
| Capability preflight and conversation archive | PARTIAL; live UNKNOWN | Client can GET `/bridge/capabilities` (cached for 15 seconds) and DELETE `/bridge/conversations/{id}`: `backend/src/cti_app/integrations/models.py:541-582`. Neither route was called; Bridge service health and extension/UI availability remain UNKNOWN. |
| Consultation types | READY contract; live UNKNOWN | Supported types are `plan_investigation`, `pivot_strategy`, `yara_draft`, `review_text`, `draft_text`, `suricata_draft`, `figure_plan`; only draft/review accept editorial units: `backend/src/cti_app/application/investigation_advisor_tools.py:306-353`. Tool is registered as `advisor.consult`: `:89-100`. |
| Dossier and evidence selection | READY contract; evidence-store availability UNKNOWN | Explicit refs are loaded; selected findings may contribute linked observation refs: `backend/src/cti_app/application/investigation_advisor_tools.py:413-455`. Dossier text, manifest, SHA, byte count and transmitted refs are constructed; credential/signed-URL checks run before transport: `:684-728`. Completeness/truncation preflight can refuse a submission: `:764-790,852-862`. |
| References sent to Bridge | READY contract; live UNKNOWN | Dossier-derived transmitted observation/article refs are computed before submission and recorded in provenance: `backend/src/cti_app/application/investigation_advisor_tools.py:715-728,852-862,1219-1264`. The prompt contains selected material rather than automatically transmitting every repository artifact. Exact payload depends on the case and must be reviewed under its G2/TLP and external disclosure rules. |
| Prompt archive before submit | READY contract; blob-store availability UNKNOWN | Exact prompt text and dossier artifacts are persisted to `advisor-prompts` before bridge invocation: `backend/src/cti_app/application/investigation_advisor_tools.py:1020-1047`. Archive failure is treated as review/refusal rather than silently sending an unarchived prompt: `:1106-1124`. Faked artifact stores in `backend/tests/test_investigation_advisor_tools.py:108-134` cannot prove live persistence. |
| Cost, quota and context budget | PARTIAL; live provider cost UNKNOWN | G2 prompt bytes/tokens are measured against reservation before transport: `backend/src/cti_app/application/investigation_advisor_tools.py:728-760`. Provenance stores consumed budget and provider usage when supplied: `:1219-1264,1305-1396`. Token estimate is derived from bytes; actual provider billing/remaining account quota cannot be inferred from estimate. `backend/tests/test_investigation_api.py::test_operator_bridge_cost_projection_separates_reported_usage_and_estimate` checks the distinction offline. Default tool reservation is 3,600 seconds: `:97-111`; Run12 historical notes a previous reservation denied before transport (`docs/operations/run12-editorial-closure-2026-10-22.md:11-20`), not current runtime evidence. |
| Status polling and pending turn recovery | PARTIAL; live UNKNOWN | Operator status reads durable pending Bridge turn ID/status/error/key and invocation cost fields: `backend/src/cti_app/api/investigation.py:1614-1629,1701-1754`. Model transport can retrieve a response by ID: `backend/src/cti_app/integrations/models.py:90-110`; the application’s Bridge submit path has idempotency and reconciliation logic: `backend/src/cti_app/application/bridge_investigator.py:583-665`. This does not prove the Bridge/UI has a currently recoverable job or that an advisor call is asynchronously pollable end-to-end. |
| Partial / ambiguous output | READY contract; live UNKNOWN | Parser and status mapping preserve partial and ambiguous outcomes; ambiguous submission is not blindly retried: `backend/src/cti_app/application/investigation_advisor_tools.py:3585-3795`. Offline test `backend/tests/test_investigation_advisor_tools.py::test_advisor_transport_failures_are_explicit_and_never_retried` supplies fake success-empty, 429 and timeout results; it does not establish provider behavior. Batch cursor and unprocessed IDs survive ambiguity: `::test_editorial_batch_retains_cursor_and_stops_after_ambiguous_result` at `:5243-5288`. |
| Invocation reconciliation | READY contract; human action required | `POST /api/investigations/{investigation_id}/invocations/{invocation_id}/reconcile` accepts `executed_with_result`, `not_executed`, or `abandon`, expected version, response refs, reason and idempotency key; actor identity is resolved before mutation: `backend/src/cti_app/api/investigation.py:3847-3911`. Test `backend/tests/test_investigation_api.py::test_invocation_reconciliation_route_uses_verified_actor_cas_and_idempotency_key`. Reconciliation must be based on operator-verified facts, not a timeout guess. |
| Successful real Bridge call | UNKNOWN | The tests use `Transport` fakes with `real_network_calls = 0`: `backend/tests/test_investigation_advisor_tools.py:65-74`; the test artifact store is in-memory: `:108-134`. Run12 recorded no successful Bridge transport because admission was denied before send (`docs/operations/run12-editorial-closure-2026-10-22.md:11-20`), a dated historical observation only. No live success is established by this review. |

## C. Local analysis

| Capability / claim | Classification | Evidence and limit |
|---|---|---|
| `sample.analyze` | READY contract; runtime/storage UNKNOWN; default BLOCKED | Resolves only a quarantined sample, dispatches the attested `sample_analysis` family, and records canonical binary-analysis and string observations with refs: `backend/src/cti_app/application/investigation_acquisition_tools.py:202-255,650-698`. Tool is local compute and candidate qualification does not itself qualify a sample: `:656-698`. Worker disabled by default: `backend/src/cti_app/config.py:318-328`. |
| `local.run_command` | READY contract; runtime UNKNOWN; default BLOCKED | Registered only when an attested catalog exists; accepts catalog command IDs/options, associates the requested sample hash to the investigation and records output: `backend/src/cti_app/application/investigation_local_tools.py:270-328,491-561`. `local_analysis_enabled=False`, strict filesystem confinement required, worker disabled by default: `backend/src/cti_app/config.py:294-328`. |
| Ghidra read-only operations | PARTIAL; runtime UNKNOWN; default BLOCKED | Five read tools: `ghidra.list_functions`, `ghidra.list_imports`, `ghidra.xrefs`, `ghidra.decompile_function`, `ghidra.search`: `backend/src/cti_app/application/investigation_local_tools.py:330-369`. Adapter checks a fixed `/v1/probe` service contract and Ghidra/Java versions; uses no ambient proxy and disallows redirects: `backend/src/cti_app/infrastructure/ghidra_mcp.py:71-185,330-366`. Defaults disabled, expected Ghidra 12.1.4 / Java 21.0.12.1: `backend/src/cti_app/config.py:359-368`. Startup probe and installed image were not inspected. |
| capa | PARTIAL; runtime UNKNOWN; default BLOCKED | `sample.analyze` composes static+capa+SMDA and marks the family available only if supported and each required engine is attested: `backend/src/cti_app/infrastructure/confined_analysis_dispatch.py:462-475,555-575`. Expected capa version/ruleset are pinned: `backend/src/cti_app/config.py:385-405`; worker defaults disabled: `:318-328`. Offline fake-worker example `backend/tests/test_investigation_acquisition_tools.py::test_sample_analyze_records_bounded_binary_observations` uses inert `b"MZ harmless static analysis fixture strings"`, fake VT and fake worker (`:685-770`); it also returns PARTIAL because fake worker and goodware service are partial/unavailable. This proves only the code path. |
| YARA | PARTIAL; runtime/rule qualification UNKNOWN; default BLOCKED | Generic command catalog exposes bounded `yara` matching; version-attested worker family requires YARA 4.5.0: `backend/src/cti_app/application/local_analysis.py:470-495`, `backend/src/cti_app/infrastructure/confined_analysis_dispatch.py:524-524`. Rule workbench is separately opt-in: `backend/src/cti_app/config.py:385-405`; worker disabled: `:318-328`. Synthetic fixture test is `backend/tests/test_yara_runner.py::test_isolated_worker_compiles_and_matches_synthetic_fixture`. Historical Run12 editorial closure records `ONE_RULE_NOT_VALIDATED` (`docs/operations/run12-editorial-closure-2026-10-22.md:36-50`); do not present that old fixture as validated current detection coverage. |
| PE and .NET | PARTIAL; runtime UNKNOWN; default BLOCKED | `objdump` accepts ELF and PE; `monodis` accepts PE and requires filesystem confinement: `backend/src/cti_app/application/local_analysis.py:435-468,496-546`; domain IDs include `objdump` and `monodis`, not a separate generic PE parser: `backend/src/cti_app/domain/local_analysis.py:10-21`. Worker also attests ILSpy command/package hash and monodis version: `backend/src/cti_app/infrastructure/confined_analysis_dispatch.py:485-497`; configured version pins are in `backend/src/cti_app/config.py:329-339,385-425`. Actual binary and package versions are UNKNOWN. |
| PCAP / PCAPNG | PARTIAL; runtime and capture availability UNKNOWN; default BLOCKED | `network.capture.inspect` only reads an associated quarantined capture; accepted input includes PCAP/PCAPNG with blob ID, SHA and size: `backend/src/cti_app/application/investigation_network_tools.py:50-73,76-109`. Tool inventory exposes tshark-dependent parsing and optional Suricata validation/replay offline: `:63-73,110-127`; expected versions/ruleset are pinned in `backend/src/cti_app/config.py:329-353,385-425`. No capture or runtime tool was inspected. VT behavior-PCAP is separately off by default: `backend/src/cti_app/config.py:237-252`. |
| Confinement and worker attestation | PARTIAL; current attestation UNKNOWN; default BLOCKED | Worker must report UID 10001, no-new-privileges, zero effective capabilities, read-only root, expected isolation profile, and DB/MinIO dependencies: `backend/src/cti_app/infrastructure/confined_analysis_dispatch.py:306-335,337-412`. Family availability checks exact versions and ruleset hashes: `:433-575`. Probe sends a broker message and performs runtime I/O, so it was deliberately not invoked. Defaults disable worker and require `bookworm-internal-v1`: `backend/src/cti_app/config.py:318-328`. |
| Tool versions | PARTIAL; installed versions UNKNOWN | Expected pins include file 5.44, binutils 2.40, xxd 2022-01-14, coreutils 9.1, Suricata 7.0.10: `backend/src/cti_app/config.py:329-353`; capa, SMDA, tshark/capinfos, .NET and monodis expected values: `:385-425`. `describe_tool_inventory` can report worker reachability, confinement, dependencies, versions and families: `backend/src/cti_app/infrastructure/investigation_runtime_ports.py:868-963`; this review found no public route exposing that full inventory (only the case-level status projection is exposed). |
| Saved local outputs | READY contract; canonical store availability UNKNOWN | Sample analysis requires a canonical result blob ID/SHA and archives a `BINARY_ANALYSIS` observation; string outputs are separately recorded: `backend/src/cti_app/application/investigation_acquisition_tools.py:236-255,650-698`. Ghidra output is bounded and records sample hash, operation, Ghidra/plugin versions, truncation and evidence ref: `backend/src/cti_app/application/investigation_local_tools.py:936-1023`. Local command output records evidence through the recorder: `:563-590`. Database/blob store health remains UNKNOWN. |
| Real Ghidra image verification | UNKNOWN | Adapter contract tests use mock HTTP transports (`backend/tests/test_ghidra_mcp_adapter.py:7-30`). An optional real image test exists and skips without installed Ghidra: `backend/tests/test_ghidra_service_real.py::test_real_headless_ghidra_inspects_a_generated_minimal_elf` (`:12-29`); it was not run. Its documented image command is in that test at `:15-18`. |

## D. Environment prerequisites and unknowns

| Prerequisite / fact | Classification | Evidence |
|---|---|---|
| VT proxy URL, API key, allowed route flags and direct clients | UNKNOWN in deployment; checked-in defaults BLOCKED | Defaults have no proxy URL/key, all read capabilities and downloads off, and both fallbacks off: `backend/src/cti_app/config.py:198-252`. Effective composition additionally requires proxy/base URL and enabled capability: `backend/src/cti_app/infrastructure/virustotal.py:1464-1540`. Runtime config was intentionally not read. |
| VT account entitlements and rate/credit quota | UNKNOWN | No offline source can establish current account entitlement or remaining provider quota. G2 caps and configured limits are not provider quota: `docs/investigation_operations.md:19-50`; global VT caps default zero: `backend/src/cti_app/config.py:385-392`. |
| Bridge base URL/key/route and Bridge UI/extension health | UNKNOWN in deployment; checked-in default is not configured | Defaults and selection gate: `backend/src/cti_app/config.py:105-107,140-145`; `_bridge_turn_transport_configured` rejects empty endpoint, rejects bare default loopback without key, and requires route selection: `backend/src/cti_app/infrastructure/investigation_runtime_ports.py:848-865`. No Bridge request was sent. |
| Case G2, TLP, `external_llm_allowed`, `do_not_submit`, revocation, remaining budgets | UNKNOWN per case | Operator status and envelope are case-specific and authoritative: `backend/src/cti_app/api/investigation.py:1614-1629,1650-1666,1757-1760`; code must recheck external disclosure before Bridge: `backend/src/cti_app/application/investigation_advisor_tools.py:466-533,1049-1100`. No case state was queried. |
| Postgres, MinIO/blob catalog and evidence storage | UNKNOWN | Worker attestation requires both dependencies: `backend/src/cti_app/infrastructure/confined_analysis_dispatch.py:354-356`; source archive paths depend on canonical stores. No connection was made. |
| Analysis worker, sandbox, binaries, exact versions and ruleset hashes | UNKNOWN in deployment; default BLOCKED | Worker and local execution default off; strict confinement default on: `backend/src/cti_app/config.py:294-328`. Attestation is runtime evidence only and checks confinement/dependencies/family versions: `backend/src/cti_app/infrastructure/confined_analysis_dispatch.py:306-412,433-575`. |
| Ghidra host/version/contract availability | UNKNOWN in deployment; default BLOCKED | Ghidra default is disabled with fixed internal base URL and expected versions: `backend/src/cti_app/config.py:359-368`; adapter checks host and startup contract: `backend/src/cti_app/infrastructure/ghidra_mcp.py:136-185,390-412`. |
| Docs versus current Ghidra settings | PARTIAL documentation mismatch | Operations docs say allowlist/path environment variables must be set (`docs/investigation_operations.md:118-120`), but the inspected adapter uses a fixed `/v1/probe` and `/v1/analyze` contract and fixed host validation (`backend/src/cti_app/infrastructure/ghidra_mcp.py:136-185,187-304,390-412`); current config exposes `ghidra_enabled`, base URL and version pins, not those documented path/allowlist fields (`backend/src/cti_app/config.py:359-368`). Resolve against the deployed contract before treating this checklist as a configuration recipe. |
| Runtime inventory access | PARTIAL | `describe_tool_inventory` builds safe binary/worker version and confinement details (`backend/src/cti_app/infrastructure/investigation_runtime_ports.py:868-963`), and app state stores the callable (`backend/src/cti_app/main.py:518-520`). Search of API route definitions found no public endpoint returning this full inventory; `/operator-status` provides case-level available/unavailable tools instead (`backend/src/cti_app/api/investigation.py:1656-1669`). |
| Historical Run12 evidence | Historical only; current state UNKNOWN | Run12 is a dated closure snapshot, not current deployment telemetry: `docs/README.md:76-80,144-148`. It recorded a Bridge admission denial, fixture-backed C01 results and incomplete editorial closure: `docs/operations/run12-editorial-closure-2026-10-22.md:11-20,24-50`. |

This review did not inspect secret/runtime directories, environment values, deployment manifests containing credentials, or dependency caches. Therefore no secret presence/value, live endpoint, account balance, runtime image, deployed version, database/blob health, active case authorization or sample eligibility is claimed.

## E. Future operator preflight checklist

Run the read-only case checks first, using an authorized operator client and the exact target investigation ID. These API reads are listed for the future operator; they were not called in this review.

1. **Read current case status:**

   ```http
   GET /api/investigations/{investigation_id}/operator-status
   ```

   Expected evidence: `available_tools`, `unavailable_tools` with reasons, G2/revocation state, remaining budgets, pending Bridge turn ID/status, invocation statuses and projected cost. A tool appearing available is not a substitute for checking its actual G2/TLP/external disclosure constraints. Route contract: `backend/src/cti_app/api/investigation.py:1614-1754`.

2. **Read operator context and version:**

   ```http
   GET /api/investigations/{investigation_id}/operator/context?view=summary
   ```

   Expected evidence: current working-state version/SHA and a bounded case summary, suitable for detecting stale planned actions. Route docs: `backend/src/cti_app/api/investigation.py:1162-1174`; operator workflow: `docs/operations/operator-playbook-medium.md:17-22,38-54`.

3. **Read the current G2 envelope:**

   ```http
   GET /api/investigations/{investigation_id}/envelope
   ```

   Expected evidence: active/revoked status, TLP, per-tool/purpose authorization and remaining budget. Stop if missing, revoked, stale, over budget, or if external disclosure is not allowed. Route: `backend/src/cti_app/api/investigation.py:1757-1760`; G2 authority: `docs/investigation_operations.md:19-50,88-105`.

4. **Check deployment runtime attestations through the approved operator/deployment channel.** No public runtime inventory route was found. Expected evidence should include worker reachable/confined/dependencies/profile, exact versions/ruleset hashes, enabled families, Ghidra service contract/version, VT proxy route readiness and Bridge endpoint/route selection. The source inventory schema is `backend/src/cti_app/infrastructure/investigation_runtime_ports.py:868-963`. Do not equate checked-in defaults with the deployment.

5. **Check Bridge capabilities only after explicit authorization to contact Bridge.** This is an external service request, even though it is a GET, so it is not part of an unapproved dry preflight:

   ```http
   GET {openai_bridge_base_url}/bridge/capabilities
   ```

   Expected evidence: Bridge capability response plus service/UI readiness, route/identity, and an operator-confirmed submission budget. Client route and timeout: `backend/src/cti_app/integrations/models.py:541-571`; deployment endpoint selection: `backend/src/cti_app/infrastructure/investigation_runtime_ports.py:848-865`.

6. **Check provider quota/route only after the case explicitly authorizes the VT operation.** The real tool invocation is a provider request, not a harmless health check. Operator tool route:

   ```http
   POST /api/investigations/{investigation_id}/operator/tools/{tool_name}
   Idempotency-Key: {unique_operation_key}
   ```

   Expected response: invocation ID/status, evidence refs and explicit budget consumption. Use only the narrowly authorized tool name and arguments; do not issue a probe request against a sample just to test credentials. Route and required idempotency header: `backend/src/cti_app/api/investigation.py:1462-1520`. VT capability names/routes and limits: `docs/investigation_vt_tools.md:11-33,40-65,83-110`.

7. **Before local analysis**, require an already-associated quarantined sample and operator status showing the specific tool available. For `sample.analyze`, capture the invocation’s canonical binary-analysis/string observation refs and partial reasons. For `local.run_command`, select a catalogued command ID only and retain its observation ref. For PCAP, require a quarantined capture reference with expected SHA/size/format. For Ghidra, require verified startup attestation and exact runtime version. Tool definitions: `backend/src/cti_app/application/investigation_acquisition_tools.py:202-255`, `backend/src/cti_app/application/investigation_local_tools.py:270-369`, `backend/src/cti_app/application/investigation_network_tools.py:50-109`.

8. **Before any Bridge consultation**, review the exact dossier/transmitted refs, TLP, `external_llm_allowed`, `do_not_submit`, G2 context/Bridge-turn reservation and prompt archive availability. Expect prompt bytes/SHA, dossier manifest and transmitted refs to be recorded. A technical preflight does not authorize disclosure: `backend/src/cti_app/application/investigation_advisor_tools.py:684-760,852-862,1020-1100`.

9. **On any ambiguous timeout or partial status**, read `/operator-status`; do not repeat the call. Reconcile only with verified execution evidence, current expected version, response refs and a unique idempotency key using:

   ```http
   POST /api/investigations/{investigation_id}/invocations/{invocation_id}/reconcile
   Idempotency-Key: {unique_reconciliation_key}
   ```

   Expected evidence: a versioned terminal invocation status and response refs. Route contract: `backend/src/cti_app/api/investigation.py:3847-3911`; ambiguous Bridge outcomes are preserved rather than blindly retried: `backend/src/cti_app/application/investigation_advisor_tools.py:3585-3795`.

10. **Keep the authorization boundary explicit:** do not upload/submit samples, execute malware, contact C2, publish/release/repin, or widen the approved purpose. These remain separate actions requiring explicit authorization (`docs/operations/operator-playbook-medium.md:84-89,103-110`; root `AGENTS.md` non-negotiable boundaries).

## Classification counts

Counts below are for the 46 capability/prerequisite rows in sections A–D, using the primary classification printed at the start of each row. `READY`, `PARTIAL`, `UNAVAILABLE`, `BLOCKED`, and `UNKNOWN` are source-contract/config assessments, not live passes. Every real deployment’s live readiness remains UNKNOWN unless the row explicitly says a checked-in default is disabled.

- READY: 21
- PARTIAL: 13
- UNAVAILABLE: 0
- BLOCKED: 1
- UNKNOWN: 11
- Verified live READY: 0

